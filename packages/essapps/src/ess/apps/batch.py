# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Batch: making requests from rules and templates (D14).

Batch and automatic reduction are one mechanism seen twice. :func:`apply` is
the one operation: it makes a batch from a rule, or from a template and its
lookup. :func:`backlog`, :func:`reprocess`, and :func:`rerun` call it with a
query. :class:`TriggerLoop` calls it with one dataset whenever the five clauses
of :func:`trigger_status` hold, and keeps no memory of what it fired on.
:func:`shadowed` is the check a reprocess itself does not make: which typed
values it would carry forward though their fill has since changed.

Nothing here stores a batch: a batch is the records under one label, and
:func:`batch_table` is that query, the latest record per member key, as a
:class:`pandas.DataFrame`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd
from pydantic import BaseModel

from .backend import SubmitError
from .client import Client
from .records import RunRecord, RunRequest, Submission
from .rules import AsOf, Lookup, LookupEntry, Rule, Series, Template, matches, precedes
from .sources import Dataset
from .spec import DatasetRef, OutputRef


def apply(
    client: Client,
    rule: Rule | Template,
    datasets: Iterable[Dataset] = (),
    typed: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None = None,
    *,
    lookup: Lookup | None = None,
    label: str | None = None,
) -> dict[str, RunRequest]:
    """
    Make requests from a rule, or from a template with its lookup.

    The members are the given datasets, keyed by dataset identity, or the keys
    of ``typed`` when no dataset is given, which is the batch form. Each member
    is filled through the precedence ladder, the template, then the lookup entry
    that matched its dataset, then the values the submitter typed, over the
    dataset itself, which fills the template's dataset field. ``typed`` may be a
    frame with the member key as index and the typed values as columns.

    The group is returned, not submitted, so that it can be previewed through
    :meth:`Client.validate` and submitted whole. For a rule with a series, each
    member's request is a contribute run and is followed by a combine request
    chained to the previous combine of its series (D15).
    """
    template, of_rule = (
        (rule.template, rule) if isinstance(rule, Rule) else (rule, None)
    )
    series = of_rule.series if of_rule is not None else None
    lookup = lookup or (of_rule.lookup if of_rule is not None else None)
    label = label or rule.name
    values = _typed(typed)
    datasets = list(datasets)
    members: list[tuple[str, Dataset | None]] = (
        [(str(d.ref), d) for d in datasets]
        if datasets
        else [(key, None) for key in values]
    )
    group: dict[str, RunRequest] = {}
    chain: dict[str, OutputRef] = {}
    for key, dataset in members:
        entry = (
            lookup.entry(dataset)
            if lookup is not None and dataset is not None
            else None
        )
        fills = _member_fill(client, template, entry, dataset) | values.get(key, {})
        group[key] = client.request(
            template.spec,
            template.fill(**fills),
            label=label,
            member_key=key,
            stage='run' if series is None else 'contribute',
            submission=Submission(
                template=template.id,
                rule=of_rule.id if of_rule is not None else None,
                entry=None if entry is None else entry.name,
                typed=dict(values.get(key, {})),
            ),
        )
        if of_rule is not None and series is not None and dataset is not None:
            name, request = _combine(
                client, of_rule, series, dataset, key, label, chain
            )
            group[name] = request
    return group


def _combine(
    client: Client,
    rule: Rule,
    series: Series,
    dataset: Dataset,
    member: str,
    label: str,
    chain: dict[str, OutputRef],
) -> tuple[str, RunRequest]:
    """
    The combine request one arrival of a series submits (D15).

    It is chained to the previous combine of the series, which is the latest
    record under the series value as member key, or to the one made earlier in
    this group. Which series a dataset belongs to is asked of the source here
    and never stored, so a metadata correction moves a run between series.
    """
    spec = client.registry.spec(rule.template.spec)
    if spec.contribution is None:
        raise ValueError(f'{spec.id} declares no contribution to combine (D15)')
    if series.key not in dataset.fields:
        raise ValueError(f'{dataset.ref} has no field {series.key!r} to key a series')
    value = str(dataset.fields[series.key])
    previous = chain.get(value)
    if previous is None and (last := client.latest(label, value)) is not None:
        previous = OutputRef(record=last.id, output=spec.contribution)
    name = f'{member}+combine'
    chain[value] = OutputRef(record=f'@{name}', output=spec.contribution)
    return name, client.request(
        spec.id,
        series.finalize,
        label=label,
        member_key=value,
        stage='combine',
        contributions=[
            *([] if previous is None else [previous]),
            OutputRef(record=f'@{member}', output=spec.contribution),
        ],
        submission=Submission(rule=rule.id),
    )


def _member_fill(
    client: Client,
    template: Template,
    entry: LookupEntry | None,
    dataset: Dataset | None,
) -> dict[str, Any]:
    """The dataset's own field plus the lookup entry's, an as-of fill resolved."""
    if dataset is None:
        return {}
    fills = {template.field_for_dataset(): dataset.ref}
    for field, value in (entry.fills if entry is not None else {}).items():
        fills[field] = (
            _as_of(client, dataset, field, value) if isinstance(value, AsOf) else value
        )
    return fills


def _as_of(client: Client, dataset: Dataset, field: str, as_of: AsOf) -> DatasetRef:
    """The reference of the nearest dataset matching ``as_of`` before ``dataset``."""
    best: Dataset | None = None
    for candidate in client.datasets():
        if not matches(as_of.match, candidate) or not precedes(candidate, dataset):
            continue
        if best is None or precedes(best, candidate):
            best = candidate
    if best is None:
        raise ValueError(
            f'{dataset.ref}: no dataset matching {as_of.match} before it for {field!r}'
        )
    return best.ref


def _typed(
    typed: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None,
) -> dict[str, dict[str, Any]]:
    """Per-member typed values; a frame is indexed by the member key."""
    if typed is None:
        return {}
    if isinstance(typed, pd.DataFrame):
        typed = typed.to_dict('index')
    return {str(key): dict(row) for key, row in typed.items()}


def backlog(client: Client, rule: Rule) -> dict[str, RunRequest]:
    """
    The datasets before the rule's bound that its selector matches.

    Offered when a rule is created; nothing is submitted until the group is.
    """
    return apply(
        client,
        rule,
        [
            dataset
            for dataset in client.datasets()
            if rule.selector.selects(dataset)
            and not rule.selector.after.passes(dataset)
            and str(dataset.ref) not in rule.exclusions
        ],
    )


def _stale(client: Client, rule: Rule) -> list[RunRecord]:
    """The heads under the rule's label made by a version other than ``rule``."""
    return [
        record
        for record in client.batch(rule.name)
        if record.request.submission.rule != rule.id
    ]


def reprocess(client: Client, rule: Rule) -> dict[str, RunRequest]:
    """
    The members whose latest record under the label came from an older rule version.

    Offered when a rule moves to a new template or lookup version. What the
    submitter typed is carried forward and what the template and lookup filled
    is made again from the new versions.
    """
    return _again(client, rule, _stale(client, rule))


def rerun(
    client: Client, rule: Rule | Template, *, label: str | None = None
) -> dict[str, RunRequest]:
    """The members under a label that have no completed record."""
    label = label or rule.name
    return _again(
        client, rule, client.members_without_completed_record(label), label=label
    )


def _selected_members(
    known: Mapping[str, Dataset], rule: Rule | Template, records: Iterable[RunRecord]
) -> list[RunRecord]:
    """Records of ``records`` whose dataset is known, not excluded, not a combine."""
    excluded = rule.exclusions if isinstance(rule, Rule) else {}
    return [
        record
        for record in records
        if record.request.member_key in known
        and record.request.member_key not in excluded
        and record.request.stage != 'combine'
    ]


def _again(
    client: Client,
    rule: Rule | Template,
    records: Iterable[RunRecord],
    *,
    label: str | None = None,
) -> dict[str, RunRequest]:
    """Apply again over the datasets of these records, carrying the typed values."""
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    members = _selected_members(known, rule, records)
    return apply(
        client,
        rule,
        [known[str(r.request.member_key)] for r in members],
        {str(r.request.member_key): r.request.submission.typed for r in members},
        label=label,
    )


def _without_typed(client: Client, rule: Rule, dataset: Dataset) -> dict[str, Any]:
    """The template and lookup fill for a dataset, before what a person typed."""
    template = rule.template
    entry = rule.lookup.entry(dataset) if rule.lookup is not None else None
    return template.params | _member_fill(client, template, entry, dataset)


def shadowed(client: Client, rule: Rule, previous: Rule) -> pd.DataFrame:
    """
    Typed values a reprocess would carry forward though their fill changed.

    A reprocess is a rebase of what was typed onto the new template and lookup:
    it keeps the typed values and recomputes the rest. The precedence ladder
    lets a typed value win over the lookup, so a value typed under ``previous``
    silently shadows a fill that ``rule``'s lookup now gives differently. This
    is the three-way compare a rebase does and the ladder does not, so that a
    person decides whether the typed value still stands.
    """
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    members = _selected_members(known, rule, _stale(client, rule))
    rows: list[dict[str, Any]] = []
    for record in members:
        dataset = known[str(record.request.member_key)]
        was = _without_typed(client, previous, dataset)
        now = _without_typed(client, rule, dataset)
        rows += [
            {
                'member': record.request.member_key,
                'field': field,
                'typed': value,
                'was': was.get(field),
                'now': now.get(field),
            }
            for field, value in record.request.submission.typed.items()
            if was.get(field) != now.get(field)
        ]
    frame = pd.DataFrame(rows, columns=['member', 'field', 'typed', 'was', 'now'])
    return frame.set_index('member')


class TriggerStatus(BaseModel, frozen=True):
    """Why a rule fires on a dataset, or does not."""

    fires: bool
    reason: str


def trigger_status(client: Client, rule: Rule, dataset: Dataset) -> TriggerStatus:
    """
    The trigger status of one dataset: the loop's decision, and why.

    The clauses are the loop's: the rule is active, the selector matches, the
    dataset lies after the rule's bound, it is not excluded, and no record
    exists under the rule's label with it as member key, unless the retry policy
    names the failure of the records that do. A facility adds one clause here,
    that the dataset's catalogue entry does not carry our provenance snapshot
    (D11), which the local application has no catalogue for.
    """
    member = str(dataset.ref)
    if not rule.active:
        return TriggerStatus(fires=False, reason=f'rule {rule.name} is paused')
    if not rule.selector.selects(dataset):
        return TriggerStatus(fires=False, reason='the selector does not match')
    if not rule.selector.after.passes(dataset):
        return TriggerStatus(fires=False, reason="before the rule's bound")
    if (reason := rule.exclusions.get(member)) is not None:
        return TriggerStatus(fires=False, reason=f'excluded: {reason}')
    records = client.records(label=rule.name, member_key=member)
    if not records:
        return TriggerStatus(fires=True, reason='no record under the label yet')
    failure = records[-1].failure
    if failure is None or failure.kind not in rule.retry.kinds:
        return TriggerStatus(
            fires=False, reason=f'{len(records)} record(s) under the label'
        )
    if len(records) >= rule.retry.limit:
        return TriggerStatus(
            fires=False, reason=f'{failure.kind} retried {rule.retry.limit} times'
        )
    return TriggerStatus(fires=True, reason=f'retry after {failure.kind}')


class TriggerLoop:
    """
    Runs rules over the datasets the sources know, and nothing else does.

    The loop keeps no memory: every clause of :func:`trigger_status` is a query
    over the records and the sources, so what arrived while the backend was down
    is fired on when it comes back, and a restart is a no-op by construction.
    ``refusals`` is what the last pass was refused, a log for a user to read,
    never read by the loop itself.
    """

    def __init__(self, client: Client, *rules: Rule) -> None:
        self.client = client
        self.rules = list(rules)
        self.refusals: dict[str, str] = {}
        for rule in self.rules:
            self.client.backend.reserve(rule.name, rule.name)

    def run_once(self) -> list[RunRecord]:
        """Fire every rule on every dataset it should, and return what was made."""
        self.refusals = {}
        datasets = self.client.datasets()
        fired: list[RunRecord] = []
        for rule in self.rules:
            for dataset in datasets:
                if not trigger_status(self.client, rule, dataset).fires:
                    continue
                try:
                    group = apply(self.client, rule, [dataset])
                    fired += self.client.submit_group(group).values()
                except (SubmitError, ValueError) as e:
                    self.refusals[f'{rule.name} {dataset.ref}'] = str(e)
        return fired


def batch_table(client: Client, batch: Rule | str) -> pd.DataFrame:
    """
    The table of what was reduced with which values: the batch under a label.

    A query over the records, latest per member key, never a stored table: one
    row per member, the member key as index, and each record's rule version,
    lookup entry, and typed values as columns. A rule's exclusions are rows
    without a record.
    """
    rule = batch if isinstance(batch, Rule) else None
    label = rule.name if rule is not None else batch
    rows: dict[str, dict[str, Any]] = {}
    for record in client.batch(str(label)):
        submission = record.request.submission
        rows[str(record.request.member_key)] = {
            'record': record.id,
            'status': record.status.value,
            'stage': record.request.stage,
            'template': submission.template,
            'rule': submission.rule,
            'entry': submission.entry,
            **submission.typed,
        }
    for member, reason in (rule.exclusions if rule is not None else {}).items():
        rows.setdefault(member, {'status': 'excluded', 'reason': reason})
    return pd.DataFrame.from_dict(rows, orient='index').rename_axis('member')

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Batch: making requests from rules and templates.

Batch and automatic reduction are one mechanism seen twice. :func:`apply` is
the one operation: it makes a batch from a rule, or from a template and its
lookup. :func:`backlog`, :func:`reprocess`, and :func:`rerun` call it with a
query. :class:`TriggerLoop` calls it with one dataset whenever the five clauses
of :func:`trigger_status` hold, and keeps no memory of what it fired on.
:func:`shadowed` is the check a reprocess itself does not make: which typed
values it would carry forward though their fill has since changed.

Nothing here stores a batch: a batch is the records under one label, and
:func:`batch_table` is that query, the latest record per member key, as a
:class:`pandas.DataFrame`. :func:`dataset_table` is the frame to join it with to
read a rule's member keys as samples.

See docs/developer/rules.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd
from pydantic import BaseModel

from .backend import GROUP_PREFIX, SubmitError
from .client import Client
from .records import RunRecord, RunRequest, Status, Submission
from .rules import AsOf, Lookup, LookupEntry, Rule, Series, Template, matches, precedes
from .sources import Dataset
from .spec import DatasetRef, OutputRef, SpecId, as_ref


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
    member's request is followed by a combine request over the series it belongs
    to.
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
    arrived: dict[str, list[str]] = {}
    combines: dict[str, str] = {}
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
            submission=Submission(
                template=template.id,
                rule=of_rule.id if of_rule is not None else None,
                lookup=None if lookup is None else lookup.id,
                entry=None if entry is None else entry.name,
                typed=dict(values.get(key, {})),
            ),
        )
        if of_rule is not None and series is not None and dataset is not None:
            if series.key not in dataset.fields:
                raise ValueError(
                    f'{dataset.ref} has no field {series.key!r} to key a series'
                )
            value = str(dataset.fields[series.key])
            arrived.setdefault(value, []).append(key)
            name = f'{key}+combine'
            group[name] = _combine(
                client,
                of_rule,
                series,
                label,
                value,
                group,
                arrived[value],
                combines.get(value),
            )
            combines[value] = name
    return group


def _combine(
    client: Client,
    rule: Rule,
    series: Series,
    label: str,
    value: str,
    group: Mapping[str, RunRequest],
    arrived: Iterable[str],
    previous_name: str | None,
) -> RunRequest:
    """
    The combine request one arrival of a series submits.

    It references the contribution of every current member of the series. If the
    combine spec declares that its combined output may come back as an element of
    the collection, and the previous combine covers only records that are still
    current members, it references that combine instead of the members it covers.
    A member that was corrected, excluded, or reprocessed leaves the previous
    combine covering a record that is no longer current, and then the combine is
    made over all current members, so that a correction is not counted twice.
    """
    spec = client.registry.spec(series.template.spec)
    members = _current_members(client, rule, series, label, value)
    members |= {str(group[name].member_key): GROUP_PREFIX + name for name in arrived}
    contributions = [
        OutputRef(record=record, output=series.output) for record in members.values()
    ]
    chained = spec.chain.get(series.parameter)
    previous = _previous_combine(client, group, label, value, previous_name)
    if chained is not None and previous is not None:
        record, request = previous
        covered = _covers(client, group, series.parameter, spec.id, request)
        if covered <= set(members.values()):
            contributions = [OutputRef(record=record, output=chained)] + [
                OutputRef(record=r, output=series.output)
                for r in members.values()
                if r not in covered
            ]
    return client.request(
        spec.id,
        series.template.fill(**{series.parameter: contributions}),
        label=label,
        member_key=value,
        submission=Submission(template=series.template.id, rule=rule.id),
    )


def _current_members(
    client: Client, rule: Rule, series: Series, label: str, value: str
) -> dict[str, str]:
    """
    The current members of one series: record ID by member key.

    The latest record per member key under the label that is a member of this
    rule's template, is not excluded, did not fail or get cancelled, and whose
    dataset still carries this series value.
    """
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    members: dict[str, str] = {}
    for record in client.batch(label):
        key = str(record.request.member_key)
        dataset = known.get(key)
        if record.spec != rule.template.spec or key in rule.exclusions:
            continue
        if record.status in (Status.FAILED, Status.CANCELLED):
            continue
        if dataset is None or str(dataset.fields.get(series.key)) != value:
            continue
        members[key] = record.id
    return members


def _previous_combine(
    client: Client,
    group: Mapping[str, RunRequest],
    label: str,
    value: str,
    previous_name: str | None,
) -> tuple[str, RunRequest] | None:
    """The combine this arrival may chain onto, by record ID and request."""
    if previous_name is not None:
        return GROUP_PREFIX + previous_name, group[previous_name]
    record = client.latest(label, value)
    if record is None or record.status in (Status.FAILED, Status.CANCELLED):
        return None
    return record.id, record.request


def _covers(
    client: Client,
    group: Mapping[str, RunRequest],
    parameter: str,
    spec: SpecId,
    request: RunRequest,
) -> set[str]:
    """
    The member records a combine covers, directly or through the combines it
    chains onto, found by following the chained parameter back.

    The walk reads one record per element of the chain, so a series chained one
    arrival at a time costs a record read per member of it. Keeping the covered
    set on the record instead would make every writer responsible for it.
    """
    covered: set[str] = set()
    for element in request.params.get(parameter, []):
        ref = as_ref(element)
        if ref is None or not isinstance(ref, OutputRef):
            continue
        producer = _producer(client, group, ref.record)
        if producer is not None and producer.spec == spec:
            covered |= _covers(client, group, parameter, spec, producer)
        else:
            covered.add(ref.record)
    return covered


def _producer(
    client: Client, group: Mapping[str, RunRequest], record: str
) -> RunRequest | None:
    """The request behind a reference, in this group or in the record store."""
    if record.startswith(GROUP_PREFIX):
        return group.get(record[len(GROUP_PREFIX) :])
    return client.record(record).request


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
    """Records of ``records`` that are members: a known dataset, not excluded.

    A combine of a series is under the same label; it is told from a member by
    its spec, which is the combine template's and not the member template's.
    """
    excluded = rule.exclusions if isinstance(rule, Rule) else {}
    spec = rule.template.spec if isinstance(rule, Rule) else rule.spec
    return [
        record
        for record in records
        if record.spec == spec
        and record.request.member_key in known
        and record.request.member_key not in excluded
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

    A typed value replaces its field whole, so a model typed to move one of its
    values shadows the others as well. The rows are therefore per leaf, named as
    in :func:`batch_table`: the leaves of a typed field whose fill changed.
    """
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    members = _selected_members(known, rule, _stale(client, rule))
    rows: list[dict[str, Any]] = []
    for record in members:
        dataset = known[str(record.request.member_key)]
        was = _without_typed(client, previous, dataset)
        now = _without_typed(client, rule, dataset)
        for field, value in record.request.submission.typed.items():
            typed = _leaves(field, value)
            before = _leaves(field, was.get(field))
            after = _leaves(field, now.get(field))
            rows += [
                {
                    'member': record.request.member_key,
                    'field': leaf,
                    'typed': typed.get(leaf),
                    'was': before.get(leaf),
                    'now': after.get(leaf),
                }
                for leaf in typed | before | after
                if before.get(leaf) != after.get(leaf)
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
    that the dataset's catalogue entry does not carry our provenance snapshot,
    which the local application has no catalogue for.
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
    row per member, the member key as index, and each record's rule version and
    lookup entry as columns. The value columns are the fields that differ per
    member, which are the blanks of the rule's template and every field a member
    typed. Each shows the value the request was made with, whoever supplied it,
    and ``typed`` names the fields of the row a person typed. A field holding a
    model is one column per leaf, ``q.start`` and ``q.stop``, and a reference
    is shown as the reference rather than in its stored form. A rule's
    exclusions are rows without a record.
    """
    rule = batch if isinstance(batch, Rule) else None
    label = rule.name if rule is not None else batch
    records = client.batch(str(label))
    fields = dict.fromkeys(rule.template.blanks if rule is not None else ())
    for record in records:
        fields |= dict.fromkeys(record.request.submission.typed)
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        submission = record.request.submission
        params = record.request.params
        rows[str(record.request.member_key)] = {
            'record': record.id,
            'status': record.status.value,
            'spec': str(record.spec),
            'template': submission.template,
            'rule': submission.rule,
            'lookup': submission.lookup,
            'entry': submission.entry,
            'typed': ', '.join(submission.typed),
        }
        for field in fields:
            if field in params:
                rows[str(record.request.member_key)] |= _leaves(field, params[field])
    for member, reason in (rule.exclusions if rule is not None else {}).items():
        rows.setdefault(member, {'status': 'excluded', 'reason': reason})
    return pd.DataFrame.from_dict(rows, orient='index').rename_axis('member')


def _leaves(name: str, value: Any) -> dict[str, Any]:
    """The leaf values of a parameter by dotted name; a reference is a leaf."""
    if (ref := as_ref(value)) is not None:
        return {name: ref}
    if isinstance(value, dict):
        leaves: dict[str, Any] = {}
        for key, inner in value.items():
            leaves |= _leaves(f'{name}.{key}', inner)
        return leaves
    return {name: value}


def dataset_table(client: Client) -> pd.DataFrame:
    """
    The datasets the sources know, with the fields a rule may match on.

    The index is the dataset identity, which is the member key of a rule's
    batch, so ``batch_table(client, rule).join(dataset_table(client))`` says
    which sample each member is. The batch table does not make that join itself:
    it is a query over the records alone, and a batch typed by hand is keyed by
    names that are no dataset.
    """
    rows = {str(dataset.ref): dataset.fields for dataset in client.datasets()}
    return pd.DataFrame.from_dict(rows, orient='index').rename_axis('dataset')

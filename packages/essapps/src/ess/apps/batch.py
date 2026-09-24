# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Batch: making requests from rules and templates.

Batch and automatic reduction are one mechanism seen twice. :func:`apply` is
the one operation: it makes a batch from a rule, or from a template and its
lookup. :func:`backlog`, :func:`reprocess`, and :func:`retry` call it with a
query. :class:`TriggerLoop` calls it with one dataset whenever the five clauses
of :func:`trigger_status` hold, and keeps no memory of what it fired on.
:func:`shadowed` is the check a reprocess itself does not make: which pinned
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

from .backend import SubmitError
from .client import Client
from .records import Group, Origin, RunRecord, RunRequest, Template
from .rules import AsOf, Lookup, LookupEntry, Rule, matches, precedes
from .sources import Dataset
from .spec import DatasetRef, as_ref, dataset_refs


def apply(
    client: Client,
    rule: Rule | Template,
    datasets: Iterable[Dataset] = (),
    pinned: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None = None,
    *,
    lookup: Lookup | None = None,
    label: str | None = None,
) -> Group:
    """
    Make requests from a rule, or from a template with its lookup.

    The members are the given datasets, keyed by dataset identity, or the keys
    of ``pinned`` when no dataset is given, which is the batch form. Each member
    is filled through the precedence ladder, the template, then the lookup entry
    that matched its dataset, then the values the submitter pinned, over the
    dataset itself, which fills the template's dataset field. ``pinned`` may be a
    frame with the member key as index and the pinned values as columns.

    For a rule with a series, a member is a series: the given datasets name the
    series they belong to, keyed by the series value, and the template's
    dataset field holds every current member of each, those given included.

    The group is returned, not submitted, so that it can be previewed through
    :meth:`Client.validate` and submitted whole. It carries the template's
    blanks as what its members vary, so that members that agree on everything
    else share a held stage in a session.
    """
    template, of_rule = (
        (rule.template, rule) if isinstance(rule, Rule) else (rule, None)
    )
    lookup = lookup or (of_rule.lookup if of_rule is not None else None)
    label = label or rule.name
    values = _pinned(pinned)
    datasets = list(datasets)
    series = of_rule.series if of_rule is not None else None
    members: dict[str, list[Dataset]]
    if series is not None and of_rule is not None and datasets:
        members = _series(client, of_rule, series.key, datasets)
    elif datasets:
        members = {str(d.ref): [d] for d in datasets}
    else:
        members = {key: [] for key in values}
    group: dict[str, RunRequest] = {}
    for key, member in members.items():
        fill, entry = _fill(client, template, lookup, member, series=series is not None)
        group[key] = client.request(
            template,
            fill | values.get(key, {}),
            label=label,
            member_key=key,
            origin=Origin(
                template=template.id,
                rule=of_rule.id if of_rule is not None else None,
                lookup=None if lookup is None else lookup.id,
                entry=None if entry is None else entry.name,
                pinned=dict(values.get(key, {})),
            ),
        )
    return Group(group, template.blanks)


def _series(
    client: Client, rule: Rule, key: str, datasets: Iterable[Dataset]
) -> dict[str, list[Dataset]]:
    """
    The current members of each series the datasets belong to, by series value.

    The members of a series are the datasets with its value of the series key
    that the rule's selector matches and that are not excluded, in the order the
    sources list them. The bound does not apply: a series that began before it
    is one series.
    """
    given: dict[str, list[Dataset]] = {}
    for dataset in datasets:
        if key not in dataset.fields:
            raise ValueError(f'{dataset.ref} has no field {key!r} to key a series')
        given.setdefault(str(dataset.fields[key]), []).append(dataset)
    members: dict[str, dict[str, Dataset]] = {value: {} for value in given}
    for dataset in [*client.datasets(), *(d for g in given.values() for d in g)]:
        value = str(dataset.fields.get(key))
        if (
            value in members
            and rule.selector.selects(dataset)
            and str(dataset.ref) not in rule.exclusions
        ):
            members[value].setdefault(str(dataset.ref), dataset)
    return {value: list(found.values()) for value, found in members.items()}


def _fill(
    client: Client,
    template: Template,
    lookup: Lookup | None,
    datasets: list[Dataset],
    *,
    series: bool,
) -> tuple[dict[str, Any], LookupEntry | None]:
    """
    What datasets fill, and the lookup entry they matched.

    The template's dataset field gets the dataset, or for a series the list of
    them, and the fields of the matched lookup entry their values, an as-of
    fill resolved. A series is one request, so its members must match one entry
    that fills the same values for each of them.
    """
    if not datasets:
        return {}, None
    fills = []
    for dataset in datasets:
        entry = lookup.entry(dataset) if lookup is not None else None
        values = {
            field: _as_of(client, dataset, field, value)
            if isinstance(value, AsOf)
            else value
            for field, value in (entry.fills if entry is not None else {}).items()
        }
        fills.append((entry, values))
    for dataset, fill in zip(datasets[1:], fills[1:], strict=True):
        if fill != fills[0]:
            raise ValueError(
                f'{dataset.ref} is filled otherwise than {datasets[0].ref}, '
                'and a series is filled alike for every member'
            )
    refs = [dataset.ref for dataset in datasets]
    entry, values = fills[0]
    field = template.field_for_dataset()
    return {field: refs if series else refs[0]} | values, entry


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


def _pinned(
    pinned: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None,
) -> dict[str, dict[str, Any]]:
    """
    Per-member pinned values; a frame is indexed by the member key.

    A blank cell of a frame, ``None`` or NaN, is not a pinned value: it falls
    through the ladder like a blank in a batch file.
    """
    if pinned is None:
        return {}
    if isinstance(pinned, pd.DataFrame):
        pinned = pinned.to_dict('index')
    return {
        str(key): {field: value for field, value in row.items() if not _blank(value)}
        for key, row in pinned.items()
    }


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, float) and value != value)


def backlog(client: Client, rule: Rule) -> Group:
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
    """The latest records under the rule's label that another version made."""
    return [
        record
        for record in client.batch(rule.name)
        if record.request.origin.rule != rule.id
    ]


def reprocess(client: Client, rule: Rule) -> Group:
    """
    The members whose latest record under the label came from an older rule version.

    Offered when a rule moves to a new template or lookup version. What the
    submitter pinned is carried forward and what the template and lookup filled
    is made again from the new versions.
    """
    return _again(client, rule, _stale(client, rule))


def retry(
    client: Client, rule: Rule | Template, *, label: str | None = None
) -> Group:
    """
    The members under a label whose latest record failed or was cancelled.

    The by-hand form of the trigger loop's retry policy: a member with a record
    in flight or a completed one is not offered.
    """
    label = label or rule.name
    return _again(client, rule, client.members_to_retry(label), label=label)


def _datasets(
    known: Mapping[str, Dataset], rule: Rule | Template, record: RunRecord
) -> list[Dataset]:
    """
    The known datasets a record was made from.

    That is its member key, or for a rule with a series every dataset its
    dataset field lists. A batch made by hand is keyed by names that are no
    dataset, and has none.
    """
    if isinstance(rule, Rule) and rule.series is not None:
        listed = record.request.params.get(rule.template.field_for_dataset())
        keys = [str(ref) for ref in dataset_refs(listed)]
    else:
        keys = [str(record.request.member_key)]
    return [known[key] for key in keys if key in known]


def _again(
    client: Client,
    rule: Rule | Template,
    records: Iterable[RunRecord],
    *,
    label: str | None = None,
) -> Group:
    """Apply again over the datasets of these records, carrying the pinned values."""
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    excluded = rule.exclusions if isinstance(rule, Rule) else {}
    spec = rule.template.spec if isinstance(rule, Rule) else rule.spec
    datasets: dict[str, Dataset] = {}
    pinned: dict[str, dict[str, Any]] = {}
    for record in records:
        found = [
            d
            for d in _datasets(known, rule, record)
            if record.spec == spec and str(d.ref) not in excluded
        ]
        datasets |= {str(d.ref): d for d in found}
        if found:
            pinned[str(record.request.member_key)] = record.request.origin.pinned
    return apply(client, rule, datasets.values(), pinned, label=label)


def _without_pinned(
    client: Client, rule: Rule, datasets: list[Dataset]
) -> dict[str, Any]:
    """The template and lookup fill for datasets, before what a person pinned."""
    fill, _ = _fill(
        client, rule.template, rule.lookup, datasets, series=rule.series is not None
    )
    return rule.template.params | fill


def shadowed(client: Client, rule: Rule, previous: Rule) -> pd.DataFrame:
    """
    Pinned values a reprocess would carry forward though their fill changed.

    A reprocess is a rebase of what was pinned onto the new template and lookup:
    it keeps the pinned values and recomputes the rest. The precedence ladder
    lets a pinned value win over the lookup, so a value pinned under ``previous``
    silently shadows a fill that ``rule``'s lookup now gives differently. This
    is the three-way compare a rebase does and the ladder does not, so that a
    person decides whether the pinned value still stands.

    A pinned value replaces its field whole, so a model pinned to move one of its
    values shadows the others as well. The rows are therefore per leaf, named as
    in :func:`batch_table`: the leaves of a pinned field whose fill changed.
    """
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    rows: list[dict[str, Any]] = []
    for record in _stale(client, rule):
        datasets = _datasets(known, rule, record)
        if record.spec != rule.template.spec or not datasets:
            continue
        was = _without_pinned(client, previous, datasets)
        now = _without_pinned(client, rule, datasets)
        for field, value in record.request.origin.pinned.items():
            pinned = _leaves(field, value)
            before = _leaves(field, was.get(field))
            after = _leaves(field, now.get(field))
            rows += [
                {
                    'member': record.request.member_key,
                    'field': leaf,
                    'pinned': pinned.get(leaf),
                    'was': before.get(leaf),
                    'now': after.get(leaf),
                }
                for leaf in pinned | before | after
                if before.get(leaf) != after.get(leaf)
            ]
    frame = pd.DataFrame(rows, columns=['member', 'field', 'pinned', 'was', 'now'])
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
    exists under the rule's label with it as member key, or for a series no
    record of its series that lists it, unless the retry policy names the
    failure of the records that do. A facility adds one clause here,
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
    if rule.series is None:
        records = client.records(label=rule.name, member_key=member)
    else:
        value = str(dataset.fields.get(rule.series.key))
        field = rule.template.field_for_dataset()
        records = [
            record
            for record in client.records(label=rule.name, member_key=value)
            if dataset.ref in dataset_refs(record.request.params.get(field))
        ]
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
    pinned. Each shows the value the request was made with, whoever supplied it,
    and ``pinned`` names the fields of the row a person pinned. A field holding a
    model is one column per leaf, ``q.start`` and ``q.stop``, and a reference
    is shown as the reference rather than in its stored form. A rule's
    exclusions are rows without a record.
    """
    rule = batch if isinstance(batch, Rule) else None
    label = rule.name if rule is not None else batch
    records = client.batch(str(label))
    fields = dict.fromkeys(rule.template.blanks if rule is not None else ())
    for record in records:
        fields |= dict.fromkeys(record.request.origin.pinned)
    rows: dict[str, dict[str, Any]] = {}
    for record in records:
        origin = record.request.origin
        params = record.request.params
        rows[str(record.request.member_key)] = {
            'record': record.id,
            'status': record.status.value,
            'spec': str(record.spec),
            'template': origin.template,
            'rule': origin.rule,
            'lookup': origin.lookup,
            'entry': origin.entry,
            'pinned': ', '.join(origin.pinned),
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
    it is a query over the records alone, and a batch made by hand is keyed by
    names that are no dataset.
    """
    rows = {str(dataset.ref): dataset.fields for dataset in client.datasets()}
    return pd.DataFrame.from_dict(rows, orient='index').rename_axis('dataset')

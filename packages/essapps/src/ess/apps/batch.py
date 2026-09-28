# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Batch: making requests from rules and templates.

Batch and automatic reduction are one mechanism seen twice. :func:`apply` is
the one operation: it makes a batch from a rule, or from a template and its
lookup. :func:`backlog`, :func:`reprocess`, and :func:`retry` call it with a
query. :class:`TriggerLoop` calls it with one candidate, a dataset or a
completed record of the rule a rule follows (:func:`candidates`), whenever the
clauses of :func:`trigger_status` hold, and keeps no memory of what it fired on.
:func:`shadowed` is the check a reprocess itself does not make: which pinned
values it would carry forward though their fill has since changed.

Nothing here stores a batch: a batch is the records under one label, and
:func:`batch_table` is that query, the latest record per member key, as a
:class:`pandas.DataFrame`. :func:`dataset_table` is the frame to join it with to
read a rule's member keys as samples.

See docs/developer/rules.md.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from .backend import SubmitError
from .client import Client
from .records import Group, Origin, RunRecord, RunRequest, Status, Template
from .rules import Lookup, Nearest, Rule, gap, matches, precedes
from .sources import Dataset
from .spec import DatasetRef, OutputRef, as_ref, dataset_refs, schema_columns, walk_refs


@dataclass(frozen=True)
class Completed:
    """
    A completed record under the label a rule follows, as one of its candidates.

    It has the fields of ``dataset``, the one its member key names, if any. It
    fills a row of references to every output of the record, or with
    ``output`` a reference to that output, to its element ``key`` for a
    collection.
    """

    record: RunRecord
    dataset: Dataset | None = None
    output: str | None = None
    key: str | None = None

    @property
    def fields(self) -> dict[str, Any]:
        return {} if self.dataset is None else self.dataset.fields

    @property
    def ref(self) -> Any:
        if self.output is None:
            return {
                name: self.record.ref(name)
                for name in sorted(self.record.output_names())
            }
        return self.record.ref(self.output, self.key)


Candidate = Dataset | Completed


def _key(candidate: Candidate) -> str:
    """
    A candidate's member key: a dataset's identity, a record's member key with
    the key of the element it stands for.
    """
    if isinstance(candidate, Dataset):
        return str(candidate.ref)
    member = str(candidate.record.request.member_key)
    return member if candidate.key is None else f'{member}[{candidate.key}]'


def candidates(client: Client, rule: Rule | Template) -> list[Candidate]:
    """
    What a rule fires on: the datasets the sources know, or, for a rule that
    follows another, the latest completed record per member key under its label.
    """
    return _candidates(client, rule, client.datasets())


def _candidates(
    client: Client, rule: Rule | Template, datasets: list[Dataset]
) -> list[Candidate]:
    """:func:`candidates` among ``datasets``, what the sources know in one pass."""
    if not isinstance(rule, Rule) or rule.follows is None:
        return list(datasets)
    known = {str(dataset.ref): dataset for dataset in datasets}
    output = rule.follows.output
    found: list[Candidate] = []
    for record in client.batch(rule.follows.label):
        if record.status != Status.COMPLETED:
            continue
        dataset = known.get(str(record.request.member_key))
        keys = None if output is None else record.output_keys(output)
        found += [
            Completed(record, dataset, output, key)
            for key in (sorted(keys) if keys else [None])
        ]
    return found


def _names(value: Any, candidate: Candidate) -> bool:
    """Whether a parameter value references the candidate."""
    if isinstance(candidate, Dataset):
        return candidate.ref in dataset_refs(value)
    return any(
        isinstance(ref, OutputRef)
        and ref.record == candidate.record.id
        and (candidate.key is None or ref.key == candidate.key)
        for _, ref in walk_refs(value)
    )


def apply(
    client: Client,
    rule: Rule | Template,
    datasets: Iterable[Candidate] = (),
    pinned: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None = None,
    *,
    lookup: Lookup | None = None,
    label: str | None = None,
) -> Group:
    """
    Make requests from a rule, or from a template with its lookup.

    The members are the given datasets, keyed by dataset identity, or the
    completed records of the rule it follows (:func:`candidates`), or the keys
    of ``pinned`` when none is given, which is the batch form. Each member
    is filled through the precedence ladder, the template, then the lookup entry
    that matched its dataset, then the values the submitter pinned, over the
    dataset itself, which fills the template's dataset field. ``pinned`` may be a
    frame with the member key as index and the pinned values as columns.

    For a rule with a series, a member is a series: the given datasets name the
    series they belong to, keyed by the series value, and the template's
    dataset field holds every current member of each, those given included.
    When the dataset field is a column of a list parameter of rows, each
    dataset is one row, filled from its own lookup entry. A series that fires
    once complete waits until it is.

    A pinned value replaces the fill of its field, so a fill it replaces is not
    resolved: pinning the can of a member that waits for one, or is refused
    for want of one, makes the member.

    The group is returned, not submitted, so that it can be previewed through
    :meth:`Client.validate` and submitted whole. It carries the template's
    blanks as what its members vary, so that members that agree on everything
    else share a held stage in a session. A member that cannot be made yet,
    because what it needs may still arrive, is left out of the group and listed
    in its ``waiting`` with the reason.
    """
    return _apply(
        client,
        rule,
        datasets,
        pinned,
        lookup=lookup,
        label=label,
        known=client.datasets(),
    )


def _apply(
    client: Client,
    rule: Rule | Template,
    datasets: Iterable[Candidate],
    pinned: Mapping[str, Mapping[str, Any]] | pd.DataFrame | None,
    *,
    lookup: Lookup | None = None,
    label: str | None = None,
    known: list[Dataset],
) -> Group:
    """:func:`apply` among ``known``, the datasets the sources know in one pass."""
    template, of_rule = (
        (rule.template, rule) if isinstance(rule, Rule) else (rule, None)
    )
    lookup = lookup or (of_rule.lookup if of_rule is not None else None)
    label = label or rule.name
    values = _pinned(pinned)
    datasets = list(datasets)
    series = of_rule.series if of_rule is not None else None
    if lookup is not None and datasets:
        _check_fills(client, template, lookup)
    members: dict[str, list[Candidate]]
    if series is not None and of_rule is not None and datasets:
        pool = _candidates(client, of_rule, known)
        members = _series(of_rule, series.key, datasets, pool)
    elif datasets:
        members = {_key(d): [d] for d in datasets}
    else:
        members = {key: [] for key in values}
    group: dict[str, RunRequest] = {}
    waiting: dict[str, str] = {}
    for key, member in members.items():
        try:
            if series is not None and member and not isinstance(series.fire, str):
                if (missing := series.fire.missing(member)) is not None:
                    raise _Waits(f'series {key}: {missing}')
            fill, entries = _fill(
                template,
                lookup,
                member,
                known,
                series=series is not None,
                pinned=values.get(key, {}),
            )
        except _Waits as e:
            waiting[key] = str(e)
            continue
        group[key] = client.request(
            template,
            fill | values.get(key, {}),
            label=label,
            member_key=key,
            origin=Origin(
                template=template.id,
                rule=of_rule.id if of_rule is not None else None,
                lookup=None if lookup is None else lookup.id,
                entries=entries,
                pinned=dict(values.get(key, {})),
            ),
        )
    return Group(group, template.blanks, waiting)


def _series(
    rule: Rule, key: str, datasets: Iterable[Candidate], pool: list[Candidate]
) -> dict[str, list[Candidate]]:
    """
    The current members of each series the datasets belong to, by series value.

    The members of a series are the candidates, the rule's in ``pool`` and
    those given, with its value of the series key that the rule's selector
    matches and that are not excluded, in the order the sources list them.
    The bound does not apply: a series that began before it is one series.
    """
    given: dict[str, list[Candidate]] = {}
    for dataset in datasets:
        if key not in dataset.fields:
            raise ValueError(f'{_key(dataset)} has no field {key!r} to key a series')
        given.setdefault(str(dataset.fields[key]), []).append(dataset)
    members: dict[str, dict[str, Candidate]] = {value: {} for value in given}
    for dataset in [*pool, *(d for g in given.values() for d in g)]:
        value = str(dataset.fields.get(key))
        if (
            value in members
            and rule.selector.selects(dataset)
            and _key(dataset) not in rule.exclusions
        ):
            members[value].setdefault(_key(dataset), dataset)
    return {value: list(found.values()) for value, found in members.items()}


def _fill(
    template: Template,
    lookup: Lookup | None,
    datasets: list[Candidate],
    known: list[Dataset],
    *,
    series: bool,
    pinned: Collection[str] = (),
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    What datasets fill, and the lookup entry each matched, by member key.

    The template's dataset field gets the dataset, or for a series the list of
    them. Each dataset is filled from its own lookup entry, a nearest fill
    resolved against it among the ``known`` datasets, except where a ``pinned``
    field replaces the fill, or the rows it would go into. When the dataset
    field is a column of a list parameter of rows, ``runs.run``, each dataset
    is a row, a list of one row outside a series, and a fill named for another
    column, ``runs.floor``, goes into that dataset's row. Every other fill goes
    into the request, which has one value per field, so members of a series
    that fill such a field differently are refused.
    """
    if not datasets:
        return {}, {}
    field, _, column = template.field_for_dataset().partition('.')
    requests: list[dict[str, Any]] = []
    members: list[Any] = []
    entries: dict[str, str] = {}
    for dataset in datasets:
        if isinstance(dataset, Dataset) and dataset.error is not None:
            raise ValueError(dataset.error)
        entry = lookup.entry(dataset) if lookup is not None else None
        request: dict[str, Any] = {}
        row: dict[str, Any] = {column: dataset.ref}
        if entry is not None:
            entries[_key(dataset)] = entry.name
        for name, fill in (entry.fills if entry is not None else {}).items():
            owner, _, inner = name.partition('.')
            if name in pinned or owner in pinned:
                continue
            value = (
                _nearest(dataset, name, fill, known)
                if isinstance(fill, Nearest)
                else fill
            )
            if column and owner == field:
                row[inner] = value
            else:
                request[name] = value
        if requests and request != requests[0]:
            raise ValueError(
                f'{_key(dataset)} fills the request otherwise than '
                f'{_key(datasets[0])}, and a request has one value per field'
            )
        requests.append(request)
        members.append(row if column else dataset.ref)
    return {field: members if series or column else members[0]} | requests[0], entries


def _listed(template: Template, params: Mapping[str, Any]) -> list[Any]:
    """
    What a request's dataset field holds of each member, the inverse of
    :func:`_fill`: of a list of rows only the dataset column, so that another
    reference in a row, a transmission run, names no member.
    """
    field, _, column = template.field_for_dataset().partition('.')
    value = params.get(field)
    listed = value if isinstance(value, list) else [value]
    if not column:
        return listed
    return [row.get(column) if isinstance(row, dict) else None for row in listed]


def _check_fills(client: Client, template: Template, lookup: Lookup) -> None:
    """
    Refuse a fill that names no parameter of the template's spec, or no column
    of the list of rows a dataset is a row of, or that replaces the dataset.

    A row ignores names it does not declare, so a misspelt column would leave
    its default in every row and in the record, with nothing to say so.
    """
    schema = client.spec(template.spec).params_schema
    dataset_field = template.field_for_dataset()
    field, _, column = dataset_field.partition('.')
    columns = schema_columns(schema, field) or ()
    for entry in lookup.entries:
        for name in entry.fills:
            owner, dot, inner = name.partition('.')
            if name in (field, dataset_field):
                error = f'fills {dataset_field!r}, which a dataset fills'
            elif dot and not column:
                error = f'names a column, but a dataset fills {dataset_field!r}'
            elif dot and (owner != field or inner not in columns):
                error = f'is no column of {field!r}, the rows a dataset is one of'
            elif not dot and name not in schema.get('properties', {}):
                error = f'is not a parameter of {template.spec}'
            else:
                continue
            raise ValueError(
                f'lookup {lookup.id}, entry {entry.name}: {name!r} {error}'
            )


class _Waits(Exception):
    """
    A member cannot be made yet, because what it needs may still arrive.

    Raised while one member is filled and caught by :func:`apply`, which lists
    the member as waiting.
    """


def _nearest(
    member: Candidate, field: str, nearest: Nearest, known: list[Dataset]
) -> DatasetRef:
    """
    The reference of the dataset among ``known`` that ``nearest`` resolves to
    for a member, which is resolved against the member's dataset.
    """
    dataset = member if isinstance(member, Dataset) else member.dataset
    if dataset is None:
        raise ValueError(f'{_key(member)} has no dataset to resolve {field!r} by')
    before: Dataset | None = None
    after: Dataset | None = None
    for candidate in known:
        if not matches(nearest.match, candidate) or any(
            candidate.fields.get(name) != dataset.fields.get(name)
            for name in nearest.same
        ):
            continue
        if precedes(candidate, dataset) and (
            before is None or precedes(before, candidate)
        ):
            before = candidate
        if precedes(dataset, candidate) and (
            after is None or precedes(candidate, after)
        ):
            after = candidate
    what = f'{dataset.ref}: no dataset matching {nearest.match}'
    if nearest.direction == 'before':
        if before is None:
            raise ValueError(f'{what} before it for {field!r}')
        return before.ref
    if after is None:
        raise _Waits(f'{what} after it yet for {field!r}')
    if nearest.direction == 'after' or before is None:
        return after.ref
    return (before if gap(before, dataset) <= gap(dataset, after) else after).ref


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
    known = client.datasets()
    return _apply(
        client,
        rule,
        [
            candidate
            for candidate in _candidates(client, rule, known)
            if rule.selector.selects(candidate)
            and not _after(rule, candidate)
            and _key(candidate) not in rule.exclusions
        ],
        None,
        known=known,
    )


def _after(rule: Rule, candidate: Candidate) -> bool:
    """Whether a candidate lies after the rule's bound, by its dataset."""
    dataset = candidate if isinstance(candidate, Dataset) else candidate.dataset
    return dataset is None or rule.selector.after.passes(dataset)


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


def retry(client: Client, rule: Rule | Template, *, label: str | None = None) -> Group:
    """
    The members under a label whose latest record failed or was cancelled.

    The by-hand form of the trigger loop's retry policy: a member with a record
    in flight or a completed one is not offered.
    """
    label = label or rule.name
    return _again(client, rule, client.members_to_retry(label), label=label)


def _datasets(
    known: Mapping[str, Candidate], rule: Rule | Template, record: RunRecord
) -> list[Candidate]:
    """
    The known candidates a record was made from.

    That is its member key, or for a rule with a series every candidate its
    dataset field lists. A batch made by hand is keyed by names that are no
    candidate, and has none.
    """
    if isinstance(rule, Rule) and rule.series is not None:
        listed = _listed(rule.template, record.request.params)
        return [c for c in known.values() if _names(listed, c)]
    member = known.get(str(record.request.member_key))
    return [] if member is None else [member]


def _again(
    client: Client,
    rule: Rule | Template,
    records: Iterable[RunRecord],
    *,
    label: str | None = None,
) -> Group:
    """Apply again over the datasets of these records, carrying the pinned values."""
    known = client.datasets()
    by_key = {_key(c): c for c in _candidates(client, rule, known)}
    excluded = rule.exclusions if isinstance(rule, Rule) else {}
    spec = rule.template.spec if isinstance(rule, Rule) else rule.spec
    datasets: dict[str, Candidate] = {}
    pinned: dict[str, dict[str, Any]] = {}
    for record in records:
        found = [
            d
            for d in _datasets(by_key, rule, record)
            if record.spec == spec and _key(d) not in excluded
        ]
        datasets |= {_key(d): d for d in found}
        if found:
            pinned[str(record.request.member_key)] = record.request.origin.pinned
    return _apply(client, rule, datasets.values(), pinned, label=label, known=known)


def _without_pinned(
    rule: Rule, datasets: list[Candidate], known: list[Dataset]
) -> dict[str, Any]:
    """The template and lookup fill for datasets, before what a person pinned."""
    try:
        fill, _ = _fill(
            rule.template, rule.lookup, datasets, known, series=rule.series is not None
        )
    except _Waits as e:
        raise ValueError(
            f'cannot compare the fills of a member that waits: {e}'
        ) from None
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
    known = client.datasets()
    by_key = {_key(c): c for c in _candidates(client, rule, known)}
    rows: list[dict[str, Any]] = []
    for record in _stale(client, rule):
        datasets = _datasets(by_key, rule, record)
        if record.spec != rule.template.spec or not datasets:
            continue
        was = _without_pinned(previous, datasets, known)
        now = _without_pinned(rule, datasets, known)
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
    """
    Whether the trigger loop fires a rule on a candidate, and why.

    ``state`` is ``'fires'``; ``'waits'``, where the clauses hold but the
    request cannot be made yet, because what it needs may still arrive; or
    ``'skips'``, where a clause does not hold or the request is refused.
    """

    state: Literal['fires', 'waits', 'skips']
    reason: str


def trigger_status(client: Client, rule: Rule, candidate: Candidate) -> TriggerStatus:
    """
    The trigger status of one candidate, a dataset or a completed record: the
    loop's decision, and why.

    The clauses are the loop's: the rule is active, the selector matches, the
    candidate lies after the rule's bound, it is not excluded, and no record
    exists under the rule's label with it as member key, or for a series no
    record of its series that lists it, unless the retry policy names the
    failure of the records that do. A completed record counts as listed only
    by a record that references it, so a new record of the followed rule under
    the same member key is fired on again. A facility adds one clause here,
    that a dataset's catalogue entry does not carry our provenance snapshot,
    which the local application has no catalogue for.

    Where these hold, the member waits while it cannot be made yet. That too is
    a question of the datasets that exist, so a later pass fires on it. A
    request that :func:`apply` refuses, or a dataset whose fields the
    instrument's extractor could not derive, is skipped, with the refusal as
    reason.
    """
    try:
        status, _ = _decide(client, rule, candidate, client.datasets())
    except _REFUSED as e:
        return TriggerStatus(state='skips', reason=str(e))
    return status


_REFUSED = (ValueError, TypeError)
"""
What a refusal of one candidate raises: a value that cannot be used, or two
order values that do not compare, a number and a datetime.
"""


def _decide(
    client: Client, rule: Rule, candidate: Candidate, known: list[Dataset]
) -> tuple[TriggerStatus, Group | None]:
    """
    The trigger status of a candidate among the ``known`` datasets, and the
    group to submit where it fires. A refusal raises.
    """
    status = _clauses(client, rule, candidate)
    if status.state != 'fires':
        return status, None
    group = _apply(client, rule, [candidate], None, known=known)
    if group.waiting:
        return TriggerStatus(
            state='waits', reason='; '.join(group.waiting.values())
        ), None
    return status, group


def _clauses(client: Client, rule: Rule, candidate: Candidate) -> TriggerStatus:
    member = _key(candidate)
    if not rule.active:
        return TriggerStatus(state='skips', reason=f'rule {rule.name} is paused')
    if isinstance(candidate, Dataset) and candidate.error is not None:
        raise ValueError(candidate.error)
    if not rule.selector.selects(candidate):
        return TriggerStatus(state='skips', reason='the selector does not match')
    if not _after(rule, candidate):
        return TriggerStatus(state='skips', reason="before the rule's bound")
    if (reason := rule.exclusions.get(member)) is not None:
        return TriggerStatus(state='skips', reason=f'excluded: {reason}')
    if rule.series is not None:
        member = str(candidate.fields.get(rule.series.key))
    records = client.records(label=rule.name, member_key=member)
    if rule.series is not None or isinstance(candidate, Completed):
        records = [
            r
            for r in records
            if _names(_listed(rule.template, r.request.params), candidate)
        ]
    if not records:
        return TriggerStatus(state='fires', reason='no record under the label yet')
    failure = records[-1].failure
    if failure is None or failure.kind not in rule.retry.kinds:
        return TriggerStatus(
            state='skips', reason=f'{len(records)} record(s) under the label'
        )
    if len(records) >= rule.retry.limit:
        return TriggerStatus(
            state='skips', reason=f'{failure.kind} retried {rule.retry.limit} times'
        )
    return TriggerStatus(state='fires', reason=f'retry after {failure.kind}')


class TriggerLoop:
    """
    Runs rules over the datasets the sources know and the completed records
    of the rules they follow, and nothing else does.

    The loop keeps no memory: every clause of :func:`trigger_status` is a query
    over the records and the sources, so what arrived while the backend was down
    is fired on when it comes back, and a restart is a no-op by construction.
    ``refusals`` is what the last pass was refused and ``waiting`` what it
    could not make yet, logs for a user to read, never read by the loop itself.
    """

    def __init__(self, client: Client, *rules: Rule) -> None:
        self.client = client
        self.rules = list(rules)
        self.refusals: dict[str, str] = {}
        self.waiting: dict[str, str] = {}
        for rule in self.rules:
            self.client.backend.reserve(rule.name, rule.name)

    def run_once(self) -> list[RunRecord]:
        """Fire every rule on every candidate it should; return what was made."""
        self.refusals = {}
        self.waiting = {}
        fired: list[RunRecord] = []
        known = self.client.datasets()
        for rule in self.rules:
            for candidate in _candidates(self.client, rule, known):
                name = f'{rule.name} {_key(candidate)}'
                try:
                    status, group = _decide(self.client, rule, candidate, known)
                    if status.state == 'waits':
                        self.waiting[name] = status.reason
                    if group is not None:
                        fired += self.client.submit_group(group).values()
                except (SubmitError, *_REFUSED) as e:
                    self.refusals[name] = str(e)
        return fired


def batch_table(client: Client, batch: Rule | str) -> pd.DataFrame:
    """
    The table of what was reduced with which values: the batch under a label.

    A query over the records, latest per member key, never a stored table: one
    row per member, the member key as index, and each record's rule version and
    lookup entries as columns. For a series, ``entries`` lists the distinct
    entries its datasets matched, not which dataset matched which, which the
    record's ``origin.entries`` holds. The value columns are the fields that
    differ per member, which are the blanks of the rule's template and every
    field a member pinned. Each shows the value the request was made with,
    whoever supplied it, and ``pinned`` names the fields of the row a person
    pinned. A field holding a model is one column per leaf, ``q.start`` and
    ``q.stop``, and a reference is shown as the reference rather than in its
    stored form. A rule's exclusions are rows without a record.
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
            'entries': ', '.join(dict.fromkeys(origin.entries.values())),
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

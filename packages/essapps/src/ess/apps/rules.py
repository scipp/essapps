# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules: how requests are made from data (D14).

Batch and automatic reduction are one mechanism seen twice: a rule is to a batch
what a template is to a request. A template is a stored, immutable, versioned
partial request; a lookup is an ordered table beside it that supplies fills per
dataset; a rule is a template with a lookup and a selector, applied to datasets.
:func:`apply` is the one operation that makes requests from all three, and the
batch form, the three deliberate operations, and the trigger loop all call it.

Nothing here stores a batch or remembers what was fired on: a batch is the
records under one label, and every clause of the trigger loop's decision is a
query over the records and the dataset sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from fnmatch import fnmatch
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from .backend import SubmitError
from .client import Client
from .records import RunRecord, RunRequest, Submission
from .sources import Dataset
from .spec import Ref, SpecId, WorkflowSpec, data_ref_fields


class Criterion(BaseModel, frozen=True):
    """One condition on one field of a dataset; the three forms a table uses."""

    def matches(self, value: Any) -> bool:
        raise NotImplementedError


class Near(Criterion, frozen=True):
    """A number within ``tolerance`` of ``value``; an exact match by default."""

    value: float
    tolerance: float = Field(default=0.0, ge=0.0)

    def matches(self, value: Any) -> bool:
        return isinstance(value, int | float) and abs(value - self.value) <= (
            self.tolerance
        )


class Like(Criterion, frozen=True):
    """A string matching a glob pattern, ``vanadium*``."""

    pattern: str

    def matches(self, value: Any) -> bool:
        return fnmatch(str(value), self.pattern)


class Between(Criterion, frozen=True):
    """A run number in a range; either end may be left open."""

    low: int | None = None
    high: int | None = None

    def matches(self, value: Any) -> bool:
        if not isinstance(value, int | float):
            return False
        return (self.low is None or value >= self.low) and (
            self.high is None or value <= self.high
        )


Criteria = dict[str, Near | Like | Between]
"""Conditions by dataset field; a dataset satisfies them when it satisfies all."""


def _matches(criteria: Criteria, dataset: Dataset) -> bool:
    fields = dataset.fields
    return all(
        name in fields and criterion.matches(fields[name])
        for name, criterion in criteria.items()
    )


class Template(BaseModel, frozen=True):
    """
    A stored, immutable, versioned partial request.

    ``blanks`` are the fields a use must supply; ``dataset_field`` is the one a
    dataset fills when a rule or :func:`apply` supplies one, which is the sole
    blank unless a template has several.
    """

    name: str
    version: int = 1
    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)
    blanks: tuple[str, ...] = ()
    dataset_field: str | None = None
    derived_from: str | None = None

    @classmethod
    def from_request(
        cls,
        name: str,
        request: RunRequest,
        spec: WorkflowSpec,
        blank: Iterable[str] = (),
    ) -> Template:
        """Save a request as a template with its data-reference fields blank."""
        blanks = tuple(sorted(set(data_ref_fields(spec.params)) | set(blank)))
        params = {k: v for k, v in request.params.items() if k not in blanks}
        return cls(name=name, spec=request.spec, params=params, blanks=blanks)

    @property
    def id(self) -> str:
        return f'{self.name}/v{self.version}'

    def revise(self, **changes: Any) -> Template:
        """A new version by copy; the old one stays."""
        return self.model_copy(
            update={
                'version': self.version + 1,
                'params': self.params | changes,
                'derived_from': self.id,
            }
        )

    def fill(self, **values: Any) -> dict[str, Any]:
        missing = set(self.blanks) - values.keys()
        if missing:
            raise ValueError(f'template {self.name} needs {sorted(missing)}')
        return self.params | values

    def field_for_dataset(self) -> str:
        """The blank a dataset fills."""
        if self.dataset_field is not None:
            return self.dataset_field
        if len(self.blanks) == 1:
            return self.blanks[0]
        raise ValueError(
            f'template {self.name} has blanks {list(self.blanks)}; name the one a '
            'dataset fills in dataset_field'
        )


class LookupEntry(BaseModel, frozen=True):
    """
    One row of a lookup: what it matches, and what it fills.

    An entry with no criteria is the wildcard, which applies to what nothing
    else matched.
    """

    name: str
    match: Criteria = Field(default_factory=dict)
    fills: dict[str, Any] = Field(default_factory=dict)

    @property
    def wildcard(self) -> bool:
        return not self.match


class Lookup(BaseModel, frozen=True):
    """
    Stored, versioned data beside a template: fills per dataset.

    An ordered list of entries matching the fields the dataset source declares
    (D7). A dataset matching more than one entry is a validation error, not a
    choice, and at most one entry is the wildcard.
    """

    name: str
    version: int = 1
    entries: tuple[LookupEntry, ...] = ()

    @model_validator(mode='after')
    def _one_wildcard(self) -> Lookup:
        wildcards = [e.name for e in self.entries if e.wildcard]
        if len(wildcards) > 1:
            raise ValueError(f'lookup {self.name} has wildcard entries {wildcards}')
        return self

    @property
    def id(self) -> str:
        return f'{self.name}/v{self.version}'

    def entry(self, dataset: Dataset) -> LookupEntry | None:
        """The entry that applies, the wildcard, or None."""
        hits = [
            e for e in self.entries if not e.wildcard and _matches(e.match, dataset)
        ]
        if len(hits) > 1:
            raise ValueError(
                f'{dataset.ref} matches lookup entries {[e.name for e in hits]}'
            )
        if hits:
            return hits[0]
        return next((e for e in self.entries if e.wildcard), None)


class Bound(BaseModel, frozen=True):
    """
    A rule's lower bound: the newest dataset the source knew when it was made.

    A run number where the source knows one, the creation time otherwise. An
    unset bound admits everything.
    """

    run: int | None = None
    created: datetime | None = None

    @classmethod
    def newest(cls, datasets: Iterable[Dataset]) -> Bound:
        datasets = list(datasets)
        runs = [d.run for d in datasets if d.run is not None]
        times = [d.created for d in datasets if d.created is not None]
        return cls(run=max(runs, default=None), created=max(times, default=None))

    def passes(self, dataset: Dataset) -> bool:
        """Whether a dataset lies after this bound."""
        if self.run is not None and dataset.run is not None:
            return dataset.run > self.run
        if self.created is not None and dataset.created is not None:
            return dataset.created > self.created
        return True


class Selector(BaseModel, frozen=True):
    """Which datasets a rule applies to: field criteria and a lower bound."""

    match: Criteria = Field(default_factory=dict)
    after: Bound = Field(default_factory=Bound)

    def selects(self, dataset: Dataset) -> bool:
        """Whether the criteria match, leaving the bound to :attr:`after`."""
        return _matches(self.match, dataset)


class RetryPolicy(BaseModel, frozen=True):
    """
    On which failures a failed record is resubmitted, and how often.

    ``limit`` counts the records under the member key, so a limit of two allows
    one retry.
    """

    kinds: tuple[str, ...] = ()
    limit: int = Field(default=2, ge=1)


class Series(BaseModel, frozen=True):
    """
    How a rule combines its members (D15).

    ``key`` is the dataset field whose value keys datasets into a series and is
    the member key of the series' combines; ``finalize`` are the finalize
    parameters of each combine request. The rule's spec must declare a
    contribution: a series over a workflow without one is an opaque combine
    over all members, which D14 allows and this prototype does not build.
    """

    key: str
    finalize: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    """
    Stored, versioned data that makes requests from datasets.

    Everything but ``exclusions`` and ``active`` changes by copy through
    :meth:`revise`, and the records say which version made them; those two are
    mutable state, because they change over a beamtime. A paused rule fires on
    nothing, and on resume the datasets that arrived meanwhile are fired on like
    any other: the rule's promise is that every matching dataset after its bound
    is reduced.
    """

    name: str
    version: int = 1
    template: Template
    lookup: Lookup | None = None
    selector: Selector = Field(default_factory=Selector)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    series: Series | None = None
    exclusions: dict[str, str] = Field(
        default_factory=dict, description="Member keys not to fire on, with a reason."
    )
    active: bool = True

    @classmethod
    def over(cls, datasets: Iterable[Dataset], **fields: Any) -> Rule:
        """A rule bounded at the newest dataset the source knows, as at creation."""
        selector = fields.pop('selector', Selector())
        return cls(
            selector=selector.model_copy(update={'after': Bound.newest(datasets)}),
            **fields,
        )

    @property
    def id(self) -> str:
        return f'{self.name}/v{self.version}'

    def revise(self, **changes: Any) -> Rule:
        """A new version by copy; its records are the reprocess of the old one's."""
        return self.model_copy(update={'version': self.version + 1, **changes})

    def exclude(self, member_key: str, reason: str) -> None:
        """Never fire on this dataset; mutable state, not a new version."""
        self.exclusions[member_key] = reason


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
    chain: dict[str, Ref] = {}
    for key, dataset in members:
        entry = (
            lookup.entry(dataset)
            if lookup is not None and dataset is not None
            else None
        )
        fills = {} if dataset is None else {template.field_for_dataset(): dataset.ref}
        fills |= (entry.fills if entry is not None else {}) | values.get(key, {})
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
    chain: dict[str, Ref],
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
        previous = Ref(record=last.id, output=spec.contribution)
    name = f'{member}+combine'
    chain[value] = Ref(record=f'@{name}', output=spec.contribution)
    return name, client.request(
        spec.id,
        series.finalize,
        label=label,
        member_key=value,
        stage='combine',
        contributions=[
            *([] if previous is None else [previous]),
            Ref(record=f'@{member}', output=spec.contribution),
        ],
        submission=Submission(rule=rule.id),
    )


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


def reprocess(client: Client, rule: Rule) -> dict[str, RunRequest]:
    """
    The members whose latest record under the label came from an older rule version.

    Offered when a rule moves to a new template or lookup version. What the
    submitter typed is carried forward and what the template and lookup filled
    is made again from the new versions.
    """
    stale = [
        record
        for record in client.batch(rule.name)
        if record.request.submission.rule != rule.id
    ]
    return _again(client, rule, stale)


def rerun(
    client: Client, rule: Rule | Template, *, label: str | None = None
) -> dict[str, RunRequest]:
    """The members under a label that have no completed record."""
    label = label or rule.name
    return _again(
        client, rule, client.members_without_completed_record(label), label=label
    )


def _again(
    client: Client,
    rule: Rule | Template,
    records: Iterable[RunRecord],
    *,
    label: str | None = None,
) -> dict[str, RunRequest]:
    """Apply again over the datasets of these records, carrying the typed values."""
    known = {str(dataset.ref): dataset for dataset in client.datasets()}
    excluded = rule.exclusions if isinstance(rule, Rule) else {}
    members = [
        record
        for record in records
        if record.request.member_key in known
        and record.request.member_key not in excluded
        and record.request.stage != 'combine'
    ]
    return apply(
        client,
        rule,
        [known[str(r.request.member_key)] for r in members],
        {str(r.request.member_key): r.request.submission.typed for r in members},
        label=label,
    )


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

    def run_once(self) -> list[RunRecord]:
        """Fire every rule on every dataset it should, and return what was made."""
        self.refusals = {}
        datasets = self.client.datasets()
        fired: list[RunRecord] = []
        for rule in self.rules:
            for dataset in datasets:
                if not trigger_status(self.client, rule, dataset).fires:
                    continue
                group = apply(self.client, rule, [dataset])
                try:
                    fired += self.client.submit_group(group).values()
                except SubmitError as e:
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

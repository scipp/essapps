# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules: the stored data requests are made from.

A rule is to a batch what a template is to a request. A template
(:class:`ess.apps.records.Template`) is a partial request, stored and
versioned here; a lookup is an ordered table beside it that supplies fills
per dataset; a rule is a template with a lookup and a selector, and says
which datasets it applies to.

All of it is versioned by copy, and the records say which version made them.
The exceptions are a rule's exclusions and its active flag, which are mutable
state because they change over a beamtime. Nothing here holds a client; the
operations that make requests from this data are in :mod:`ess.apps.batch`.

See docs/developer/rules.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from fnmatch import fnmatch
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from .records import Plain, Template
from .sources import Dataset


class HasFields(Protocol):
    """
    What a rule fires on: a dataset, or a record of the rule it follows
    (:class:`ess.apps.batch.Followed`). Criteria match its fields, and
    its ``ref`` is what it fills the template's dataset field with.
    """

    @property
    def fields(self) -> dict[str, Any]: ...

    @property
    def ref(self) -> Any: ...


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
"""Conditions by field; a candidate satisfies them when it satisfies all."""


def matches(criteria: Criteria, candidate: HasFields) -> bool:
    fields = candidate.fields
    return all(
        name in fields and criterion.matches(fields[name])
        for name, criterion in criteria.items()
    )


class Nearest(BaseModel, frozen=True):
    """
    A fill resolved per member: the nearest dataset matching ``match``.

    Cans, dark frames, and empty-beam runs are measured repeatedly during an
    experiment; which one belongs to a member depends on the member's own
    dataset, so this is resolved against it in :func:`ess.apps.batch.apply`,
    never stored as a fixed reference. ``same`` names fields whose value the
    match shares with the member, a sample holder or a configuration.
    ``direction`` is where to look in the order of :func:`precedes`: the
    nearest match ``'before'`` the member, ``'after'`` it, or ``'either'``, the
    nearer of those two, a tie going to the one before.

    A member with no match before it is refused. A member with no match after
    it waits, since one may yet be measured, and ``'either'`` waits for the
    match after it too, so that the answer depends only on the datasets that
    exist and not on when it is asked.
    """

    kind: Literal['nearest'] = 'nearest'
    match: Criteria
    same: tuple[str, ...] = ()
    direction: Literal['before', 'after', 'either'] = 'before'


class LookupEntry(BaseModel, frozen=True):
    """
    One row of a lookup: what it matches, and what it fills.

    An entry with no criteria is the wildcard, which applies to what nothing
    else matched. A fill may be a :class:`Nearest` instead of a value. A fill
    named for a column of a list parameter of rows, ``runs.floor``, fills that
    column in the row of the dataset that matched.
    """

    name: str
    match: Criteria = Field(default_factory=dict)
    fills: dict[str, Nearest | Plain] = Field(default_factory=dict)

    @property
    def wildcard(self) -> bool:
        return not self.match


class Lookup(BaseModel, frozen=True):
    """
    Stored, versioned data beside a template: fills per dataset.

    An ordered list of entries matching the fields the dataset source declares.
    A dataset matching more than one entry is a validation error, not a
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

    def entry(self, candidate: HasFields) -> LookupEntry | None:
        """The entry that applies, the wildcard, or None."""
        hits = [
            e for e in self.entries if not e.wildcard and matches(e.match, candidate)
        ]
        if len(hits) > 1:
            raise ValueError(
                f'{candidate.ref} matches lookup entries {[e.name for e in hits]}'
            )
        if hits:
            return hits[0]
        return next((e for e in self.entries if e.wildcard), None)


def _positions(a: Dataset, b: Dataset) -> tuple[Any, Any] | None:
    """Where ``a`` and ``b`` lie, in the terms of :func:`precedes`."""
    pairs = [(a.run, b.run), (a.created, b.created)]
    if a.order is not None and a.order == b.order:
        pairs.insert(0, (a.fields.get(a.order), b.fields.get(b.order)))
    return next(((x, y) for x, y in pairs if x is not None and y is not None), None)


def precedes(a: Dataset, b: Dataset) -> bool:
    """
    Whether ``a`` lies before ``b``, by the first of these both carry: the
    field their instrument's extractor declares to order datasets, the run
    number, the creation time.
    """
    positions = _positions(a, b)
    return positions is not None and bool(positions[0] < positions[1])


def gap(a: Dataset, b: Dataset) -> Any:
    """How far ``b`` lies after ``a``, in the terms :func:`precedes` compares."""
    positions = _positions(a, b)
    if positions is None:
        raise ValueError(f'{a.ref} and {b.ref} carry no common order')
    return positions[1] - positions[0]


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

    def selects(self, candidate: HasFields) -> bool:
        """Whether the criteria match, leaving the bound to :attr:`after`."""
        return matches(self.match, candidate)


class RetryPolicy(BaseModel, frozen=True):
    """
    On which failures a failed record is resubmitted, and how often.

    ``limit`` counts the records under the member key, so a limit of two allows
    one retry.
    """

    kinds: tuple[str, ...] = ()
    limit: int = Field(default=2, ge=1)


class Complete(BaseModel, frozen=True):
    """
    When a series is complete: once it has ``count`` members, or a member of
    each of ``roles``, the values of the members' ``role`` field.
    """

    count: int | None = Field(default=None, ge=1)
    roles: tuple[str, ...] = ()

    @model_validator(mode='after')
    def _one_criterion(self) -> Complete:
        if (self.count is None) == (not self.roles):
            raise ValueError('a series is complete by a count or by roles, not both')
        return self

    def missing(self, members: Iterable[HasFields]) -> str | None:
        """What the members lack to be complete, or None."""
        members = list(members)
        if self.count is not None:
            if len(members) < self.count:
                return f'{len(members)} of {self.count} members'
            return None
        lacking = set(self.roles) - {m.fields.get('role') for m in members}
        return f'no member of role {sorted(lacking)}' if lacking else None


class Series(BaseModel, frozen=True):
    """
    How a rule makes one request over several datasets.

    ``key`` is the dataset field whose value keys datasets into a series, and
    is the member key of the series' requests. The rule submits one request
    whose dataset field lists every current member of the series, in the order
    of :func:`precedes`, so
    successive requests supersede each other, and the result a record stands
    for is read off its request alone. Each member is filled from its own
    lookup entry: into its row where the template's dataset field is a column
    of a list parameter of rows, into the request otherwise. Whether the
    workflow sums the members or stitches them is the workflow's.

    ``fire`` says when: on ``'each'`` arrival, or once the series is
    :class:`Complete` and on each arrival after; until then the series waits.
    """

    key: str
    fire: Literal['each'] | Complete = 'each'


class Follows(BaseModel, frozen=True):
    """
    The candidates of a rule that fires on the completed records of another.

    ``label`` is the other rule's label, and its candidates are the latest
    completed record per member key under it. A candidate has the fields of
    the dataset its member key names, or else, as for a record of a series,
    the fields on which every dataset the record references agrees, the series
    key among them. Selectors, lookups, and series keys therefore work as they
    do on datasets. It fills the template's dataset field with a
    row of references to every output of the record, by output name, which is
    the form a combine spec over the records of a contribute spec takes.
    Where ``output`` names one output, it fills a reference to that output
    instead, and one to each element of a collection output, each its own
    candidate: the second phase of a fan-out whose keys the first found; a
    collection with no element makes no candidate.

    A series never loses a member in silence: while the latest record of one
    of its members under ``label`` has failed or is not done, the series waits
    and names it, until it completes or the rule excludes it.
    """

    label: str
    output: str | None = None


class Rule(BaseModel):
    """
    Stored, versioned data that makes requests from datasets, or from the
    completed records of another rule when it :class:`Follows` one.

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
    follows: Follows | None = None
    exclusions: dict[str, str] = Field(
        default_factory=dict,
        description="Candidates not to fire on or list, with a reason: datasets "
        "by identity, completed records by member key.",
    )
    active: bool = True

    @model_validator(mode='after')
    def _not_itself(self) -> Rule:
        if self.follows is not None and self.follows.label == self.name:
            raise ValueError(f'rule {self.name} would follow its own records')
        return self

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
        """
        Never fire on or list this candidate, a dataset by identity or a followed
        record by member key; mutable state, not a new version.
        """
        self.exclusions[member_key] = reason

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
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .records import Plain, Template
from .sources import Dataset


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


def matches(criteria: Criteria, dataset: Dataset) -> bool:
    fields = dataset.fields
    return all(
        name in fields and criterion.matches(fields[name])
        for name, criterion in criteria.items()
    )


class AsOf(BaseModel, frozen=True):
    """
    A fill resolved per member: the nearest earlier dataset matching ``match``.

    Cans, dark frames, and empty-beam runs are measured repeatedly during an
    experiment; the right one for a member is the one nearest before it, not
    the one latest at submission, so this is resolved against the member's own
    dataset in :func:`ess.apps.batch.apply`, never stored as a fixed reference.
    """

    kind: Literal['as-of'] = 'as-of'
    match: Criteria


class LookupEntry(BaseModel, frozen=True):
    """
    One row of a lookup: what it matches, and what it fills.

    An entry with no criteria is the wildcard, which applies to what nothing
    else matched. A fill may be an :class:`AsOf` instead of a value.
    """

    name: str
    match: Criteria = Field(default_factory=dict)
    fills: dict[str, AsOf | Plain] = Field(default_factory=dict)

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

    def entry(self, dataset: Dataset) -> LookupEntry | None:
        """The entry that applies, the wildcard, or None."""
        hits = [e for e in self.entries if not e.wildcard and matches(e.match, dataset)]
        if len(hits) > 1:
            raise ValueError(
                f'{dataset.ref} matches lookup entries {[e.name for e in hits]}'
            )
        if hits:
            return hits[0]
        return next((e for e in self.entries if e.wildcard), None)


def precedes(a: Dataset, b: Dataset) -> bool:
    """
    Whether ``a`` lies before ``b``: by run number when both carry one, by
    creation time otherwise; the same two forms as :class:`Bound`.
    """
    if a.run is not None and b.run is not None:
        return a.run < b.run
    if a.created is not None and b.created is not None:
        return a.created < b.created
    return False


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
        return matches(self.match, dataset)


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
    How a rule makes one request over several datasets.

    ``key`` is the dataset field whose value keys datasets into a series, and
    is the member key of the series' requests. On each arrival the rule
    submits one request whose dataset field lists every current member of the
    series, so successive requests supersede each other, and the result a
    record stands for is read off its request alone. Whether the workflow sums
    the members or stitches them is the workflow's.
    """

    key: str


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

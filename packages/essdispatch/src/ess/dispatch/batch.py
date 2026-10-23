# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch: one template filled for each of many datasets.

A lookup fills further blanks per dataset, such as the can measured most
recently before a sample. A dataset fills a place in the one blank the
lookup leaves: a file field of the blank, a group, such as
``('sample', 'file')``, since every parameter is a group. Datasets are
ordered by their run number.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ess.spec import DatasetRef

from .client import Datasets
from .datasets import Selector
from .records import Request, Template


def run_number(ref: DatasetRef, source: Datasets) -> int:
    return int(source.metadata(ref)['run'])


@dataclass(frozen=True)
class LastBefore:
    """The matching dataset with the highest run number below the filled one's."""

    selector: Selector

    def find(self, ref: DatasetRef, source: Datasets) -> DatasetRef:
        run = run_number(ref, source)
        earlier = [d for d in source.list(self.selector) if run_number(d, source) < run]
        if not earlier:
            raise LookupError(f'no dataset matching {self.selector} before run {run}')
        return max(earlier, key=lambda d: run_number(d, source))


class Lookup:
    """
    Rules that fill blanks per dataset, each at a field of its blank, a
    group, keyed by that place; ``fields`` gives the other fields of each
    blank, by blank, as :func:`apply` takes them::

        Lookup({('can', 'file'): LastBefore(Selector(role='can'))})

    Raises ``ValueError`` for a place that is not a tuple of a blank and its
    fields, for two places in one blank, and for fields of a blank that the
    lookup does not fill, or that give a place the lookup fills.
    """

    def __init__(
        self,
        rules: Mapping[tuple[str, ...], LastBefore],
        fields: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if bad := [p for p in rules if not isinstance(p, tuple) or len(p) < 2]:
            raise ValueError(
                f'a lookup fills a field of a blank, such as (blank, field), not {bad}'
            )
        blanks = [place[0] for place in rules]
        if len(set(blanks)) != len(blanks):
            raise ValueError(f'a lookup fills one place per blank, not {list(rules)}')
        fields = dict(fields or {})
        if other := sorted(set(fields) - set(blanks)):
            raise ValueError(f'fields of {other}, which the lookup does not fill')
        for place in rules:
            holding(place, None, fields.get(place[0]))  # refuses a clash
        self.rules = dict(rules)
        self.fields = fields

    @property
    def blanks(self) -> set[str]:
        """The blanks the lookup fills."""
        return {place[0] for place in self.rules}

    def fill(self, ref: DatasetRef, source: Datasets) -> dict[str, Any]:
        """Each blank the lookup fills, with the dataset found at its place."""
        return {
            place[0]: holding(place, rule.find(ref, source), self.fields.get(place[0]))
            for place, rule in self.rules.items()
        }


def dataset_place(
    template: Template, lookup: Lookup | None, at: tuple[str, ...]
) -> tuple[str, ...]:
    """
    The place a dataset fills, ``at``: a field of the one blank the lookup
    leaves, at any depth.
    """
    left = [b for b in template.blanks if lookup is None or b not in lookup.blanks]
    if len(left) != 1:
        raise ValueError(f'a dataset fills one blank; {template.blanks} leave {left}')
    if len(at) < 2 or at[0] != left[0]:
        raise ValueError(
            f'a dataset fills a field of the blank {left[0]}, such as '
            f'({left[0]!r}, {"file"!r}), not {at}'
        )
    return at


def holding(
    at: tuple[str, ...], ref: Any, fields: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """
    The value that holds ``ref`` at the place ``at`` below the blank
    ``at[0]``: of the blank, or of a row of it for a series, inside a copy of
    ``fields``, the blank's other fields, at any depth::

        holding(('sample', 'run', 'file'), ref, {'run': {'transmission': None}})
        # {'run': {'transmission': None, 'file': ref}}

    Raises ``ValueError`` if ``fields`` gives the place ``at``, or a value
    that is not a dict on the way to it.
    """
    value = copy.deepcopy(dict(fields or {}))
    inner = value
    for i, field in enumerate(at[1:-1], start=2):
        inner = inner.setdefault(field, {})
        if not isinstance(inner, dict):
            raise ValueError(
                f'{at[:i]} holds the dataset at {at}; fields gives {inner!r} there'
            )
    if at[-1] in inner:
        raise ValueError(f'{at} holds the dataset; fields gives it too')
    inner[at[-1]] = ref
    return value


def apply(
    template: Template,
    datasets: Iterable[DatasetRef],
    source: Datasets,
    *,
    at: tuple[str, ...],
    fields: Mapping[str, Any] | None = None,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> dict[str, Request]:
    """
    Fill the template for each dataset.

    ``source`` answers metadata queries, such as ``client.datasets``. Each
    dataset fills the place ``at`` in the one blank the lookup leaves, a
    group: ``at`` names its file field. ``fields`` gives the group's other
    fields, the same for each dataset, and a field left out takes its
    default; a field without a default, such as a correction that a request
    writes as ``None`` to leave it out, is given so::

        template = Template(IOFQ, blanks=('sample',))
        apply(template, runs, client.datasets, at=('sample', 'file'),
              fields={'transmission': None})
        # {'101': Request(IOFQ, params={'sample': {'file': dataset(run=101),
        #                                          'transmission': None}}),
        #  ...}

    The requests are keyed by member: the value of the metadata field
    ``member_field`` of each dataset. Submitting them under a label makes the
    keys the members of their records. Raises ``LookupError`` if the request
    of a dataset cannot be made.
    """
    requests, failed = try_apply(
        template,
        datasets,
        source,
        at=at,
        fields=fields,
        member_field=member_field,
        lookup=lookup,
    )
    if failed:
        raise next(iter(failed.values()))
    return requests


def try_apply(
    template: Template,
    datasets: Iterable[DatasetRef],
    source: Datasets,
    *,
    at: tuple[str, ...],
    fields: Mapping[str, Any] | None = None,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> tuple[dict[str, Request], dict[DatasetRef, LookupError]]:
    """
    As :func:`apply`, but leave out each dataset whose request cannot be made.

    A request cannot be made if the lookup finds nothing for the dataset, or
    its metadata lacks ``member_field``. Returns the requests, and the error of
    each dataset left out.
    """
    place = dataset_place(template, lookup, at)
    requests: dict[str, Request] = {}
    failed: dict[DatasetRef, LookupError] = {}
    for ref in datasets:
        try:
            member = str(source.metadata(ref)[member_field])
            found = lookup.fill(ref, source) if lookup else {}
        except LookupError as error:
            failed[ref] = error
            continue
        if member in requests:
            raise ValueError(f'two datasets have {member_field} {member}')
        requests[member] = template.fill(
            params={place[0]: holding(place, ref, fields), **found}
        )
    return requests, failed

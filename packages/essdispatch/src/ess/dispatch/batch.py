# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch: one template filled for each of many datasets.

A lookup fills further blanks per dataset, such as the can measured most
recently before a sample. Datasets are ordered by their run number.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    """Rules that fill blanks per dataset, by blank name."""

    def __init__(self, **rules: LastBefore) -> None:
        self.rules = rules

    def fill(self, ref: DatasetRef, source: Datasets) -> dict[str, Any]:
        return {blank: rule.find(ref, source) for blank, rule in self.rules.items()}


def dataset_blank(template: Template, lookup: Lookup | None) -> str:
    """The one blank a dataset fills: the one the lookup leaves."""
    left = [b for b in template.blanks if lookup is None or b not in lookup.rules]
    if len(left) != 1:
        raise ValueError(f'a dataset fills one blank; {template.blanks} leave {left}')
    return left[0]


def apply(
    template: Template,
    datasets: Iterable[DatasetRef],
    source: Datasets,
    *,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> dict[str, Request]:
    """
    Fill the template for each dataset.

    ``source`` answers metadata queries, such as ``client.datasets``. Each
    dataset fills the one blank the lookup leaves. The requests are keyed
    by member: the value of the metadata field ``member_field`` of each
    dataset. Submitting them under a label makes the keys the members of their
    records. Raises ``LookupError`` if the request of a dataset cannot be made.
    """
    requests, failed = try_apply(
        template, datasets, source, member_field=member_field, lookup=lookup
    )
    if failed:
        raise next(iter(failed.values()))
    return requests


def try_apply(
    template: Template,
    datasets: Iterable[DatasetRef],
    source: Datasets,
    *,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> tuple[dict[str, Request], dict[DatasetRef, LookupError]]:
    """
    As :func:`apply`, but leave out each dataset whose request cannot be made.

    A request cannot be made if the lookup finds nothing for the dataset, or
    its metadata lacks ``member_field``. Returns the requests, and the error of
    each dataset left out.
    """
    blank = dataset_blank(template, lookup)
    requests: dict[str, Request] = {}
    failed: dict[DatasetRef, LookupError] = {}
    for ref in datasets:
        try:
            member = str(source.metadata(ref)[member_field])
            values = {blank: ref, **(lookup.fill(ref, source) if lookup else {})}
        except LookupError as error:
            failed[ref] = error
            continue
        if member in requests:
            raise ValueError(f'two datasets have {member_field} {member}')
        requests[member] = template.fill(values)
    return requests, failed

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Naming datasets, and the dataset source that knows them.

A person names a dataset by what they know: a run number, a path, or a PID.
The backend asks its dataset source to resolve such a name to the dataset's
identity when a request is submitted, so a record names the dataset itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from ess.reduce.spec import DatasetRef


def dataset(
    *, run: int | None = None, path: Path | str | None = None, pid: str | None = None
) -> DatasetRef:
    """A reference to a dataset by its run number, its path, or its PID."""
    given = {'run': run, 'path': path, 'pid': pid}
    named = {kind: value for kind, value in given.items() if value is not None}
    if len(named) != 1:
        raise ValueError(f'name a dataset by exactly one of {list(given)}')
    ((kind, value),) = named.items()
    return DatasetRef(dataset=f'{kind}:{value}')


class Selector:
    """
    Which datasets to take: those whose metadata has the given values.

    A dataset has a kind, such as raw, derived, mask, or calibration; a
    selector matches raw datasets unless it names another kind.
    """

    def __init__(self, kind: str = 'raw', **fields: Any) -> None:
        self._fields = tuple(sorted({'kind': kind, **fields}.items()))

    @property
    def fields(self) -> dict[str, Any]:
        return dict(self._fields)

    def matches(self, metadata: dict[str, Any]) -> bool:
        metadata = {'kind': 'raw', **metadata}
        return all(metadata.get(k) == v for k, v in self._fields)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Selector) and self._fields == other._fields

    def __hash__(self) -> int:
        return hash(self._fields)

    def __repr__(self) -> str:
        return f'Selector({self.fields})'


def readable(metadata: dict[str, Any], proposal: str) -> bool:
    """Whether requests of ``proposal`` may read a dataset with this metadata."""
    return metadata.get('proposal', proposal) == proposal


class DatasetSource(Protocol):
    """
    Which datasets exist, and what they hold.

    A backend has one. It resolves names and reads datasets through it, and
    answers through it the queries of its clients: listing datasets, waiting
    for new ones, and reading their metadata. A client sees only the datasets
    its proposal may read.
    """

    def resolve(self, name: DatasetRef) -> DatasetRef:
        """The identity of the named dataset; raises ``KeyError`` if unknown."""
        ...

    def read(self, ref: DatasetRef) -> Any:
        """The dataset's content, in the form workflows take."""
        ...

    def metadata(self, ref: DatasetRef) -> dict[str, Any]:
        """
        The dataset's current metadata.

        The field ``proposal`` names the proposal whose requests may read the
        dataset; a dataset without it, such as published data, is open to all.
        """
        ...

    def list(self, selector: Selector) -> list[DatasetRef]:
        """The datasets the selector matches, in the order they were measured."""
        ...

    def watch(self, selector: Selector) -> Iterator[DatasetRef]:
        """Matching datasets: the existing ones first, then new ones, each once."""
        ...

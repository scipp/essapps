# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Naming datasets, and the dataset source that knows them.

A person names a dataset by what they know: a run number, a path, or a PID.
The backend asks its dataset source to resolve such a name to the dataset's
identity when a request is submitted, so a record names the dataset itself.
"""

from __future__ import annotations

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


class DatasetSource(Protocol):
    """What the backend needs to know about datasets."""

    def resolve(self, name: DatasetRef) -> DatasetRef:
        """The identity of the named dataset; raises ``KeyError`` if unknown."""
        ...

    def read(self, ref: DatasetRef) -> Any:
        """The dataset's content, in the form workflows take."""
        ...

    def metadata(self, ref: DatasetRef) -> dict[str, Any]:
        """The dataset's current metadata."""
        ...

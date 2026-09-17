# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Dataset sources: where data the framework did not compute comes from (D7).

A source yields the datasets of a proposal and locates the bytes of a dataset
reference. It persists nothing: which datasets a rule has already fired on is a
query over the records, and a dataset enters the store only as a reference in
the requests that name it. SciCat is the implementation for a facility,
:class:`FolderSource` the one for the local application, and
:class:`ess.apps.testing.FakeDatasetSource` the fake for tests.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .spec import DatasetRef


@dataclass(frozen=True)
class Dataset:
    """
    What a source yields: where the bytes are now, what identifies them, metadata.

    A source fills the identity fields it can: ``pid`` for a catalogue dataset,
    ``instrument`` and ``run`` for a file that carries a run identity, neither
    for a file that carries none, whose path is then its identity. ``metadata``
    is what the source declares for the instrument, an angle, a sample name, a
    run's role; ``created`` is when the acquisition wrote the dataset, which is
    the other form a rule's lower bound takes.
    """

    path: Path
    pid: str | None = None
    instrument: str | None = None
    run: int | None = None
    created: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> dict[str, Any]:
        """
        What a lookup entry or a selector may match on (D7, D14).

        The metadata the source declares for the instrument, plus the run
        number, which every source that knows one declares under ``run``.
        """
        return ({'run': self.run} if self.run is not None else {}) | self.metadata

    @property
    def ref(self) -> DatasetRef:
        """This dataset's identity, which is how a request names it."""
        if self.pid is not None:
            return DatasetRef(pid=self.pid)
        if self.run is not None:
            return DatasetRef(instrument=self.instrument, run=self.run)
        return DatasetRef(path=self.path)


class DatasetSource(Protocol):
    def new_datasets(self, proposal: str) -> Iterable[Dataset]:
        """Datasets of this proposal; arrival may be out of order and repeated."""
        ...

    def locate(self, ref: DatasetRef) -> Path | None:
        """Where the bytes are now, or None if this source does not have them."""
        ...


_RUN_IDENTITY = re.compile(r'(?P<instrument>[a-zA-Z]+)_(?P<run>\d+)')


class FolderSource:
    """
    The local application's dataset source: the files of one folder.

    A file whose name carries an instrument and a run number, ``dream_4711.nxs``,
    is identified by those, which is what a PID is minted from; any other file is
    identified by its path. The folder is one proposal's, so ``proposal`` selects
    nothing here.

    Facilities name files differently, so ``identity`` is the expression matched
    against a file's stem: a group ``run`` gives the run number and a group
    ``instrument`` the instrument, which ``instrument`` supplies for names that
    do not carry one. A stem the expression does not match is identified by its
    path.
    """

    def __init__(
        self,
        path: Path | str,
        pattern: str = '*',
        *,
        identity: str | re.Pattern[str] = _RUN_IDENTITY,
        instrument: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.pattern = pattern
        self.identity = re.compile(identity)
        self.instrument = instrument

    def new_datasets(self, proposal: str) -> list[Dataset]:
        return self._scan()

    def locate(self, ref: DatasetRef) -> Path | None:
        return next((d.path for d in self._scan() if d.ref == ref), None)

    def _scan(self) -> list[Dataset]:
        return [
            self._dataset(p)
            for p in sorted(self.path.glob(self.pattern))
            if p.is_file()
        ]

    def _dataset(self, path: Path) -> Dataset:
        match = self.identity.fullmatch(path.stem)
        if match is None:
            return Dataset(path=path, created=self._created(path))
        instrument = match.groupdict().get('instrument') or self.instrument
        if instrument is None:
            raise ValueError(
                f'{path.name} carries a run number but no instrument; give the '
                'source an instrument name'
            )
        return Dataset(
            path=path,
            instrument=instrument.lower(),
            run=int(match['run']),
            created=self._created(path),
        )

    @staticmethod
    def _created(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)

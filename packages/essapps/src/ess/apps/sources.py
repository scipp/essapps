# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Dataset sources: where data the framework did not compute comes from.

A source yields the datasets of a proposal and locates the bytes of a dataset
reference. It persists nothing: which datasets a rule has already fired on is a
query over the records, and a dataset enters the store only as a reference in
the requests that name it. SciCat is the implementation for a facility,
:class:`FolderSource` the one for the local application, and
:class:`ess.apps.testing.FakeDatasetSource` the fake for tests.

See docs/developer/rules.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

from .spec import DatasetRef, dataset_ref


@dataclass(frozen=True)
class Dataset:
    """
    What a source yields: where the bytes are now, what identifies them, metadata.

    A source fills the identity fields it can: ``pid`` for a catalogue dataset,
    ``instrument`` and ``run`` for a file that carries a run identity, neither
    for a file that carries none, whose path is then its identity. ``metadata``
    is what the instrument's :class:`FieldExtractor` derived, an angle, a sample
    name, a run's role, and ``order`` the one of those fields that orders the
    instrument's datasets, as the extractor declares; ``created`` is when the
    acquisition wrote the dataset, which is the other form a rule's lower bound
    takes.
    """

    path: Path
    pid: str | None = None
    instrument: str | None = None
    run: int | None = None
    created: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    order: str | None = None

    @property
    def fields(self) -> dict[str, Any]:
        """
        What a lookup entry or a selector may match on.

        The fields the instrument's extractor derived, plus the run number,
        which every source that knows one declares under ``run``.
        """
        return ({'run': self.run} if self.run is not None else {}) | self.metadata

    @property
    def ref(self) -> DatasetRef:
        """This dataset's identity, which is how a request names it."""
        if self.pid is not None:
            return dataset_ref(pid=self.pid)
        if self.run is not None:
            return dataset_ref(instrument=self.instrument, run=self.run)
        return dataset_ref(path=self.path)


FIELDS_GROUP = 'ess.apps.fields'


@dataclass(frozen=True)
class FieldExtractor:
    """
    An instrument's code that derives the fields of its datasets.

    ``derive`` is given what a source has for a dataset, its catalogue entry or
    journal row (empty when there is none) and its file, and returns the fields
    lookups, selectors, and series keys match on. Where a role, a sample, or an
    angle is written differs between instruments, a title prefix, a NeXus
    field, a variable of the acquisition script, so this is code the instrument
    team owns, and nothing is required of acquisition. ``order`` names the
    field that orders the instrument's datasets, typically the start time of
    the acquisition; without one, datasets are ordered by run number.

    An installed package registers one per instrument under the entry-point
    group ``ess.apps.fields``, named for the instrument, as it registers specs.
    """

    derive: Callable[[Mapping[str, Any], Path], Mapping[str, Any]]
    order: str | None = None

    def dataset(self, entry: Mapping[str, Any], path: Path, **identity: Any) -> Dataset:
        """The dataset with this identity, its fields derived from ``entry``."""
        return Dataset(
            path=path,
            metadata=dict(self.derive(entry, path)),
            order=self.order,
            **identity,
        )


def _entry_as_is(entry: Mapping[str, Any], path: Path) -> Mapping[str, Any]:
    return entry


AS_IS = FieldExtractor(_entry_as_is)
"""The extractor of an instrument that registers none: the entry's own fields."""


@cache
def field_extractor(instrument: str | None) -> FieldExtractor:
    """The extractor an installed package registers for an instrument, or AS_IS."""
    found = entry_points(group=FIELDS_GROUP, name=instrument or '')
    return next((ep.load() for ep in found), AS_IS)


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

    ``journal`` is what the facility's run journal or catalogue says about
    each run, an entry by run number. The instrument's field extractor derives
    each dataset's fields from its entry and its file; ``fields`` gives one in
    place of the one an installed package registers for the instrument.
    """

    def __init__(
        self,
        path: Path | str,
        pattern: str = '*',
        *,
        identity: str | re.Pattern[str] = _RUN_IDENTITY,
        instrument: str | None = None,
        journal: Mapping[int, Mapping[str, Any]] | None = None,
        fields: FieldExtractor | None = None,
    ) -> None:
        self.path = Path(path)
        self.pattern = pattern
        self.identity = re.compile(identity)
        self.instrument = instrument
        self.journal = journal or {}
        self.fields = fields

    def __repr__(self) -> str:
        return f'FolderSource({str(self.path)!r}, {self.pattern!r})'

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
        created = self._created(path)
        match = self.identity.fullmatch(path.stem)
        if match is None:
            return self._extractor(self.instrument).dataset({}, path, created=created)
        instrument = match.groupdict().get('instrument') or self.instrument
        if instrument is None:
            raise ValueError(
                f'{path.name} carries a run number but no instrument; give the '
                'source an instrument name'
            )
        run = int(match['run'])
        return self._extractor(instrument).dataset(
            self.journal.get(run, {}),
            path,
            instrument=instrument.lower(),
            run=run,
            created=created,
        )

    def _extractor(self, instrument: str | None) -> FieldExtractor:
        if self.fields is not None:
            return self.fields
        return field_extractor(None if instrument is None else instrument.lower())

    @staticmethod
    def _created(path: Path) -> datetime:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Dataset sources: where data the framework did not compute comes from.

A source yields the datasets of proposals, finds the dataset a reference names,
and locates its bytes. It persists nothing: which datasets a rule has already
fired on is a query over the records, and a dataset enters the store only as a
reference in the requests that name it. SciCat is the implementation for a facility,
:class:`FolderSource` the one for the local application, and
:class:`ess.apps.testing.FakeDatasetSource` the fake for tests.

See docs/developer/rules.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

import h5py

from .runner import FileMemo, file_checksum
from .spec import IDENTITIES, DatasetRef, dataset_ref


@dataclass(frozen=True)
class Dataset:
    """
    What a source yields: where the bytes are now, what identifies them, metadata.

    A source fills the identity fields it can. A reference by any of them names
    the dataset, and the first of ``pid``, ``uuid``, ``sha256`` is the one a new
    request is written with, see :func:`ess.apps.spec.dataset_ref`.
    ``proposals`` are the proposals the dataset belongs to, which decide who may
    read it; a catalogue may list several. ``instrument`` and ``run`` are the
    instrument and run number the dataset carries, fields like any other, which
    a person may type to name it. ``metadata`` is what the instrument's
    :class:`FieldExtractor` derived, an angle, a sample name, a run's role, and
    ``order`` the one of those fields that orders the instrument's datasets, as
    the extractor declares, which two datasets must share for it to order them;
    ``created`` is when the acquisition wrote the dataset, which is the other
    form a rule's lower bound takes. ``error`` says why the extractor gave no
    usable fields, and a rule refuses such a dataset, visibly, rather than the
    source failing for every dataset.
    """

    path: Path
    proposals: list[str]
    pid: str | None = None
    uuid: str | None = None
    sha256: str | None = None
    instrument: str | None = None
    run: int | None = None
    created: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    order: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.pid is None and self.uuid is None and self.sha256 is None:
            raise ValueError(f'{self.path}: a dataset needs a pid, uuid, or sha256')

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
        """The identity a new request names this dataset by, its first identity."""
        return self.identities[0]

    @property
    def identities(self) -> list[DatasetRef]:
        """
        Every identity this dataset has, in order of preference.

        A reference by any of them names the dataset: a PID minted after a
        request named the dataset by its UUID leaves the record locatable.
        """
        return [
            dataset_ref(**{kind: value})
            for kind in IDENTITIES
            if (value := getattr(self, kind)) is not None
        ]


def merge(datasets: Iterable[Dataset]) -> list[Dataset]:
    """
    One dataset for each set of datasets that share an identity.

    Two sources may know one file, a catalogue by its PID and UUID and a folder
    by its UUID. The first of datasets that share an identity stands for all of
    them, with the identities the others add and the union of their proposals.
    """
    merged: list[Dataset] = []
    at: dict[DatasetRef, int] = {}
    for dataset in datasets:
        i = next((at[r] for r in dataset.identities if r in at), None)
        if i is None:
            i = len(merged)
            merged.append(dataset)
        else:
            first = merged[i]
            merged[i] = replace(
                first,
                **{k: getattr(first, k) or getattr(dataset, k) for k in IDENTITIES},
                proposals=list(dict.fromkeys([*first.proposals, *dataset.proposals])),
            )
        at |= dict.fromkeys(merged[i].identities, i)
    return merged


def find(datasets: Iterable[Dataset], ref: DatasetRef) -> Dataset | None:
    """
    The dataset ``ref`` names among ``datasets``, or None.

    ``ref`` is any identity of the dataset, or a stand-in a person typed,
    ``run:<instrument>/<run>`` or ``path:<path>``, where the instrument is
    matched regardless of case and the run number regardless of leading zeros.
    A stand-in that fits datasets of several identities names none of them,
    and raises.
    """
    kind, _, value = ref.dataset.partition(':')
    if kind == 'run':
        instrument, _, run = value.rpartition('/')
        hits = [
            d
            for d in datasets
            if (d.instrument or '').lower() == instrument.lower()
            and run.isdigit()
            and d.run == int(run)
        ]
    elif kind == 'path':
        path = Path(value).resolve()
        hits = [d for d in datasets if d.path.resolve() == path]
    else:
        hits = [d for d in datasets if ref in d.identities]
    found = merge(hits)
    if len(found) > 1:
        raise ValueError(f'{ref} names several datasets: {[str(d.ref) for d in found]}')
    return next(iter(found), None)


UUID_FIELD = 'entry/entry_identifier_uuid'


def file_identity(path: Path) -> dict[str, str]:
    """
    The identity fields of a local file: the UUID a NeXus file carries in
    ``entry/entry_identifier_uuid``, else the sha256 of its bytes.

    A UUID field that holds no single non-empty string is no UUID. Raises
    ``OSError`` for an HDF5 file that cannot be opened, truncated or locked
    by the writer.
    """
    if h5py.is_hdf5(path):
        with h5py.File(path, 'r') as file:
            uuid = file.get(UUID_FIELD)
            if (
                isinstance(uuid, h5py.Dataset)
                and uuid.shape == ()
                and h5py.check_string_dtype(uuid.dtype) is not None
                and (value := uuid.asstr()[()])
            ):
                return {'uuid': value}
    return {'sha256': file_checksum(path)}


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
    the acquisition, and its value must be a number or a :class:`datetime`;
    without one, datasets are ordered by run number.

    The framework reads one field by name: ``role``, which
    :class:`ess.apps.rules.Complete` counts a series' roles by.

    An installed package registers one per instrument under the entry-point
    group ``ess.apps.fields``, named for the instrument, as it registers specs.
    """

    derive: Callable[[Mapping[str, Any], Path], Mapping[str, Any]]
    order: str | None = None

    def dataset(self, entry: Mapping[str, Any], path: Path, **given: Any) -> Dataset:
        """
        The dataset with the fields ``given``, its identity among them, and
        the fields derived from ``entry``, or with the reason as its ``error``
        where they cannot be.
        """
        dataset = Dataset(path=path, order=self.order, **given)
        try:
            metadata = dict(self.derive(entry, path))
        # Instrument code: whatever it raises concerns this dataset only.
        except Exception as e:
            return replace(dataset, error=f'{dataset.ref}: no fields derived: {e!r}')
        dataset = replace(dataset, metadata=metadata)
        value = metadata.get(self.order) if self.order is not None else 0
        if isinstance(value, bool) or not isinstance(value, int | float | datetime):
            return replace(
                dataset,
                error=f'{dataset.ref}: its order field {self.order!r} is '
                f'{value!r}, not a number or a datetime',
            )
        return dataset


def _entry_as_is(entry: Mapping[str, Any], path: Path) -> Mapping[str, Any]:
    return entry


AS_IS = FieldExtractor(_entry_as_is)
"""The extractor of an instrument that registers none: the entry's own fields."""


@cache
def field_extractor(instrument: str | None) -> FieldExtractor:
    """The extractor an installed package registers for an instrument, or AS_IS."""
    found = entry_points(group=FIELDS_GROUP, name=instrument or '')
    extractor = next((ep.load() for ep in found), AS_IS)
    if not isinstance(extractor, FieldExtractor):
        raise TypeError(
            f'{FIELDS_GROUP} entry point {instrument!r} is {extractor!r}, '
            'not a FieldExtractor'
        )
    return extractor


class DatasetSource(Protocol):
    def datasets(self, proposals: Collection[str]) -> Iterable[Dataset]:
        """
        Datasets that belong to any of these proposals; arrival may be out of
        order and repeated.
        """
        ...

    def find(self, ref: DatasetRef) -> Dataset | None:
        """
        The dataset an identity or a stand-in names, see :func:`find`, whether
        or not its bytes have landed; None if this source does not know it.
        """
        ...

    def locate(self, ref: DatasetRef) -> Path | None:
        """Where the bytes are now, or None if this source does not have them."""
        ...


_RUN_NAME = re.compile(r'(?P<instrument>[a-zA-Z]+)_(?P<run>\d+)')


class FolderSource:
    """
    The local application's dataset source: the files of one folder, which
    belong to one proposal.

    A file is identified by the UUID it carries, or else by the sha256 of its
    bytes, which is read once per file and kept while the file is unchanged:
    the first scan of a folder reads every file it matches, which for tens of
    gigabytes on a network mount takes minutes. A file that is still being
    written is not identified yet: one the writer holds open, one that changes
    while it is read, and one modified less than ``settle`` seconds ago, which
    is how a copy in progress that pauses is told apart. Two files that carry
    one UUID are not identified either, since a request could not say which
    it means. ``skipped`` says why each file the last scan passed over was.
    A file whose name carries an instrument and a run number,
    ``dream_4711.nxs``, has those as fields, so that a person can name it by
    them. Facilities name files differently, so ``stem`` is the expression
    matched against a file's stem: a group ``run`` gives the run number and a
    group ``instrument`` the instrument, which ``instrument`` supplies for
    names that do not carry one.

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
        proposal: str,
        stem: str | re.Pattern[str] = _RUN_NAME,
        instrument: str | None = None,
        journal: Mapping[int, Mapping[str, Any]] | None = None,
        fields: FieldExtractor | None = None,
        settle: float = 0.0,
    ) -> None:
        self.path = Path(path)
        self.pattern = pattern
        self.proposal = proposal
        self.stem = re.compile(stem)
        self.instrument = instrument
        self.journal = journal or {}
        self.fields = fields
        self.settle = settle
        self.skipped: dict[Path, str] = {}
        self._identity = FileMemo(file_identity)

    def __repr__(self) -> str:
        return f'FolderSource({str(self.path)!r}, {self.pattern!r})'

    def datasets(self, proposals: Collection[str]) -> list[Dataset]:
        return self._scan() if self.proposal in proposals else []

    def find(self, ref: DatasetRef) -> Dataset | None:
        return find(self._scan(), ref)

    def locate(self, ref: DatasetRef) -> Path | None:
        dataset = self.find(ref)
        return None if dataset is None else dataset.path

    def _scan(self) -> list[Dataset]:
        paths = [p for p in sorted(self.path.glob(self.pattern)) if p.is_file()]
        self._identity.keep(paths)
        self.skipped = {}
        identified: dict[Path, dict[str, Any]] = {}
        for path in paths:
            if isinstance(identity := self._identified(path), str):
                self.skipped[path] = identity
            else:
                identified[path] = identity
        by_uuid: dict[str, list[Path]] = {}
        for path, identity in identified.items():
            if 'uuid' in identity:
                by_uuid.setdefault(identity['uuid'], []).append(path)
        for uuid, shared in by_uuid.items():
            if len(shared) > 1:
                for path in shared:
                    del identified[path]
                    self.skipped[path] = f'{len(shared)} files carry uuid {uuid}'
        return [self._dataset(path, identity) for path, identity in identified.items()]

    def _identified(self, path: Path) -> dict[str, Any] | str:
        """The identity fields of a file and when it was written, or why not."""
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if (datetime.now(UTC) - modified).total_seconds() < self.settle:
                return f'not identified: modified less than {self.settle} s ago'
            return {**self._identity(path), 'created': modified}
        except OSError as e:
            return f'not identified: {e}'

    def _dataset(self, path: Path, identity: dict[str, Any]) -> Dataset:
        identity = {**identity, 'proposals': [self.proposal]}
        match = self.stem.fullmatch(path.stem)
        if match is None:
            return self._extractor(self.instrument).dataset({}, path, **identity)
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
            **identity,
        )

    def _extractor(self, instrument: str | None) -> FieldExtractor:
        if self.fields is not None:
            return self.fields
        return field_extractor(None if instrument is None else instrument.lower())

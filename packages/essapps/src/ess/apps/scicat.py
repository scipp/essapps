# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
SciCat as a file source and as the place where results are published.

It needs the extra ``scicat`` (scitacean).
"""

from __future__ import annotations

import importlib.metadata
import logging
import math
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import httpx
    from scitacean import PID, Dataset, RemotePath
    from scitacean import Client as ScicatClient
    from scitacean.model import DownloadDataset
except ModuleNotFoundError as error:
    raise ImportError(
        'ess.apps.scicat needs scitacean: install essapps[scicat]'
    ) from error

from ess.spec import FileRef

from .client import Client
from .files import Selector
from .records import Record
from .writers import DatasetMetadata, Person

_PID_PREFIX = 'pid:'


@dataclass(frozen=True, kw_only=True)
class Catalogue:
    """
    Where to publish.

    Parameters
    ----------
    client:
        The SciCat client, with the file transfer that uploads the files.
    instrument:
        The instrument of the workspace, such as ``'loki'``.
    folder:
        The folder on the file server for the files of a dataset, as a pattern
        of ``proposal``, ``instrument`` and ``record``. The default is that of
        ESS. A file transfer with a ``source_folder`` of its own puts the files
        there instead, so that the datasets of one proposal would share a
        folder. Give the client a transfer without one.
    """

    client: ScicatClient
    instrument: str
    folder: str = '/ess/data/{proposal}/{instrument}/upload/{record}'


def principal_investigator(catalogue: Catalogue, proposal: str) -> Person | None:
    """
    The principal investigator of ``proposal``, as the catalogue states.

    The ESS ingestor makes this person the owner and contact of each raw
    dataset. Returns ``None`` and logs a warning if the catalogue cannot tell,
    such as without a login or when it cannot be reached.
    """
    try:
        found = catalogue.client.get_proposal(proposal)
    except Exception as error:  # httpx raises its own errors, such as unreachable
        logging.getLogger(__name__).warning(
            'The principal investigator of proposal %s is not known: %s',
            proposal,
            error,
        )
        return None
    name = ' '.join(n for n in (found.pi_firstname, found.pi_lastname) if n)
    return Person(name=name, email=found.pi_email or '')


def installed_software() -> list[str]:
    """The installed Python distributions as ``name==version``, sorted."""
    found = {
        d.name.lower(): d.version
        for d in importlib.metadata.distributions()
        if d.name  # a dist-info folder left without its metadata has none
    }
    return [f'{name}=={version}' for name, version in sorted(found.items())]


def publish(
    catalogue: Catalogue,
    client: Client,
    record: Record,
    files: Sequence[Path],
    metadata: DatasetMetadata,
    format: str,
) -> str:
    """
    Uploads ``files`` as a new derived dataset of ``record``, and returns its identity.

    The identity is ``pid:<pid>``, as for a file (ADR 0010). The input datasets
    are the files with a catalogue identifier that the record read, also
    through upstream records. The used software is that of this process, since
    a record holds no software versions.

    ``format`` is the name of the writer of ``files``. The dataset stores it,
    since files of two formats may have the same names, such as ``iofq.h5``.
    Raises what the SciCat client raises, such as ``pydantic.ValidationError``.
    """
    provenance = client.provenance(record)
    identities = [f.file for f in provenance.files()]
    inputs = [i.removeprefix(_PID_PREFIX) for i in identities if _is_pid(i)]
    dataset = Dataset(
        type='derived',
        name=metadata.name,
        description=metadata.description or None,
        keywords=list(metadata.keywords),
        owner=metadata.owner,
        contact_email=metadata.contact_email,
        owner_group=record.proposal,
        proposal_ids=[record.proposal],
        instrument_ids=[catalogue.instrument],
        creation_location=f'ESS:{catalogue.instrument.upper()}',
        creation_time=datetime.now(tz=UTC),
        input_datasets=[PID.parse(i) for i in inputs],
        used_software=installed_software(),
        source_folder=RemotePath(
            catalogue.folder.format(
                proposal=record.proposal,
                instrument=catalogue.instrument,
                record=record.id,
            )
        ),
        meta={
            'spec': str(record.spec),
            'record': record.id,
            'format': format,
            'request': record.request.model_dump(mode='json', by_alias=True),
            'other_inputs': [i for i in identities if not _is_pid(i)],
        },
    )
    dataset.add_local_files(*files)
    finalized = catalogue.client.upload_new_dataset_now(dataset)
    return f'{_PID_PREFIX}{finalized.pid}'


def _is_pid(identity: str) -> bool:
    return identity.startswith(_PID_PREFIX)


_NEXUS_SUFFIXES = frozenset({'.hdf', '.h5', '.nxs'})


@dataclass(frozen=True)
class _Listing:
    """The raw datasets of a proposal as one query found them, at ``time``."""

    datasets: dict[FileRef, DownloadDataset]
    """By identity, in the order measured."""
    names: dict[FileRef, FileRef]
    """The identity of the file of each name: itself, and ``run:<run>``."""
    time: float
    """When the query was answered, by ``time.monotonic``."""


@dataclass(frozen=True)
class _Failure:
    """Why a question to the catalogue failed, and when."""

    reason: str
    time: float


class ScicatFiles:
    """
    A file source over the raw datasets of ``proposal`` in SciCat, one file each.

    The catalogue is read with the login of ``client``::

        ScicatFiles(scitacean.Client.login('staging.ess'), '336349')

    A file is identified by the PID of its dataset, ``pid:20.500.12269/<uuid>``,
    and named by it or by its run, ``run:385``. Its metadata come from the
    dataset's record in the catalogue::

        kind      'raw'
        run       runNumber, as text, as in files.csv
        pid       the PID
        proposal  the proposal
        sample    sample_name of the scientific metadata, if given
        title     datasetName, without the end that the ingestor adds,
                  ' - Proposal:<proposal> - Run: <run>'

    The catalogue does not state what a run is for, so no file has a ``role``.
    :class:`~ess.batch.CorrectedFiles` lets a person add one.

    :meth:`list` queries the catalogue on each call, so that it finds a run
    ingested since. :meth:`resolve` and :meth:`metadata` use the last listing,
    and query again for a file not in it. :meth:`path` queries the file list of
    a dataset once, and returns the path that the ingestor recorded, such as
    ``/ess/raw/loki/<proposal>/raw/<file>.hdf``. Nothing is downloaded. A
    workflow can read the file only on a machine where that path is mounted.

    A page asks for the files of all its rows every second. So a query that
    failed is not repeated for ``max_age`` seconds: for a name that the listing
    lacks, for the file list of a dataset without one NeXus file, and for any
    query after one that was not answered, such as after a timeout.

    Raises ``OSError`` if the catalogue cannot be reached, rejects a query, or
    the login expired. :meth:`path` also raises it if the dataset does not list
    exactly one NeXus file.
    """

    def __init__(
        self, client: ScicatClient, proposal: str, *, max_age: float = 10.0
    ) -> None:
        self._client = client
        self._proposal = proposal
        self._max_age = max_age
        self._listing = _Listing(datasets={}, names={}, time=-math.inf)
        self._paths: dict[FileRef, Path] = {}
        self._failed: dict[FileRef, _Failure] = {}
        self._unanswered: _Failure | None = None

    def resolve(self, name: FileRef) -> FileRef:
        return self._lookup(name, lambda listing: listing.names)

    def path(self, ref: FileRef) -> Path:
        self._lookup(ref, lambda listing: listing.datasets)
        if (path := self._paths.get(ref)) is not None:
            return path
        if (failed := self._failed.get(ref)) is not None and self._recent(failed.time):
            raise OSError(failed.reason)
        pid = PID.parse(ref.file.removeprefix(_PID_PREFIX))
        try:
            path = _nexus_file(
                self._ask(
                    lambda: self._client.scicat.get_dataset_model(pid, datablocks=True)
                )
            )
        except (OSError, ValueError) as error:
            self._failed[ref] = _Failure(str(error), time.monotonic())
            raise OSError(str(error)) from None
        self._failed.pop(ref, None)
        self._paths[ref] = path
        return path

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return self._metadata(self._lookup(ref, lambda listing: listing.datasets))

    def list(self, selector: Selector) -> list[FileRef]:
        datasets = self._read().datasets
        return [r for r, d in datasets.items() if selector.matches(self._metadata(d))]

    def _lookup[T](
        self, name: FileRef, table: Callable[[_Listing], Mapping[FileRef, T]]
    ) -> T:
        """
        What ``table`` holds of ``name``, asking the catalogue again if the
        listing lacks it and is older than ``max_age``.
        """
        found = table(self._listing).get(name)
        if found is None and not self._recent(self._listing.time):
            found = table(self._read()).get(name)
        if found is None:
            raise KeyError(
                f'proposal {self._proposal} has no raw dataset of the file {name.file}'
            )
        return found

    def _read(self) -> _Listing:
        """Queries the catalogue for the raw datasets, and holds them as the listing."""
        fields = {'type': 'raw', 'proposalIds': [self._proposal]}
        found = self._ask(lambda: self._client.scicat.query_datasets(fields))
        found.sort(key=_measured)
        datasets = {FileRef(file=f'{_PID_PREFIX}{d.pid}'): d for d in found}
        runs: dict[FileRef, list[FileRef]] = {}
        for ref, dataset in datasets.items():
            if dataset.runNumber is not None:
                run = FileRef(file=f'run:{dataset.runNumber}')
                runs.setdefault(run, []).append(ref)
        names = {ref: ref for ref in datasets}
        for run, refs in runs.items():
            if len(refs) == 1:
                names[run] = refs[0]
            else:
                logging.getLogger(__name__).warning(
                    'Proposal %s has %d raw datasets of %s, so it names none: %s',
                    self._proposal,
                    len(refs),
                    run.file,
                    ', '.join(r.file for r in refs),
                )
        self._listing = _Listing(datasets=datasets, names=names, time=time.monotonic())
        return self._listing

    def _metadata(self, dataset: DownloadDataset) -> dict[str, Any]:
        sample = (dataset.scientificMetadata or {}).get('sample_name')
        found = {
            'kind': 'raw',
            'run': dataset.runNumber,
            'pid': str(dataset.pid),
            'proposal': self._proposal,
            'sample': sample.get('value') if isinstance(sample, dict) else sample,
            'title': self._title(dataset),
        }
        return {k: v for k, v in found.items() if v is not None}

    def _title(self, dataset: DownloadDataset) -> str | None:
        """
        The name of ``dataset`` without the end the ingestor adds, which holds
        what the metadata hold anyway.
        """
        added = f' - Proposal:{self._proposal} - Run: {dataset.runNumber}'
        name = dataset.datasetName
        return None if name is None else name.removesuffix(added)

    def _ask[T](self, query: Callable[[], T]) -> T:
        """
        The answer to ``query``.

        Raises ``OSError`` if it is not answered, and then for ``max_age``
        without asking.
        """
        if (failed := self._unanswered) is not None and self._recent(failed.time):
            raise OSError(failed.reason)
        try:
            answer = query()
        # scitacean raises RuntimeError once the login expired, and its own
        # errors, such as ScicatCommError, derive from it.
        except (RuntimeError, httpx.HTTPError) as error:
            self._unanswered = _Failure(
                f'SciCat did not answer: {error}', time.monotonic()
            )
            raise OSError(self._unanswered.reason) from error
        self._unanswered = None
        return answer

    def _recent(self, then: float) -> bool:
        return time.monotonic() - then < self._max_age


def _nexus_file(dataset: DownloadDataset) -> Path:
    """
    The path of the one NeXus file in the file list of ``dataset``.

    Raises ``ValueError`` if the list holds none or several.
    """
    listed = [
        PurePosixPath(f.path)
        for block in dataset.origdatablocks or []
        for f in block.dataFileList or []
    ]
    nexus = [p for p in listed if p.suffix in _NEXUS_SUFFIXES]
    if len(nexus) != 1:
        raise ValueError(
            f'the dataset {dataset.pid} lists {len(nexus)} NeXus files, not one: '
            f'{[str(p) for p in listed]}'
        )
    folder = PurePosixPath(dataset.sourceFolder.posix if dataset.sourceFolder else '/')
    return Path(folder / nexus[0])  # a path that is absolute stays as it is


def _measured(dataset: DownloadDataset) -> tuple[bool, datetime, str]:
    """The order of measurement: by start time, then datasets without one by PID."""
    start = dataset.startTime or datetime.min.replace(tzinfo=UTC)
    return (dataset.startTime is None, start, str(dataset.pid))


def scan_catalogue(files: ScicatFiles) -> list[dict[str, Any]]:
    """
    The rows of ``files.csv`` for the raw files that ``files`` lists.

    ``file`` is the path that the ingestor recorded, and the rest is the
    metadata. A folder with this table identifies each file by its ``pid``, as
    ``files`` does. The table opens only where the paths are mounted, since
    :class:`~ess.apps.FolderFiles` rejects a file that does not exist.

    A dataset whose path cannot be found is left out with a warning. Raises
    ``OSError`` if the catalogue does not answer the listing.
    """
    rows = []
    for ref in files.list(Selector()):
        try:
            path = files.path(ref)
        except OSError as error:
            warnings.warn(f'{ref.file} is left out: {error}', stacklevel=2)
            continue
        rows.append({'file': str(path), **files.metadata(ref)})
    return rows

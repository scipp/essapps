# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A SciCat in memory for tests of :class:`~ess.apps.scicat.ScicatFiles`. It needs
the extra ``scicat``.

scitacean's ``FakeClient`` holds datasets but answers no queries, so
:class:`FakeCatalogue` extends it to answer ``query_datasets``::

    catalogue = FakeCatalogue()
    ref = catalogue.add(385, minutes=0, sample='water')
    ScicatFiles(catalogue, '336349').resolve(FileRef(file='run:385')) == ref
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scitacean import PID, Profile, model
from scitacean.testing.client import FakeClient, FakeScicatClient

from ess.spec import FileRef

PROPOSAL = '336349'
"""The proposal of the datasets that :meth:`FakeCatalogue.add` makes by default."""
START = datetime(2026, 11, 25, 10, tzinfo=UTC)
"""The start of the first run, from which :meth:`FakeCatalogue.add` counts."""


class _QueryingScicat(FakeScicatClient):
    """scitacean's fake, answering ``query_datasets`` by equal fields."""

    main: FakeCatalogue

    def query_datasets(
        self, fields: dict[str, Any], **kwargs: Any
    ) -> list[model.DownloadDataset]:
        self.main.asked.append('datasets')
        if 'query_datasets' in self.main.disabled:
            raise self.main.disabled['query_datasets']
        return [
            d.model_copy(deep=True)
            for d in self.main.datasets.values()
            if all(getattr(d, k) == v for k, v in fields.items())
        ]

    def get_dataset_model(
        self, pid: PID, *args: Any, **kwargs: Any
    ) -> model.DownloadDataset:
        self.main.asked.append(f'files of {pid}')
        return super().get_dataset_model(pid, *args, **kwargs)


class FakeCatalogue(FakeClient):
    """
    A SciCat in memory that answers queries, and lists those it was asked in
    :attr:`asked`: ``'datasets'`` for a query, ``'files of <pid>'`` for the
    file list of a dataset. Like ``FakeClient``, a name in :attr:`disabled`
    makes that method raise the error given.
    """

    def __init__(self) -> None:
        super().__init__(
            profile=Profile(url='https://scicat.invalid/api/v3', file_transfer=None)
        )
        self.asked: list[str] = []

    @property
    def scicat(self) -> _QueryingScicat:
        return _QueryingScicat(self, url='https://scicat.invalid/api/v3')

    def add(
        self,
        run: int | None,
        *,
        minutes: int | None = None,
        proposal: str = PROPOSAL,
        files: tuple[str, ...] = ('loki_{run}.hdf',),
        sample: str | None = None,
        name: str = 'title {run} - Proposal:{proposal} - Run: {run}',
    ) -> FileRef:
        """
        A raw dataset of ``run``, started ``minutes`` after :data:`START`,
        listing ``files`` (``{run}`` is the run), named as the ingestor names
        it unless ``name`` is given; returns the identity of its file.
        """
        pid = PID(prefix='20.500.12269', pid=f'{proposal}-{run}-{len(self.datasets)}')
        self.datasets[pid] = model.DownloadDataset(
            pid=pid,
            type='raw',
            proposalIds=[proposal],
            runNumber=None if run is None else str(run),
            datasetName=name.format(run=run, proposal=proposal),
            startTime=None if minutes is None else START + timedelta(minutes=minutes),
            sourceFolder=f'/ess/data/loki/{proposal}',
            scientificMetadata=(
                {} if sample is None else {'sample_name': {'value': sample}}
            ),
        )
        self.orig_datablocks[pid] = [
            model.DownloadOrigDatablock(
                datasetId=pid,
                dataFileList=[
                    model.DownloadDataFile(path=f.format(run=run), size=1, time=START)
                    for f in files
                ],
            )
        ]
        return FileRef(file=f'pid:{pid}')

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scitacean import PID, Profile, ScicatCommError, model
from scitacean.testing.client import FakeClient, FakeScicatClient

from ess.apps.files import Selector
from ess.apps.scicat import ScicatFiles
from ess.spec import FileRef

PROPOSAL = '336349'
START = datetime(2026, 11, 25, 10, tzinfo=UTC)


class QueryingScicat(FakeScicatClient):
    """scitacean's fake, answering ``query_datasets`` by equal fields."""

    def query_datasets(
        self, fields: dict[str, Any], **kwargs: Any
    ) -> list[model.DownloadDataset]:
        if 'query_datasets' in self.main.disabled:
            raise self.main.disabled['query_datasets']
        return [
            d.model_copy(deep=True)
            for d in self.main.datasets.values()
            if all(getattr(d, k) == v for k, v in fields.items())
        ]


class Catalogue(FakeClient):
    """A SciCat in memory that answers queries."""

    @property
    def scicat(self) -> QueryingScicat:
        return QueryingScicat(self, url='https://scicat.invalid/api/v3')

    def add(
        self,
        run: int | None,
        *,
        minutes: int | None = None,
        proposal: str = PROPOSAL,
        files: tuple[str, ...] = ('loki_{run}.hdf',),
        sample: str | None = None,
    ) -> FileRef:
        """A raw dataset of ``run``, started ``minutes`` after ``START``."""
        pid = PID(prefix='20.500.12269', pid=f'{proposal}-{run}-{len(self.datasets)}')
        self.datasets[pid] = model.DownloadDataset(
            pid=pid,
            type='raw',
            proposalIds=[proposal],
            runNumber=None if run is None else str(run),
            datasetName=f'run {run}',
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


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue(
        profile=Profile(url='https://scicat.invalid/api/v3', file_transfer=None)
    )


@pytest.fixture
def files(catalogue: Catalogue) -> ScicatFiles:
    return ScicatFiles(catalogue, PROPOSAL)


def run(number: int) -> FileRef:
    return FileRef(file=f'run:{number}')


def test_lists_raw_datasets_of_the_proposal_in_the_order_measured(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    later = catalogue.add(2, minutes=10)
    earlier = catalogue.add(1, minutes=0)
    undated = catalogue.add(3)
    catalogue.add(4, minutes=5, proposal='other')

    assert files.list(Selector()) == [earlier, later, undated]


def test_metadata_come_from_the_record(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    ref = catalogue.add(385, minutes=0, sample='water')

    assert files.metadata(ref) == {
        'kind': 'raw',
        'run': '385',
        'pid': ref.file.removeprefix('pid:'),
        'proposal': PROPOSAL,
        'sample': 'water',
        'name': 'run 385',
    }
    assert files.list(Selector(sample='water')) == [ref]
    assert files.list(Selector(kind='mask')) == []


def test_resolves_a_run_and_an_identity(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    ref = catalogue.add(385, minutes=0)

    assert files.resolve(run(385)) == ref
    assert files.resolve(ref) == ref


def test_finds_a_run_ingested_after_the_last_listing(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    catalogue.add(1, minutes=0)
    files.list(Selector())
    ref = catalogue.add(2, minutes=1)

    assert files.resolve(run(2)) == ref


def test_a_run_of_two_datasets_names_neither(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    first = catalogue.add(1, minutes=0)
    catalogue.add(1, minutes=1)

    with pytest.raises(KeyError):
        files.resolve(run(1))
    assert files.resolve(first) == first


def test_refuses_an_unknown_file(files: ScicatFiles) -> None:
    unknown = FileRef(file='pid:20.500.12269/nope')
    with pytest.raises(KeyError):
        files.resolve(run(1))
    with pytest.raises(KeyError):
        files.metadata(unknown)
    with pytest.raises(KeyError):
        files.path(unknown)


def test_path_is_where_the_ingestor_recorded_the_file(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    absolute = catalogue.add(1, files=('/ess/raw/loki/336349/raw/loki_{run}.hdf',))
    relative = catalogue.add(2, files=('raw/loki_{run}.nxs', 'run.log'))

    assert files.path(absolute) == Path('/ess/raw/loki/336349/raw/loki_1.hdf')
    assert files.path(relative) == Path('/ess/data/loki/336349/raw/loki_2.nxs')


def test_path_refuses_a_dataset_without_one_nexus_file(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    ref = catalogue.add(1, files=('a.hdf', 'b.hdf'))

    with pytest.raises(OSError, match='2 NeXus files'):
        files.path(ref)


def test_a_catalogue_that_does_not_answer_raises_oserror(
    catalogue: Catalogue,
) -> None:
    catalogue.disabled['query_datasets'] = ScicatCommError('timed out')

    with pytest.raises(OSError, match='timed out'):
        ScicatFiles(catalogue, PROPOSAL).list(Selector())

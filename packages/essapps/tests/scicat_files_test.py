# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest
from scitacean import ScicatCommError

from ess.apps.files import Selector
from ess.apps.scicat import ScicatFiles, scan_catalogue
from ess.apps.scicat_testing import PROPOSAL
from ess.apps.scicat_testing import FakeCatalogue as Catalogue
from ess.spec import FileRef


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue()


@pytest.fixture
def files(catalogue: Catalogue) -> ScicatFiles:
    """A file source that asks again at each call: ``max_age`` 0."""
    return ScicatFiles(catalogue, PROPOSAL, max_age=0)


@pytest.fixture
def patient(catalogue: Catalogue) -> ScicatFiles:
    """A file source that asks nothing again within a test: ``max_age`` 60 s."""
    return ScicatFiles(catalogue, PROPOSAL, max_age=60)


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
        'title': 'title 385',
    }
    assert files.list(Selector(sample='water')) == [ref]
    assert files.list(Selector(kind='mask')) == []


def test_a_name_of_another_form_than_the_ingestors_stays_whole(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    ref = catalogue.add(385, name='title 385 - Proposal:other - Run: 385')

    assert files.metadata(ref)['title'] == 'title 385 - Proposal:other - Run: 385'


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


def test_within_max_age_names_not_listed_are_not_asked_for(
    catalogue: Catalogue, patient: ScicatFiles
) -> None:
    catalogue.add(1, minutes=0)
    patient.list(Selector())
    for _ in range(3):  # polls of a page with rows of two runs not listed
        for missing in (run(8), run(9)):
            with pytest.raises(KeyError):
                patient.resolve(missing)

    assert catalogue.asked == ['datasets']


def test_within_max_age_a_dataset_without_one_nexus_file_is_asked_for_once(
    catalogue: Catalogue, patient: ScicatFiles
) -> None:
    ref = catalogue.add(1, minutes=0, files=('run.log',))
    for _ in range(3):
        with pytest.raises(OSError, match='0 NeXus files'):
            patient.path(ref)

    assert catalogue.asked == ['datasets', f'files of {ref.file[4:]}']


def test_within_max_age_a_catalogue_that_did_not_answer_is_not_asked(
    catalogue: Catalogue, patient: ScicatFiles
) -> None:
    catalogue.disabled['query_datasets'] = ScicatCommError('timed out')
    for _ in range(3):
        with pytest.raises(OSError, match='timed out'):
            patient.list(Selector())

    assert catalogue.asked == ['datasets']


def test_an_expired_login_raises_oserror(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    # scitacean raises RuntimeError once the token expired
    catalogue.disabled['query_datasets'] = RuntimeError('SciCat login has expired.')

    with pytest.raises(OSError, match='login has expired'):
        files.list(Selector())


def test_a_file_list_that_the_catalogue_did_not_give_is_asked_for_again(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    ref = catalogue.add(1, minutes=0, files=('/ess/raw/run.hdf',))
    catalogue.disabled['get_dataset_model'] = ScicatCommError('timed out')
    with pytest.raises(OSError, match='timed out'):
        files.path(ref)
    del catalogue.disabled['get_dataset_model']

    assert files.path(ref) == Path('/ess/raw/run.hdf')


def test_scan_catalogue_gives_a_row_of_files_csv_per_raw_file(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    first = catalogue.add(1, minutes=0, files=('/ess/raw/run-1.hdf',), sample='water')
    second = catalogue.add(2, minutes=1, files=('/ess/raw/run-2.hdf',))

    assert scan_catalogue(files) == [
        {
            'file': '/ess/raw/run-1.hdf',
            'kind': 'raw',
            'run': '1',
            'pid': first.file.removeprefix('pid:'),
            'proposal': PROPOSAL,
            'sample': 'water',
            'title': 'title 1',
        },
        {
            'file': '/ess/raw/run-2.hdf',
            'kind': 'raw',
            'run': '2',
            'pid': second.file.removeprefix('pid:'),
            'proposal': PROPOSAL,
            'title': 'title 2',
        },
    ]


def test_scan_catalogue_leaves_out_a_dataset_without_a_path_with_a_warning(
    catalogue: Catalogue, files: ScicatFiles
) -> None:
    catalogue.add(1, minutes=0, files=('run.log',))
    catalogue.add(2, minutes=1)

    with pytest.warns(UserWarning, match='is left out: .* 0 NeXus files'):
        rows = scan_catalogue(files)

    assert [r['run'] for r in rows] == ['2']

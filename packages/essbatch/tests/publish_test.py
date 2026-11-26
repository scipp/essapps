# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Publishing results of a batch to a fake SciCat."""

import pytest
from ess.batch import Batch, BatchManager, FolderDefinitions
from ess.batch.definition import OutputOptions, published
from ess.batch.publish import Publisher, UploadFailed
from ess.batch.writing import Writing
from scitacean import model
from scitacean.testing.client import FakeClient
from scitacean.testing.transfer import FakeFileTransfer

from ess.apps import Client
from ess.apps.scicat import Catalogue, principal_investigator
from ess.apps.testing import FakeFiles
from ess.apps.writers import Person

from . import toys

FORM = OutputOptions(
    format='scipp-hdf5',
    outputs=('total',),
    owner='Anna',
    contact_email='anna@example.org',
)


@pytest.fixture
def scicat() -> FakeClient:
    """A catalogue that lists the proposal of the workspace, p1."""
    scicat = FakeClient.without_login(
        url='https://scicat.example.org/api/v3', file_transfer=FakeFileTransfer()
    )
    scicat.proposals['p1'] = model.DownloadProposal(
        proposalId='p1',
        title='Micelles',
        email='office@example.org',
        ownerGroup='p1',
        pi_firstname='Ada',
        pi_lastname='Lovelace',
        pi_email='ada@example.org',
    )
    return scicat


@pytest.fixture
def catalogue(scicat: FakeClient) -> Catalogue:
    return Catalogue(client=scicat, instrument='loki')


@pytest.fixture
def publisher(manager: BatchManager, catalogue: Catalogue) -> Publisher:
    return Publisher(Writing(manager, {}), catalogue)


@pytest.fixture
def batch(publisher: Publisher, done_batch: Batch) -> Batch:
    """The batch of ``done_batch``, with :data:`FORM`."""
    publisher.writing.set_options(done_batch.label, FORM)
    return done_batch


def test_a_row_is_published_as_a_derived_dataset_of_its_inputs(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    pids = publisher.publish(batch.label, ['1'])

    [pid] = scicat.datasets
    assert pids == {'1': f'pid:{pid}'}
    dataset = scicat.get_dataset(pid)
    assert dataset.type == 'derived'
    assert dataset.name == 'Morning water'
    assert [str(p) for p in dataset.input_datasets] == ['20.500/s1']
    assert dataset.proposal_ids == ['p1']
    assert dataset.owner_group == 'p1'
    assert dataset.meta['other_inputs'] == ['uuid:run-1001']
    assert dataset.meta['format'] == 'scipp-hdf5'
    assert any(s.startswith('scitacean==') for s in dataset.used_software)
    assert sorted(
        f.remote_access_path(dataset.source_folder).name for f in dataset.files
    ) == ['_record.json', 'total.h5']


def test_what_is_published_is_kept_with_the_batch(
    publisher: Publisher, batch: Batch, client: Client, definitions: FolderDefinitions
) -> None:
    pids = publisher.publish(batch.label, ['1'])

    again = BatchManager(client, definitions=definitions)

    record = batch.state(['1'])['1'].record
    assert record is not None
    assert published(again.definitions, batch.label) == {record.id: pids['1']}


def test_a_result_is_published_once(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    publisher.publish(batch.label, ['1'])

    with pytest.raises(ValueError, match='published already: 1'):
        publisher.publish(batch.label, ['2', '1'])
    assert len(scicat.datasets) == 1


def test_output_options_without_owner_or_contact_are_refused_before_any_upload(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    publisher.writing.set_options(
        batch.label, FORM.model_copy(update={'owner': '', 'contact_email': ''})
    )

    with pytest.raises(ValueError, match='no owner, no contact email'):
        publisher.publish(batch.label, ['1'])
    assert scicat.datasets == {}


def test_a_row_without_a_result_is_refused(
    publisher: Publisher, batch: Batch, fake: FakeFiles, client: Client
) -> None:
    fake.measure(3, 3.0, role='sample', sample='B')
    fake.measure(1003, 1.0, role='transmission', sample='B')
    batch.set_table(toys.rows(client))

    with pytest.raises(ValueError, match='not outdated: 3'):
        publisher.publish(batch.label, ['1', '3'])


def test_an_outdated_result_is_refused(publisher: Publisher, batch: Batch) -> None:
    toys.edit(batch, '1', thickness=2.0)

    with pytest.raises(ValueError, match='not outdated: 1'):
        publisher.publish(batch.label, ['1'])


def test_a_row_the_batch_lacks_is_refused(publisher: Publisher, batch: Batch) -> None:
    with pytest.raises(ValueError, match='the batch has no row 9'):
        publisher.publish(batch.label, ['9'])


def test_the_principal_investigator_is_the_one_the_catalogue_names(
    catalogue: Catalogue,
) -> None:
    assert principal_investigator(catalogue, 'p1') == Person(
        name='Ada Lovelace', email='ada@example.org'
    )


@pytest.mark.parametrize('unknown', ['proposal', 'catalogue'])
def test_a_pi_the_catalogue_cannot_name_is_none(
    catalogue: Catalogue, scicat: FakeClient, unknown: str
) -> None:
    if unknown == 'proposal':
        del scicat.proposals['p1']
    else:  # as one that cannot be reached
        scicat.scicat.main.disabled['get_proposal_model'] = OSError('unreachable')

    assert principal_investigator(catalogue, 'p1') is None


def test_removing_the_batch_forgets_what_was_published(
    publisher: Publisher, batch: Batch, manager: BatchManager
) -> None:
    publisher.publish(batch.label, ['1'])

    manager.remove(batch.label)

    assert manager.definitions.labels() == []


def test_an_upload_that_fails_names_and_keeps_the_rows_published_before_it(
    publisher: Publisher,
    batch: Batch,
    scicat: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = scicat.scicat.create_dataset_model

    def second_fails(dataset: object) -> object:
        if scicat.datasets:
            raise OSError('SciCat is down')
        return created(dataset)

    monkeypatch.setattr(scicat.scicat, 'create_dataset_model', second_fails)

    with pytest.raises(UploadFailed, match=r'published 1 as pid:.*; then 2 failed'):
        publisher.publish(batch.label, ['1', '2'])

    kept = published(publisher.writing.manager.definitions, batch.label)
    assert list(kept.values()) == [f'pid:{next(iter(scicat.datasets))}']

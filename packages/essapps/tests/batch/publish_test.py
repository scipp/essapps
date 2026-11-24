# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Publishing results of a batch to a fake SciCat."""

import pytest
from scitacean import model
from scitacean.testing.client import FakeClient
from scitacean.testing.transfer import FakeFileTransfer

from ess.apps import Client
from ess.apps.batch import Batch, BatchManager, FolderDefinitions
from ess.apps.batch.definition import Form
from ess.apps.batch.publish import Publisher
from ess.apps.scicat import Catalogue
from ess.apps.testing import FakeFiles

from . import toys

FORM = Form(outputs=('total',), owner='Anna', contact_email='anna@example.org')


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
def publisher(manager: BatchManager, scicat: FakeClient) -> Publisher:
    return Publisher(manager, Catalogue(client=scicat, instrument='loki'))


@pytest.fixture
def batch(fake: FakeFiles, client: Client, manager: BatchManager) -> Batch:
    """A batch of two rows, water (1) and sample A (2), both reduced."""
    for run, name in [(1, 'water'), (2, 'sample A')]:
        fake.measure(run, float(run), role='sample', sample=name, pid=f'20.500/s{run}')
        fake.measure(run + 1000, 1.0, role='transmission', sample=name)
    batch = manager.create(
        'plain', title='Morning', description='2 mm cells', rows=toys.rows(client)
    )
    client.wait(list(batch.reduce(['1', '2']).values()))
    return batch


def test_a_row_is_published_as_a_derived_dataset_of_its_inputs(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    publisher.set_form(batch.label, FORM)

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
    assert any(s.startswith('scitacean==') for s in dataset.used_software)
    assert sorted(
        f.remote_access_path(dataset.source_folder).name for f in dataset.files
    ) == ['_record.json', 'total.h5']


def test_what_is_published_is_kept_with_the_batch(
    publisher: Publisher, batch: Batch, client: Client, definitions: FolderDefinitions
) -> None:
    publisher.set_form(batch.label, FORM)
    pids = publisher.publish(batch.label, ['1'])

    again = Publisher(
        BatchManager(client, definitions=definitions), publisher.catalogue
    )

    assert again.form(batch.label) == FORM
    record = batch.state(['1'])['1'].record
    assert record is not None
    assert again.published(batch.label) == {record.id: pids['1']}


def test_a_result_is_published_once(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    publisher.set_form(batch.label, FORM)
    publisher.publish(batch.label, ['1'])

    with pytest.raises(ValueError, match='published already: 1'):
        publisher.publish(batch.label, ['2', '1'])
    assert len(scicat.datasets) == 1


def test_a_form_without_owner_or_output_is_refused_before_any_upload(
    publisher: Publisher, batch: Batch, scicat: FakeClient
) -> None:
    publisher.set_form(batch.label, Form(contact_email='anna@example.org'))

    with pytest.raises(ValueError, match='no output, no owner'):
        publisher.publish(batch.label, ['1'])
    assert scicat.datasets == {}


def test_a_row_without_a_result_is_refused(
    publisher: Publisher, batch: Batch, fake: FakeFiles, client: Client
) -> None:
    fake.measure(3, 3.0, role='sample', sample='B')
    fake.measure(1003, 1.0, role='transmission', sample='B')
    batch.put(toys.rows(client))
    publisher.set_form(batch.label, FORM)

    with pytest.raises(ValueError, match='not outdated: 3'):
        publisher.publish(batch.label, ['1', '3'])


def test_an_outdated_result_is_refused(publisher: Publisher, batch: Batch) -> None:
    toys.edit(batch, '1', thickness=2.0)
    publisher.set_form(batch.label, FORM)

    with pytest.raises(ValueError, match='not outdated: 1'):
        publisher.publish(batch.label, ['1'])


def test_a_row_the_batch_lacks_is_refused(publisher: Publisher, batch: Batch) -> None:
    publisher.set_form(batch.label, FORM)

    with pytest.raises(ValueError, match='the batch has no row 9'):
        publisher.publish(batch.label, ['9'])


def test_until_set_the_form_has_the_description_of_the_batch_and_the_pi_as_owner(
    publisher: Publisher, batch: Batch
) -> None:
    assert publisher.form(batch.label) == Form(
        description='2 mm cells', owner='Ada Lovelace', contact_email='ada@example.org'
    )


@pytest.mark.parametrize('unknown', ['proposal', 'catalogue'])
def test_a_pi_the_catalogue_cannot_name_leaves_owner_and_contact_empty(
    manager: BatchManager, batch: Batch, scicat: FakeClient, unknown: str
) -> None:
    if unknown == 'proposal':
        del scicat.proposals['p1']
    else:  # as one that cannot be reached
        scicat.scicat.main.disabled['get_proposal_model'] = OSError('unreachable')

    publisher = Publisher(manager, Catalogue(client=scicat, instrument='loki'))

    assert publisher.form(batch.label) == Form(description='2 mm cells')


def test_a_form_of_another_output_is_refused(
    publisher: Publisher, batch: Batch
) -> None:
    before = publisher.form(batch.label)

    with pytest.raises(ValueError, match='plain has no output iofq'):
        publisher.set_form(batch.label, Form(outputs=('iofq',)))
    assert publisher.form(batch.label) == before


@pytest.mark.parametrize(
    ('name', 'match'),
    [
        ('{title} {sample}', 'not {sample}'),
        ('{title:>99}', 'not {title}'),
        ('{title!r}', 'not {title}'),
        ('{title.upper}', 'not {title.upper}'),
        ('{title', 'do not match'),
        (' ', 'not empty'),
    ],
)
def test_a_name_with_another_field_a_format_or_nothing_is_refused(
    name: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        Form(name=name)


def test_removing_the_batch_forgets_what_was_published(
    publisher: Publisher, batch: Batch, manager: BatchManager
) -> None:
    publisher.set_form(batch.label, FORM)
    publisher.publish(batch.label, ['1'])

    manager.remove(batch.label)

    assert manager.definitions.labels() == []

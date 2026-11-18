# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batches of a workspace, made, kept, changed and removed by a manager."""

import re
from collections.abc import Callable
from contextlib import closing

import pytest

from ess.apps import Client, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    Defaults,
    FolderDefinitions,
    Header,
    Row,
)
from ess.apps.batch.definition import settings_key, template_of
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import COLUMNS, FIND, PLAIN, SOURCES, STAGED, Measure, pair


def _again(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """A manager made again on ``definitions``, as after a restart."""
    return BatchManager(client, sources=SOURCES, definitions=definitions)


def test_a_batch_made_is_listed_with_its_header(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create(
        'plain', title='Morning', source='pairs', description='2 mm cells', label='b'
    )

    header = Header('Morning', '2 mm cells', 'pairs')
    assert label == 'b'
    assert manager.batches() == {'b': header}
    assert _again(client, definitions).batches() == {'b': header}


def test_a_batch_made_keeps_its_spec_and_columns_before_any_settings_are_applied(
    manager: BatchManager, definitions: FolderDefinitions
) -> None:
    manager.create('plain', title='Morning', source='pairs', columns=('thickness',))

    (label,) = manager.batches()
    template = definitions.load(settings_key(label), template_of)
    assert template is not None
    assert template.spec.name == 'plain'
    assert template.blanks == ('sample', 'transmission', 'thickness')


def test_a_manager_made_again_on_the_definitions_hands_out_the_batch_as_it_was(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    water = sample(1, 'water')
    label = manager.create(
        'staged', title='Morning', source='pairs', columns=('thickness',), label='b'
    )
    batch = manager.batch(label)
    batch.settings = {'binning': {'bins': 3}}
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    batch.put([Row('mine', {'sample': water})])

    again = _again(client, definitions).batch(label)

    assert again.spec == STAGED
    assert again.columns == ('sample', 'transmission', 'thickness')
    assert again.settings == batch.settings
    assert list(again.rows) == list(batch.rows)


def test_the_columns_of_a_batch_are_those_of_its_source_and_those_the_person_adds(
    manager: BatchManager,
) -> None:
    label = manager.create(
        'plain', title='Morning', source='pairs', columns=('thickness', 'sample')
    )

    assert manager.batch(label).columns == ('sample', 'transmission', 'thickness')


def test_a_batch_starts_with_the_settings_registered_for_its_spec_but_its_columns(
    client: Client, definitions: FolderDefinitions
) -> None:
    settings = {'thickness': 2.0, 'binning': {'bins': 3}}
    manager = BatchManager(
        client,
        sources=SOURCES,
        definitions=definitions,
        defaults={'plain': Defaults(settings=settings)},
    )

    label = manager.create('plain', title='Morning', columns=COLUMNS)

    assert manager.batch(label).settings == {'binning': {'bins': 3}}


def test_a_batch_is_of_the_highest_version_of_its_spec_that_the_workspace_binds(
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
) -> None:
    newer = PLAIN.model_copy(update={'version': 2})
    bind = [function_binding(s, staged_binding.function) for s in (newer, PLAIN)]
    workspace = Workspace(proposal='p1', files=files, bind=bind, store=FakeStore())
    with closing(workspace):
        client = Client(workspace, submitter='anna')
        manager = BatchManager(client, sources=SOURCES, definitions=definitions)

        label = manager.create('plain', title='Morning', columns=COLUMNS)

        assert manager.batch(label).spec == newer


def _with_definitions(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='b', definitions=definitions)
    batch.settings = {}


def _with_records(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='b', source=pair)
    batch.scan()
    client.wait(batch.reduce(['1']))


@pytest.mark.parametrize('taken', [_with_definitions, _with_records])
def test_a_label_that_has_definitions_or_records_is_refused(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
    taken: Callable[[Client, FolderDefinitions], None],
) -> None:
    sample(1, 'water')
    taken(client, definitions)

    with pytest.raises(ValueError, match='has definitions or records'):
        manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    assert manager.batches() == {}


@pytest.mark.parametrize('label', ['auto/iofq', '.b', '..'])
def test_a_label_with_a_slash_or_a_leading_dot_is_refused(
    manager: BatchManager, label: str
) -> None:
    with pytest.raises(ValueError, match='a label is'):
        manager.create('plain', title='Morning', columns=COLUMNS, label=label)
    assert manager.batches() == {}


def test_a_source_that_is_not_registered_is_refused(manager: BatchManager) -> None:
    with pytest.raises(KeyError, match='runs'):
        manager.create('plain', title='Morning', source='runs')
    assert manager.batches() == {}


def test_a_source_whose_columns_the_spec_lacks_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(ValueError, match=r"Find has no parameters \['transmission'\]"):
        manager.create(FIND.name, title='Morning', source='pairs')
    assert manager.batches() == {}


def test_a_spec_that_the_workspace_does_not_bind_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(KeyError, match='no spec iofq'):
        manager.create('iofq', title='Morning')
    assert manager.batches() == {}


def test_a_generated_label_is_the_time_of_making(manager: BatchManager) -> None:
    label = manager.create('plain', title='Morning', columns=COLUMNS)

    assert re.fullmatch(r'\d{8}-\d{6}', label)


def test_the_batch_of_a_label_is_one_object(manager: BatchManager) -> None:
    label = manager.create('plain', title='Morning', source='pairs')

    assert manager.batch(label) is manager.batch(label)


def test_a_title_and_description_changed_are_kept(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create('plain', title='Morning', source='pairs', description='a')

    header = manager.edit(label, title='Afternoon')
    header = manager.edit(label, description='b')

    assert header == Header('Afternoon', 'b', 'pairs')
    assert _again(client, definitions).batches() == {label: header}


def test_a_batch_removed_is_gone_with_its_definitions_and_its_records_stay(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    label = manager.create('plain', title='Morning', source='pairs', columns=COLUMNS)
    batch = manager.batch(label)
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    client.wait(batch.reduce(['1']))

    manager.remove(label)

    assert manager.batches() == {}
    assert definitions.labels() == []
    assert [r.member for r in client.records(label=label)] == ['1']
    with pytest.raises(KeyError):
        manager.batch(label)


def test_a_batch_without_a_source_has_only_rows_added_by_hand(
    manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    label = manager.create('plain', title='Morning', columns=COLUMNS)
    batch = manager.batch(label)

    assert manager.source(label) is None
    assert list(batch.rows) == []
    batch.put([Row('mine', {'sample': sample(2, 'oil')})])
    assert [r.key for r in manager.batch(label).rows] == ['mine']

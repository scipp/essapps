# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The commands of ``essbatch`` that write results by the output options of a batch:
output-options, export -l, and publish to a fake SciCat.
"""

import json
import sys
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from ess.batch.cli import main
from ess.batch.table import read_text
from scitacean.testing.client import FakeClient

CATALOGUE = '''
from scitacean import model
from scitacean.testing.client import FakeClient
from scitacean.testing.transfer import FakeFileTransfer

from ess.apps.scicat import Catalogue

SCICAT = FakeClient.without_login(
    url='https://scicat.example.org/api/v3', file_transfer=FakeFileTransfer()
)


def catalogue(proposal):
    SCICAT.proposals[proposal] = model.DownloadProposal(
        proposalId=proposal, title='Micelles', email='office@example.org',
        ownerGroup=proposal, pi_firstname='Ada', pi_lastname='Lovelace',
        pi_email='ada@example.org',
    )
    return Catalogue(client=SCICAT, instrument='toy')


def unreachable(proposal):
    raise ConnectionError('no catalogue')
'''


@pytest.fixture
def scicat(folder: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """The fake behind ``-c cli_toys_catalogue:catalogue``, new for each test."""
    (folder / 'cli_toys_catalogue.py').write_text(CATALOGUE)
    monkeypatch.syspath_prepend(folder)
    monkeypatch.delitem(sys.modules, 'cli_toys_catalogue', raising=False)
    return import_module('cli_toys_catalogue').SCICAT


@pytest.fixture
def publishing(command: list[str], reduced_once: Path, scicat: FakeClient) -> list[str]:
    """The command with the catalogue, on the batch x of rows row-0 and row-1."""
    return [*command, '-c', 'cli_toys_catalogue:catalogue']


@pytest.fixture
def options(command: list[str], reduced_once: Path) -> Path:
    """The output options of x, kept, with an owner and a contact."""
    path = reduced_once / 'options.yaml'
    assert main([*command, 'output-options', '-l', 'x', '-o', str(path)]) == 0
    fields = yaml.safe_load(path.read_text())
    path.write_text(
        yaml.safe_dump(fields | {'owner': 'Anna', 'contact_email': 'anna@example.org'})
    )
    assert main([*command, 'output-options', '-l', 'x', str(path)]) == 0
    return path


def test_output_options_says_the_formats_and_outputs_and_no_owner_without_a_catalogue(
    command: list[str], reduced_once: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, 'output-options', '-l', 'x']) == 0

    text = capsys.readouterr().out
    assert '# format: one of scipp-hdf5' in text
    assert '# outputs: some of total' in text
    fields = yaml.safe_load(text)
    assert fields['format'] == 'scipp-hdf5'
    assert fields['outputs'] == ['total']
    assert fields['owner'] == ''


def test_output_options_with_a_catalogue_defaults_to_the_principal_investigator(
    publishing: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*publishing, 'output-options', '-l', 'x']) == 0

    fields = yaml.safe_load(capsys.readouterr().out)
    assert fields['owner'] == 'Ada Lovelace'
    assert fields['contact_email'] == 'ada@example.org'


def test_output_options_keeps_the_options_of_a_file_and_says_them(
    command: list[str], options: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()

    assert main([*command, 'output-options', '-l', 'x']) == 0

    assert yaml.safe_load(capsys.readouterr().out) == yaml.safe_load(
        options.read_text()
    )


def test_an_options_file_without_output_options_is_refused(
    command: list[str], reduced_once: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = reduced_once / 'options.yaml'
    path.write_text('format: scipp-hdf5\ncolour: blue\n')

    assert main([*command, 'output-options', '-l', 'x', str(path)]) == 2

    assert f'{path}:' in capsys.readouterr().err


def test_export_of_a_label_writes_by_the_first_writer_of_w(
    command: list[str], reduced_once: Path
) -> None:
    out = reduced_once / 'out'
    writers = ['-W', 'cli_toys_batch_files:WRITERS']

    assert main([*command, *writers, 'export', '-l', 'x', str(out)]) == 0

    assert sorted(p.name for p in (out / 'row-0').iterdir()) == [
        '_record.json',
        'total.txt',
    ]
    assert (out / 'row-0' / 'total.txt').read_text().startswith('x row-0: ')


def test_export_of_a_label_again_after_a_format_change_leaves_only_the_new_files(
    command: list[str], reduced_once: Path
) -> None:
    out = reduced_once / 'out'
    writers = ['-W', 'cli_toys_batch_files:WRITERS']
    path = reduced_once / 'options.yaml'
    assert main([*command, *writers, 'export', '-l', 'x', str(out)]) == 0
    assert main([*command, *writers, 'output-options', '-l', 'x', '-o', str(path)]) == 0
    path.write_text(path.read_text().replace('format: text', 'format: scipp-hdf5'))
    assert main([*command, *writers, 'output-options', '-l', 'x', str(path)]) == 0

    assert main([*command, *writers, 'export', '-l', 'x', str(out)]) == 0

    assert sorted(p.name for p in (out / 'row-0').iterdir()) == [
        '_record.json',
        'total.h5',
    ]


def test_export_of_a_label_does_not_contact_the_catalogue(
    command: list[str], reduced_once: Path, scicat: FakeClient
) -> None:
    unreachable = ['-c', 'cli_toys_catalogue:unreachable']
    out = reduced_once / 'out'

    assert main([*command, *unreachable, 'export', '-l', 'x', str(out)]) == 0

    assert (out / 'row-0' / 'total.h5').is_file()


def test_kept_output_options_of_a_format_that_w_lacks_are_refused_naming_it(
    command: list[str], reduced_once: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    writers = ['-W', 'cli_toys_batch_files:WRITERS']
    path = reduced_once / 'options.yaml'
    assert main([*command, *writers, 'output-options', '-l', 'x', '-o', str(path)]) == 0
    assert main([*command, *writers, 'output-options', '-l', 'x', str(path)]) == 0

    assert main([*command, 'export', '-l', 'x', str(reduced_once / 'out')]) == 2

    assert 'no writer of the format text' in capsys.readouterr().err
    assert not (reduced_once / 'out').exists()


def test_w_that_names_no_writers_is_refused(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                *command,
                '-W',
                'cli_toys_batch_files:SOURCES',
                'output-options',
                '-l',
                'x',
            ]
        )

    assert 'not a mapping of spec names' in capsys.readouterr().err


def test_publish_writes_each_row_with_its_dataset(
    publishing: list[str], options: Path, folder: Path, scicat: FakeClient
) -> None:
    out = folder / 'published.csv'

    assert main([*publishing, 'publish', '-l', 'x', 'row-0', '-o', str(out)]) == 0

    [pid] = scicat.datasets
    written = read_text(out)
    assert list(written['key']) == ['row-0']
    assert list(written['published']) == [f'pid:{pid}']
    assert scicat.get_dataset(pid).name == 'x row-0'
    assert scicat.get_dataset(pid).owner == 'Anna'


def test_a_result_published_already_is_refused_and_uploads_nothing(
    publishing: list[str],
    options: Path,
    scicat: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publish = [*publishing, 'publish', '-l', 'x', 'row-0']
    assert main(publish) == 0

    assert main([*publish, 'row-1']) == 2

    assert 'published already: row-0' in capsys.readouterr().err
    assert len(scicat.datasets) == 1


def test_an_upload_that_fails_exits_with_1_and_is_not_kept_as_published(
    publishing: list[str],
    options: Path,
    scicat: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publish = [*publishing, 'publish', '-l', 'x', 'row-0']
    scicat.disabled['create_dataset_model'] = RuntimeError('SciCat is down')

    assert main(publish) == 1

    assert 'SciCat is down' in capsys.readouterr().err
    del scicat.disabled['create_dataset_model']
    assert main(publish) == 0


@pytest.mark.parametrize(
    ('arguments', 'said'),
    [
        (['publish', '-l', 'x', 'row-0'], 'publish needs -c/--catalogue'),
        (['-c', 'm:f', 'publish', '-l', 'x'], 'the following arguments are required'),
    ],
)
def test_publish_without_a_catalogue_or_anything_to_do_is_refused(
    command: list[str],
    arguments: list[str],
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main([*command, *arguments])

    assert said in capsys.readouterr().err


def test_show_says_which_results_are_published_without_a_catalogue(
    command: list[str],
    publishing: list[str],
    options: Path,
    scicat: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([*publishing, 'publish', '-l', 'x', 'row-0']) == 0
    capsys.readouterr()

    assert main([*command, '--json', 'show', '-l', 'x']) == 0

    [pid] = scicat.datasets
    rows = json.loads(capsys.readouterr().out)
    assert {r['key']: r['published'] for r in rows} == {
        'row-0': f'pid:{pid}',
        'row-1': None,
    }

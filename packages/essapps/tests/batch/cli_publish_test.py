# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The commands of ``essapps-batch`` that publish to a fake SciCat."""

import sys
from importlib import import_module
from pathlib import Path

import pytest
import yaml
from scitacean.testing.client import FakeClient

from ess.apps.batch.cli import main
from ess.apps.batch.table import read_text

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
def form(publishing: list[str], folder: Path) -> Path:
    """The form that publish-form writes, with the output total chosen."""
    path = folder / 'form.yaml'
    assert main([*publishing, 'publish-form', '-l', 'x', '-o', str(path)]) == 0
    fields = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump(fields | {'outputs': ['total']}))
    return path


def test_publish_form_says_the_outputs_and_defaults_to_the_principal_investigator(
    publishing: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*publishing, 'publish-form', '-l', 'x']) == 0

    text = capsys.readouterr().out
    assert '# outputs: some of total' in text
    fields = yaml.safe_load(text)
    assert fields['owner'] == 'Ada Lovelace'
    assert fields['contact_email'] == 'ada@example.org'
    assert fields['outputs'] == []


def test_publish_keeps_the_form_and_writes_each_row_with_its_dataset(
    publishing: list[str], form: Path, folder: Path, scicat: FakeClient
) -> None:
    out = folder / 'published.csv'
    publish = ['publish', '-l', 'x', '--form', str(form), 'row-0', '-o', str(out)]

    assert main([*publishing, *publish]) == 0

    [pid] = scicat.datasets
    written = read_text(out)
    assert list(written['key']) == ['row-0']
    assert list(written['pid']) == [f'pid:{pid}']
    assert scicat.get_dataset(pid).name == 'x row-0'
    assert main([*publishing, 'publish', '-l', 'x', 'row-1']) == 0  # the form kept
    assert len(scicat.datasets) == 2


def test_a_result_published_already_is_refused_and_uploads_nothing(
    publishing: list[str],
    form: Path,
    scicat: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publish = [*publishing, 'publish', '-l', 'x', '--form', str(form), 'row-0']
    assert main(publish) == 0

    assert main([*publish, 'row-1']) == 2

    assert 'published already: row-0' in capsys.readouterr().err
    assert len(scicat.datasets) == 1


def test_a_form_file_that_is_not_a_form_is_refused(
    publishing: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = folder / 'form.yaml'
    path.write_text('outputs: [total]\ncolour: blue\n')

    assert main([*publishing, 'publish', '-l', 'x', '--form', str(path)]) == 2

    assert f'{path}:' in capsys.readouterr().err


def test_an_upload_that_fails_exits_with_1_and_is_not_kept_as_published(
    publishing: list[str],
    form: Path,
    scicat: FakeClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    publish = [*publishing, 'publish', '-l', 'x', '--form', str(form), 'row-0']
    scicat.disabled['create_dataset_model'] = RuntimeError('SciCat is down')

    assert main(publish) == 1

    assert 'SciCat is down' in capsys.readouterr().err
    del scicat.disabled['create_dataset_model']
    assert main(publish) == 0


@pytest.mark.parametrize(
    ('arguments', 'said'),
    [
        (['publish', '-l', 'x', 'row-0'], 'publish needs -c/--catalogue'),
        (['-c', 'm:f', 'publish', '-l', 'x'], 'give a KEY to publish, or --form'),
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

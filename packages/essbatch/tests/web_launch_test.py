# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The launch page over HTTP: the workspaces of a root folder, opened and removed."""

import time
from collections.abc import Callable, Iterator

import pytest
from ess.batch import Header, WorkspaceHeader, create_workspace, workspaces
from ess.batch.web import Launcher, create_launch_app
from fastapi.testclient import TestClient

from ess.apps.folder import ArrivingFiles, open_folder

from . import toys

CATALOGUED = WorkspaceHeader(Header('Commissioning'), 'p1', 'scicat:fake')
NEW = {'name': 'test', 'title': 'Test', 'proposal': 'local', 'files': 'local'}


@pytest.fixture
def http(launcher: Launcher, gate: toys.Gate) -> Iterator[TestClient]:
    """The launch app, served until the test ends; the gate opens before."""
    with TestClient(create_launch_app(launcher)) as http:
        try:
            yield http
        finally:
            gate.open()


def opened(http: TestClient, name: str) -> TestClient:
    """A client of the batch page of the workspace ``name``, opened."""
    assert http.post(f'/api/workspaces/{name}/open').status_code == 200
    page = http.get(f'/workspaces/{name}/').url
    return TestClient(http.app, base_url=str(page))


def wait_for(done: Callable[[], bool]) -> None:
    give_up = time.monotonic() + 10
    while not done() and time.monotonic() < give_up:
        time.sleep(0.01)


def test_the_list_states_each_workspace_and_why_one_cannot_be_opened(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    other = WorkspaceHeader(Header('Elsewhere', 'Another SciCat'), 'p2', 'scicat:x')
    create_workspace(launcher.root, 'elsewhere', other)

    listed = http.get('/api/workspaces').json()

    assert listed['application'] == 'Toy'
    assert listed['sources'] == ['local', 'scicat:fake']
    assert listed['rows'] == [
        {
            'name': 'commissioning',
            'title': 'Commissioning',
            'description': '',
            'proposal': 'p1',
            'files': 'scicat:fake',
            'open': False,
            'problem': None,
        },
        {
            'name': 'elsewhere',
            'title': 'Elsewhere',
            'description': 'Another SciCat',
            'proposal': 'p2',
            'files': 'scicat:x',
            'open': False,
            'problem': 'its files are of scicat:x, which this application does '
            'not offer (it offers local, scicat:fake)',
        },
    ]


def test_a_workspace_made_on_the_page_is_stored_in_the_root(
    http: TestClient, launcher: Launcher
) -> None:
    made = http.post('/api/workspaces', json=NEW)

    assert made.json() == {'notice': 'Made Test.'}
    assert workspaces(launcher.root) == {'test': WorkspaceHeader(Header('Test'))}


@pytest.mark.parametrize(
    ('change', 'said'),
    [
        ({'files': 'scicat:x'}, 'its files are of scicat:x, which this'),
        ({'name': 'a b'}, "'a b': use letters"),
        ({'title': ' '}, 'give a title that is not empty'),
        ({'proposal': ''}, 'give a proposal'),
    ],
)
def test_a_new_workspace_in_error_is_rejected_with_a_message(
    http: TestClient, launcher: Launcher, change: dict[str, str], said: str
) -> None:
    made = http.post('/api/workspaces', json=NEW | change)

    assert made.status_code == 422
    assert made.json()['message'].startswith(f'The workspace was not made: {said}')
    assert workspaces(launcher.root) == {}


def test_an_opened_workspace_serves_its_batch_page_under_its_name(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)

    state = opened(http, 'commissioning').get('api/state').json()

    assert state['title'] == 'Pairs'
    assert http.get('/api/workspaces').json()['rows'][0]['open'] is True


def test_a_request_to_a_workspace_that_is_not_open_opens_it(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)

    batches = http.get('/workspaces/commissioning/api/batches')

    assert [b['title'] for b in batches.json()['rows']] == ['Pairs']


def test_a_workspace_of_a_file_source_not_offered_is_not_opened(
    http: TestClient, launcher: Launcher
) -> None:
    other = WorkspaceHeader(Header('Elsewhere'), 'p2', 'scicat:x')
    create_workspace(launcher.root, 'elsewhere', other)

    answer = http.post('/api/workspaces/elsewhere/open')

    assert answer.status_code == 422
    assert answer.json()['message'].startswith(
        'Cannot open elsewhere: its files are of scicat:x'
    )


def test_follow_of_an_opened_workspace_steps(
    http: TestClient, launcher: Launcher, sample: toys.Measure
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    page = opened(http, 'commissioning')

    sample(1, 'water')
    wait_for(lambda: page.get('api/state').json()['rows'] != [])

    assert [r['name'] for r in page.get('api/state').json()['rows']] == ['water']


def test_a_removed_workspace_is_closed_and_its_folder_deleted(
    http: TestClient, launcher: Launcher
) -> None:
    folder = create_workspace(launcher.root, 'commissioning', CATALOGUED)
    opened(http, 'commissioning')

    removed = http.post('/api/workspaces/commissioning/remove')

    assert removed.json() == {'notice': 'Removed Commissioning.'}
    assert (http.get('/api/workspaces').json()['rows'], folder.exists()) == ([], False)


def test_remove_is_refused_while_a_record_of_the_workspace_is_pending(
    http: TestClient, launcher: Launcher, sample: toys.Measure, gate: toys.Gate
) -> None:
    folder = create_workspace(launcher.root, 'commissioning', CATALOGUED)
    page = opened(http, 'commissioning')
    gate.close()
    sample(1, 'water')
    wait_for(lambda: page.get('api/state').json()['rows'] != [])

    removed = http.post('/api/workspaces/commissioning/remove')
    gate.open()

    assert removed.status_code == 422
    assert removed.json()['message'] == (
        'A reduction has not ended. Cancel it, or wait until it ends.'
    )
    assert (folder / 'workspace.json').is_file()
    assert page.get('api/state').json()['follow']['on'] is False


def test_the_open_workspaces_close_when_the_server_stops(launcher: Launcher) -> None:
    folder = create_workspace(launcher.root, 'test', WorkspaceHeader(Header('Test')))
    with TestClient(create_launch_app(launcher)) as http:
        opened(http, 'test')

    with open_folder(folder, [], files=ArrivingFiles(folder)) as client:
        assert client.records() == []  # opened: no other holds the folder

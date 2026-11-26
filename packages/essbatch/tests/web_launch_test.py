# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The launch page over HTTP: the workspaces of a root folder, opened, closed and
removed.
"""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urljoin

import pytest
from ess.batch import (
    Header,
    WorkspaceHeader,
    create_workspace,
    read_workspace_header,
    workspaces,
)
from ess.batch.web import Launcher, create_launch_app
from fastapi.testclient import TestClient

from ess.apps.errors import Stopped
from ess.apps.folder import ArrivingFiles, open_folder

from . import toys

CATALOGUED = WorkspaceHeader(Header('Commissioning'), 'p1', 'scicat:fake')
LOCAL = WorkspaceHeader(Header('Test'))
NOT_ALLOWED = WorkspaceHeader(Header('Elsewhere', 'Another SciCat'), 'p2', 'scicat:x')
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


def wait_for(done: Callable[[], object]) -> None:
    give_up = time.monotonic() + 10
    while not done() and time.monotonic() < give_up:
        time.sleep(0.01)


def statuses(page: TestClient) -> list[str]:
    return [r['status'] for r in page.get('api/state').json()['rows']]


def opens(folder: Path) -> bool:
    """Whether a process may open the workspace in ``folder``, as essbatch does."""
    try:
        proposal = read_workspace_header(folder).proposal
        open_folder(folder, [], files=ArrivingFiles(folder), proposal=proposal).close()
    except RuntimeError:  # another holds it
        return False
    return True


def test_the_list_states_each_workspace_and_why_one_cannot_be_opened(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    create_workspace(launcher.root, 'elsewhere', NOT_ALLOWED)

    listed = http.get('/api/workspaces').json()

    assert listed['application'] == 'Toy'
    assert listed['allowed'] == ['local', 'scicat:fake']
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
            'problem': 'its file source is scicat:x, which this application does '
            'not allow (it allows local, scicat:fake)',
        },
    ]


def test_a_workspace_whose_header_cannot_be_read_is_listed_with_the_reason(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    (launcher.root / 'broken').mkdir()
    (launcher.root / 'broken' / 'workspace.json').write_text('{')

    rows = http.get('/api/workspaces').json()['rows']

    assert [(r['name'], r['title'], r['proposal']) for r in rows] == [
        ('broken', 'broken', None),
        ('commissioning', 'Commissioning', 'p1'),
    ]
    assert rows[0]['problem'].startswith(f'{launcher.root}/broken/workspace.json: ')


def test_a_workspace_made_on_the_page_is_stored_in_the_root(
    http: TestClient, launcher: Launcher
) -> None:
    made = http.post('/api/workspaces', json=NEW)

    assert made.json() == {'notice': 'Made Test.'}
    assert workspaces(launcher.root) == ['test']
    assert read_workspace_header(launcher.root / 'test') == LOCAL


@pytest.mark.parametrize(
    ('change', 'said'),
    [
        ({'files': 'scicat:x'}, 'its file source is scicat:x, which this'),
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
    assert workspaces(launcher.root) == []


def test_an_opened_workspace_serves_its_batch_page_under_its_name(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)

    state = opened(http, 'commissioning').get('api/state').json()

    assert state['title'] == 'Pairs'
    assert http.get('/api/workspaces').json()['rows'][0]['open'] is True


def test_the_batch_page_of_a_workspace_links_to_the_launch_page(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    root = 'http://testserver/workspaces/commissioning/'

    home = opened(http, 'commissioning').get('api/home').json()['url']

    assert urljoin(root, home) == str(http.get('/').url)


def test_a_request_to_a_workspace_that_is_not_open_opens_it(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)

    batches = http.get('/workspaces/commissioning/api/batches')

    assert [b['title'] for b in batches.json()['rows']] == ['Pairs']


def test_a_workspace_of_a_file_source_not_allowed_is_not_opened(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'elsewhere', NOT_ALLOWED)

    answer = http.post('/api/workspaces/elsewhere/open')

    assert answer.status_code == 422
    assert answer.json()['message'].startswith(
        'Cannot open elsewhere: its file source is scicat:x'
    )


def test_follow_of_an_opened_workspace_steps(
    http: TestClient, launcher: Launcher, sample: toys.Measure
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    page = opened(http, 'commissioning')

    sample(1, 'water')
    wait_for(lambda: page.get('api/state').json()['rows'] != [])

    assert [r['name'] for r in page.get('api/state').json()['rows']] == ['water']


def test_a_closed_workspace_is_free_for_another_process_and_not_opened_again(
    http: TestClient, launcher: Launcher
) -> None:
    folder = create_workspace(launcher.root, 'commissioning', CATALOGUED)
    page = opened(http, 'commissioning')

    closed = http.post('/api/workspaces/commissioning/close')

    assert closed.json() == {
        'notice': 'Closed Commissioning. Nothing is deleted; open it to go on.'
    }
    assert http.get('/api/workspaces').json()['rows'][0]['open'] is False
    assert opens(folder)
    polled = page.get('api/state')
    assert (polled.status_code, polled.json()['message']) == (
        422,
        'commissioning is closed. Open it on the launch page.',
    )


def test_a_workspace_closed_and_opened_again_follows_again(
    http: TestClient, launcher: Launcher, sample: toys.Measure
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    opened(http, 'commissioning')
    assert http.post('/api/workspaces/commissioning/close').status_code == 200

    sample(1, 'water')
    page = opened(http, 'commissioning')
    wait_for(lambda: page.get('api/state').json()['rows'] != [])

    state = page.get('api/state').json()
    assert state['follow']['on'] is True
    assert [r['name'] for r in state['rows']] == ['water']


def test_close_is_refused_while_a_record_is_pending_and_follow_goes_on(
    http: TestClient, launcher: Launcher, sample: toys.Measure, gate: toys.Gate
) -> None:
    folder = create_workspace(launcher.root, 'commissioning', CATALOGUED)
    page = opened(http, 'commissioning')
    gate.close()
    sample(1, 'water')
    wait_for(lambda: page.get('api/state').json()['rows'] != [])

    closed = http.post('/api/workspaces/commissioning/close')
    gate.open()

    assert closed.status_code == 422
    assert closed.json()['message'] == (
        'A reduction has not ended. Cancel it, or wait until it ends.'
    )
    assert page.get('api/state').json()['follow']['on'] is True
    assert not opens(folder)


def test_a_removed_workspace_is_closed_and_its_folder_deleted(
    http: TestClient, launcher: Launcher
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    opened(http, 'commissioning')

    removed = http.post('/api/workspaces/commissioning/remove')

    assert removed.json() == {'notice': 'Removed commissioning.'}
    assert http.get('/api/workspaces').json()['rows'] == []
    assert list(launcher.root.iterdir()) == []


def test_remove_is_refused_while_a_record_of_the_workspace_is_pending(
    http: TestClient, launcher: Launcher, sample: toys.Measure, gate: toys.Gate
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
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
    assert workspaces(launcher.root) == ['commissioning']


@pytest.mark.parametrize('broken', ['not allowed', 'unreadable'])
def test_a_workspace_that_cannot_be_opened_is_removed_without_opening_it(
    http: TestClient, launcher: Launcher, broken: str
) -> None:
    folder = create_workspace(launcher.root, 'elsewhere', NOT_ALLOWED)
    if broken == 'unreadable':
        (folder / 'workspace.json').write_text('{')

    removed = http.post('/api/workspaces/elsewhere/remove')

    assert removed.json() == {'notice': 'Removed elsewhere.'}
    assert list(launcher.root.iterdir()) == []


@pytest.mark.parametrize(
    ('action', 'status', 'said'),
    [
        ('remove', 422, 'Cannot remove test: another process has it open.'),
        ('close', 404, 'test is not open.'),
    ],
)
def test_a_workspace_that_another_process_holds_is_not_removed_or_closed(
    http: TestClient, launcher: Launcher, action: str, status: int, said: str
) -> None:
    folder = create_workspace(launcher.root, 'test', LOCAL)

    with open_folder(folder, [], files=ArrivingFiles(folder)):
        answer = http.post(f'/api/workspaces/test/{action}')

    assert (answer.status_code, answer.json()['message']) == (status, said)
    assert workspaces(launcher.root) == ['test']


def test_a_workspace_folder_that_is_a_link_is_refused_before_it_is_closed(
    http: TestClient, launcher: Launcher, tmp_path: Path
) -> None:
    target = create_workspace(tmp_path / 'elsewhere', 'real', CATALOGUED)
    launcher.root.mkdir()
    (launcher.root / 'linked').symlink_to(target)
    opened(http, 'linked')

    removed = http.post('/api/workspaces/linked/remove')

    assert (removed.status_code, removed.json()['message']) == (
        422,
        'Cannot remove linked: its folder is a link to another folder. Delete the '
        'link by hand.',
    )
    assert http.get('/api/workspaces').json()['rows'][0]['open'] is True


def test_the_open_workspaces_close_when_the_server_stops(launcher: Launcher) -> None:
    folder = create_workspace(launcher.root, 'test', LOCAL)
    with TestClient(create_launch_app(launcher)) as http:
        opened(http, 'test')

    assert opens(folder)


def test_the_server_stops_with_each_workspace_closed_when_one_close_raises(
    launcher: Launcher, sample: toys.Measure
) -> None:
    resource = pytest.importorskip('resource')
    local = create_workspace(launcher.root, 'local', LOCAL)
    stopping = create_workspace(launcher.root, 'commissioning', CATALOGUED)
    with pytest.raises(ExceptionGroup) as raised:  # noqa: F821, PT012
        with TestClient(create_launch_app(launcher)) as http:
            opened(http, 'local')
            page = opened(http, 'commissioning')  # the first to close
            sample(1, 'water')
            wait_for(lambda: statuses(page) == ['done'])
            soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            # The log cannot grow, as on a full disk: the workspace stops at
            # the next submission, and its close raises.
            size = (stopping / 'log.jsonl').stat().st_size
            resource.setrlimit(resource.RLIMIT_FSIZE, (size, hard))
            try:
                page.post('api/reduce', json={'which': 'all'})
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))

    assert [type(e) for e in raised.value.exceptions] == [Stopped]
    assert (opens(stopping), opens(local)) == (True, True)


def test_a_workspace_that_waits_for_its_records_at_the_stop_blocks_no_request(
    launcher: Launcher, sample: toys.Measure, gate: toys.Gate
) -> None:
    create_workspace(launcher.root, 'commissioning', CATALOGUED)
    answered: list[float] = []
    released: list[float] = []

    def release() -> None:
        released.append(time.monotonic())
        gate.open()

    with TestClient(create_launch_app(launcher)) as http:
        page = opened(http, 'commissioning')
        gate.close()
        sample(1, 'water')
        wait_for(lambda: page.get('api/state').json()['rows'] != [])

        def ask() -> None:
            http.get('/api/workspaces')
            answered.append(time.monotonic())

        threading.Timer(0.2, ask).start()
        threading.Timer(1.0, release).start()
    # The stop waits for the reduction, which runs until the gate opens.

    assert answered[0] < released[0]

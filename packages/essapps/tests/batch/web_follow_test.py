# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Follow on the page of a batch: a step of the batch that follows scans as
"Rescan runs" does, and reduces the rows that arrived. The tests call
:meth:`Pages.tick` in place of the timer, except one, which runs the app's
timer.
"""

import threading
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ess.apps import Client, Status
from ess.apps.batch import BatchManager, CorrectedFiles, FolderDefinitions
from ess.apps.batch.auto import Follow
from ess.apps.batch.web import Pages, Server, create_app
from ess.apps.batch.web.models import CellEdit, Cells, Edit, Reduce, State
from ess.apps.batch.web.server import Problem
from ess.apps.testing import FakeFiles

from .toys import COLUMNS, DEFAULTS, SOURCES, STAGED, Gate, Measure, Refusing

BINS = ['binning', 'bins']


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def pages(manager: BatchManager, refusing: Refusing, files: CorrectedFiles) -> Pages:
    return Pages(manager, Follow(manager, refusing), files=files, marks=Marks)


@pytest.fixture
def server(pages: Pages, label: str) -> Server:
    """The server of the page of ``label``, the batch that follows."""
    pages.follow.on(label)
    return pages.page(label)


def opened(pages: Pages, label: str) -> TestClient:
    """A client of the page of ``label``, which asks for ``api/...`` as it does."""
    return TestClient(create_app(pages), base_url=f'http://testserver/batches/{label}/')


def statuses(state: State) -> dict[str, str]:
    return {row.name: row.status for row in state.rows}


def bins(server: Server, value: int) -> None:
    """Set the number of bins in the form, and apply it."""
    server.edit_settings(Edit(path=BINS, op='set', value=value))
    server.apply_settings()


def done(server: Server, client: Client) -> State:
    client.wait(client.records())
    return server.state()


def test_a_step_shows_new_rows_and_runs_and_reduces_the_rows_that_arrived(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    before = server.state()
    sample(1, 'water')
    sample(2, 'oil', partner=False)

    gate.close()
    server.pages.tick()

    after = server.state()
    assert after.version > before.version
    assert server.runs().version > before.runs  # type: ignore[operator]
    assert len(server.runs().rows) == 3
    assert statuses(after) == {'water': 'running', 'oil': 'incomplete'}
    assert after.follow is not None
    assert after.follow.failure is None
    gate.open()
    assert statuses(done(server, client)) == {'water': 'done', 'oil': 'incomplete'}


def versions(server: Server) -> tuple[int, int]:
    return server.version, server.pages.runs_version


@pytest.mark.parametrize(
    'action',
    [
        lambda server: None,
        lambda server: server.scan(),
        lambda server: server.correct(
            CellEdit(keys=[server.runs().rows[0].key], column='sample', value='tea')
        ),
    ],
    ids=['nothing', 'rescan', 'correction'],
)
def test_a_step_that_finds_nothing_new_changes_no_version(
    server: Server, client: Client, sample: Measure, action: Any
) -> None:
    sample(1, 'water')
    server.pages.tick()
    client.wait(client.records())
    server.pages.tick()  # sees the result that finished
    action(server)
    before = versions(server)

    server.pages.tick()
    server.pages.tick()

    assert versions(server) == before
    assert server.poll(before[0], server.start) is None


def changed_behind_the_page(server: Server, what: str, fake: FakeFiles) -> None:
    """Change one thing a step shows, and nothing else, but not by an action."""
    if what == 'runs':
        fake.measure(9, 1.0, role='background')
    elif what == 'rows':
        server.batch.rows = server.batch.rows.edit('1', {'thickness': 2.0})
    elif what == 'refusals':
        server.batch.settings = {**server.batch.settings, 'binning': {'bins': 0}}
    elif what == 'failure':
        fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))


@pytest.mark.parametrize('what', ['runs', 'rows', 'refusals', 'failure'])
def test_a_step_that_changes_one_thing_the_page_shows_tells_the_page(
    server: Server, client: Client, sample: Measure, fake: FakeFiles, what: str
) -> None:
    sample(1, 'water')
    server.follow(True)
    client.wait(client.records())
    server.pages.tick()
    seen = server.version

    changed_behind_the_page(server, what, fake)
    server.pages.tick()

    assert server.poll(seen, server.start) is not None


def test_a_step_after_a_result_finished_tells_the_page(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    server.pages.tick()
    running = server.state().version
    gate.open()
    client.wait(client.records())

    server.pages.tick()

    shown = server.poll(running, server.start)
    assert shown is not None
    assert statuses(shown) == {'water': 'done'}


class FinishingClient:
    """
    Delegates to a client, and lets the reductions in flight finish right after
    the first read of their status that says they run: a result that finishes
    just after the server looked.
    """

    def __init__(self, client: Client, gate: Gate) -> None:
        self._client = client
        self._gate = gate
        self.finishing = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def status(self, records: Any) -> Any:
        statuses = self._client.status(records)
        if self.finishing and Status.PENDING in statuses:
            self.finishing = False
            self._gate.open()
            self._client.wait(records)
        return statuses


def test_a_result_that_finishes_while_a_step_runs_reaches_the_page_at_its_next_poll(
    client: Client, sample: Measure, gate: Gate, definitions: FolderDefinitions
) -> None:
    finishing = FinishingClient(client, gate)
    manager = BatchManager(
        finishing,  # type: ignore[arg-type]
        sources=SOURCES,
        definitions=definitions,
        defaults=DEFAULTS,
    )
    label = manager.create(
        STAGED.name, title='Scan', source='pairs', columns=('thickness',)
    )
    sample(1, 'water')
    pages = Pages(manager, Follow(manager, definitions))
    pages.follow.on(label)
    page = opened(pages, label)
    server = pages.page(label)
    gate.close()
    finishing.finishing = True

    server.pages.tick()

    shown = page.get(
        '/api/state', params={'after': server.version, 'start': server.start}
    )
    assert shown.status_code == 200
    assert {r['status'] for r in shown.json()['rows']} == {'done'}


def test_a_step_reduces_a_row_whose_cell_changed_at_a_column_of_at(
    server: Server, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    server.pages.tick()
    first = done(server, client).rows[0].record

    fake.measure(1005, 2.0, role='transmission', sample='water')
    server.pages.tick()

    assert done(server, client).rows[0].record != first


def test_a_run_that_a_step_finds_can_be_chosen_in_a_cell_without_a_runs_view(
    manager: BatchManager, refusing: Refusing, label: str, sample: Measure
) -> None:
    pages = Pages(manager, Follow(manager, refusing))
    pages.follow.on(label)
    server = pages.page(label)
    server.pages.tick()
    sample(1, 'water', partner=False)  # a row that waits, so no record is made

    server.pages.tick()

    assert option(server, 'sample', 1) == 'run 1 (sample)'


def test_a_step_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.pages.tick()
    first = done(server, client).rows[0].record

    bins(server, 3)
    sample(2, 'oil')
    server.pages.tick()

    shown = {row.name: row for row in done(server, client).rows}
    assert (shown['water'].record, shown['water'].earlier) == (first, True)
    assert (shown['oil'].status, shown['oil'].earlier) == ('done', False)
    server.reduce(Reduce(which='earlier'))
    assert not done(server, client).rows[0].earlier


def test_a_row_the_step_could_not_submit_shows_why(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    bins(server, 0)

    server.follow(True)

    (row,) = server.state().rows
    assert row.status == 'incomplete'
    assert row.reason.startswith('Follow could not reduce it: ')
    assert 'greater than or equal to 1' in row.reason
    assert client.records() == []
    assert statuses(server.follow(False)) == {'water': 'ready'}


def test_apply_while_following_reduces_nothing_and_the_next_step_uses_it(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.follow(True)
    done(server, client)
    sample(2, 'oil')
    bins(server, 0)
    server.pages.tick()
    before = ids(client)

    server.edit_settings(Edit(path=BINS, op='set', value=3))
    applied = server.apply_settings()

    assert ids(client) == before
    oil = {row.name: row for row in applied.rows}['oil']
    assert (oil.status, oil.reason) == ('ready', '')
    server.pages.tick()
    shown = {row.name: row for row in done(server, client).rows}
    assert (shown['water'].earlier, shown['oil'].status) == (True, 'done')
    assert client.latest('scan', '2').request.params['binning'] == {'bins': 3}


def test_a_done_row_reads_outdated_while_follow_is_refused_the_settings(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.follow(True)
    done(server, client)
    sample(2, 'oil')
    bins(server, 0)

    server.pages.tick()

    shown = {row.name: row for row in server.state().rows}
    assert (shown['water'].status, shown['oil'].status) == ('outdated', 'incomplete')
    assert shown['oil'].reason.startswith('Follow could not reduce it: ')


def test_a_step_that_raises_is_said_and_the_next_step_runs(
    server: Server,
    client: Client,
    sample: Measure,
    fake: FakeFiles,
    files: CorrectedFiles,
) -> None:
    sample(1, 'water')
    nameless = fake.measure(7, 1.0, role='transmission')  # the source needs a name

    server.pages.tick()
    failed = server.state().follow
    files.correct(nameless, sample='oil')
    server.pages.tick()

    assert failed is not None
    assert failed.failure == "Follow could not step: 'sample'"
    assert server.state().follow.failure is None  # type: ignore[union-attr]
    assert statuses(done(server, client)) == {'water': 'done'}


def test_a_step_waits_for_a_request_that_holds_the_lock(
    server: Server, sample: Measure
) -> None:
    sample(1, 'water')
    stepped = threading.Event()

    with server.lock:
        thread = threading.Thread(target=lambda: (server.pages.tick(), stepped.set()))
        thread.start()
        assert not stepped.wait(0.2)
        assert server.state().rows == []
    thread.join()

    assert stepped.is_set()
    assert [row.name for row in server.state().rows] == ['water']


def test_turning_follow_on_steps_at_once_and_off_stops(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')

    gate.close()
    on = server.follow(True)
    sample(2, 'oil')
    off = server.follow(False)
    gate.open()

    assert on.follow is not None
    assert (on.follow.on, on.notice) == (True, 'Following.')
    assert statuses(on) == {'water': 'running'}
    assert (off.follow.on, off.notice) == (False, 'Stopped following.')  # type: ignore[union-attr]
    assert statuses(off) == {'water': 'running'}


def test_a_batch_without_a_source_offers_no_follow(
    pages: Pages, manager: BatchManager
) -> None:
    server = pages.page(manager.create(STAGED.name, title='By hand', columns=COLUMNS))

    assert server.state().follow is None
    with pytest.raises(Problem, match='no source'):
        server.follow(True)


def option(server: Server, column: str, run: int) -> str:
    """The label of the option of a run in the editor of ``column``."""
    (shown,) = (c for c in server.state().columns if c.name == column)
    assert shown.editor is not None
    return next(o for o in shown.editor.options if o.startswith(f'run {run}'))


def test_a_refusal_of_a_running_row_is_said_once_and_not_kept(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    server.follow(True)
    request = Reduce(which='selected', keys=['1'])

    with pytest.raises(Problem, match=r'Nothing was reduced: .*still running'):
        server.reduce(request)
    gate.open()
    after = done(server, client)

    assert statuses(after) == {'water': 'done'}
    assert after.refused == []


def ids(client: Client) -> set[str]:
    return {r.id for r in client.records()}


def test_follow_leaves_a_row_changed_by_hand_to_the_person(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    server.follow(True)
    done(server, client)
    before = ids(client)

    for column, run in [('sample', 2), ('transmission', 1002)]:
        value = option(server, column, run)
        edited = server.edit(CellEdit(keys=['1'], column=column, value=value))
        server.pages.tick()

    assert ids(client) == before
    assert statuses(edited) == {'water': 'outdated', 'oil': 'done'}
    assert edited.action.label == 'Reduce 1 outdated'
    server.reduce(Reduce(which='remaining'))
    server.pages.tick()
    assert len(ids(client) - before) == 1
    water, oil = (client.latest('scan', k).request.params for k in ('1', '2'))
    assert (water['sample'], water['transmission']) == (
        oil['sample'],
        oil['transmission'],
    )


@pytest.mark.parametrize('following', [True, False])
def test_a_row_set_by_hand_says_that_follow_leaves_it_to_the_person(
    server: Server, sample: Measure, following: bool
) -> None:
    sample(1, 'water')
    server.scan()
    server.edit(CellEdit(keys=['1'], column='thickness', value=2.0))

    shown = server.follow(following)

    (row,) = shown.rows
    assert row.status == 'ready'
    assert row.reason == (
        'Set by hand, so Follow leaves it to you: reduce it.' * following
    )
    assert shown.action.label == 'Reduce 1 remaining'


def test_a_row_a_person_reduced_after_an_edit_follows_the_files_again(
    server: Server, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    server.follow(True)
    done(server, client)
    other = option(server, 'sample', 2)
    server.edit(CellEdit(keys=['1'], column='sample', value=other))
    server.reduce(Reduce(which='remaining'))
    edited = done(server, client).rows[0].record

    fake.measure(1005, 2.0, role='transmission', sample='water')
    server.pages.tick()

    assert statuses(done(server, client)) == {'water': 'done', 'oil': 'done'}
    record = client.latest('scan', '1')
    assert record.id != edited
    assert (
        record.request.params['sample']
        == client.latest('scan', '2').request.params['sample']
    )


def test_a_cell_reverted_after_a_reduction_is_followed_at_the_next_step(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    server.follow(True)
    done(server, client)
    for column, run in [('sample', 2), ('transmission', 1002)]:
        value = option(server, column, run)
        server.edit(CellEdit(keys=['1'], column=column, value=value))
    server.reduce(Reduce(which='remaining'))
    done(server, client)

    server.revert(Cells(keys=['1'], column='sample'))
    server.pages.tick()

    water, oil = (client.latest('scan', k).request.params for k in ('1', '2'))
    assert water['sample'] != oil['sample']  # the scan's
    assert water['transmission'] == oil['transmission']  # still set by hand


def test_an_edit_not_reduced_leaves_its_row_to_the_person_when_a_file_arrives(
    server: Server, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    server.follow(True)
    done(server, client)
    server.edit(CellEdit(keys=['1'], column='thickness', value=2.0))
    before = ids(client)

    fake.measure(1005, 2.0, role='transmission', sample='water')
    server.pages.tick()

    (row,) = server.state().rows
    assert ids(client) == before
    assert (row.status, row.reason) == (
        'outdated',
        'Transmission and Thickness changed.'
        ' Set by hand, so Follow leaves it to you: reduce it.',
    )


@pytest.mark.parametrize('value', [3, None], ids=['changed', 'missing'])
def test_follow_reduces_with_the_batch_s_settings_whatever_the_form_holds(
    server: Server, client: Client, sample: Measure, value: int | None
) -> None:
    server.follow(True)
    server.edit_settings(Edit(path=BINS, op='set', value=value))
    sample(1, 'water')

    server.pages.tick()

    assert statuses(done(server, client)) == {'water': 'done'}
    assert client.latest('scan', '1').request.params['binning'] == {'bins': 2}


def test_a_step_that_raises_anywhere_is_said_and_follow_stays_on(
    server: Server, pages: Pages, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    on = server.follow(True)
    pages.tick()
    failed = server.state().follow
    fake.list = listed
    pages.tick()

    assert on.follow is not None
    assert (on.follow.on, on.notice) == (True, 'Following.')
    assert failed is not None
    assert 'half written' in (failed.failure or '')
    assert statuses(done(server, client)) == {'water': 'done'}


class Unsayable(Exception):
    """An exception that cannot be put in words."""

    def __str__(self) -> str:
        raise ValueError('no words')

    __repr__ = __str__


def test_a_tick_whose_step_raises_what_cannot_be_said_lets_the_next_tick_run(
    server: Server, pages: Pages, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(Unsayable())

    pages.tick()
    fake.list = listed
    pages.tick()

    assert statuses(done(server, client)) == {'water': 'done'}


def test_a_tick_does_nothing_while_not_following(
    server: Server, pages: Pages, sample: Measure
) -> None:
    pages.follow.off()
    sample(1, 'water')

    pages.tick()

    assert server.state().rows == []


class WatchedLock:
    """A re-entrant lock that says when a thread has to wait to take it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.waited = threading.Event()

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.waited.set()
            self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def test_a_tick_that_waited_for_the_request_that_stops_following_does_nothing(
    server: Server, pages: Pages, client: Client, sample: Measure
) -> None:
    lock = WatchedLock()
    pages.lock = lock  # type: ignore[assignment]
    timer = threading.Thread(target=pages.tick)

    with pages.lock:  # a request that runs while the timer fires
        timer.start()
        assert lock.waited.wait(30)  # the tick waits for the lock
        sample(1, 'water')
        server.follow(False)
    timer.join()

    assert server.state().rows == []
    assert client.records() == []


def test_the_app_steps_while_following_and_stops_with_the_app(
    manager: BatchManager,
    refusing: Refusing,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')
    follow = Follow(manager, refusing, interval=0.01)
    follow.on(label)

    with opened(Pages(manager, follow), label) as http:
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)
        state: dict[str, Any] = http.get('/api/state').json()

    assert state['follow']['on']
    assert [row['name'] for row in state['rows']] == ['water']
    assert not any(t.name == 'every' for t in threading.enumerate())


def test_a_parent_that_runs_the_lifespan_of_the_app_it_mounts_steps_follow(
    manager: BatchManager,
    refusing: Refusing,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')
    follow = Follow(manager, refusing, interval=0.01)
    follow.on(label)
    batch = create_app(Pages(manager, follow))
    parent = FastAPI(lifespan=lambda _: batch.router.lifespan_context(batch))
    parent.mount('/batch', batch)

    with TestClient(parent):
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)

    assert [(r.label, r.member) for r in client.records()] == [(label, '1')]


# One Follow for all batches


def test_follow_turned_on_on_one_page_shows_on_the_other_which_batch_follows(
    server: Server, pages: Pages, manager: BatchManager
) -> None:
    other = pages.page(
        manager.create(STAGED.name, title='Other', source='pairs', columns=COLUMNS)
    )
    seen = server.version

    on = other.follow(True)

    shown = server.poll(seen, server.start)
    assert shown is not None
    assert shown.follow is not None
    assert (shown.follow.on, shown.follow.other) == (False, 'Other')
    assert on.follow is not None
    assert (on.follow.on, on.follow.other) == (True, None)
    off = server.follow(False)  # a page that does not follow stops nothing
    assert (off.notice, pages.follow.label) == ('', other.label)


def test_follow_resumes_after_a_restart(
    server: Server,
    label: str,
    client: Client,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    manager = BatchManager(
        client, sources=SOURCES, definitions=definitions, defaults=DEFAULTS
    )
    again = Pages(manager, Follow(manager, definitions))
    sample(1, 'water')

    again.tick()

    shown = done(again.page(label), client)
    assert shown.follow is not None
    assert shown.follow.on
    assert statuses(shown) == {'water': 'done'}


def test_a_tick_steps_the_batch_that_follows_without_a_page_open(
    pages: Pages, label: str, client: Client, sample: Measure
) -> None:
    pages.follow.on(label)
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [('scan', '1')]


def test_a_tick_after_follow_moved_steps_the_batch_that_follows_now(
    server: Server, pages: Pages, manager: BatchManager, client: Client, sample: Measure
) -> None:
    other = manager.create(STAGED.name, title='Other', source='pairs')
    pages.follow.on(other)
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [(other, '1')]


def test_the_failure_of_a_step_shows_on_the_page_of_the_batch_that_follows_only(
    server: Server, pages: Pages, manager: BatchManager, fake: FakeFiles
) -> None:
    other = pages.page(manager.create(STAGED.name, title='Other', source='pairs'))
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    pages.tick()

    following, shown = server.state().follow, other.state().follow
    assert following is not None
    assert following.failure == 'Follow could not step: half written'
    assert shown is not None
    assert shown.failure is None


def test_a_step_that_cannot_make_its_batch_is_a_failure_that_its_page_says(
    pages: Pages, label: str, fake: FakeFiles, sample: Measure
) -> None:
    pages.follow.on(label)
    sample(1, 'water')
    fake.measure(7, 1.0, role='transmission')  # the source needs a name

    pages.tick()

    assert pages.failure == "Follow could not step: 'sample'"
    with pytest.raises(Problem, match="Cannot show scan: 'sample'"):
        pages.page(label)


def test_a_correction_on_another_page_moves_the_version_of_the_runs_once(
    server: Server, pages: Pages, manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    other = pages.page(manager.create(STAGED.name, title='Other', source='pairs'))
    pages.tick()
    before = pages.runs_version

    run = other.runs().rows[0].key
    other.correct(CellEdit(keys=[run], column='sample', value='tea'))
    pages.tick()
    pages.tick()

    assert pages.runs_version == before + 1


def test_the_counts_of_the_batch_that_follows_are_the_chips_of_its_page(
    server: Server, pages: Pages, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.follow(True)
    done(server, client)
    sample(2, 'oil')
    bins(server, 0)
    pages.tick()  # Follow cannot submit oil with these settings

    counted = pages.counts().rows[server.label]

    chips = {s: n for s, n in server.state().counts.items() if n and s != 'all'}
    assert counted == chips == {'outdated': 1, 'incomplete': 1}


def test_follow_turning_itself_off_moves_the_version_of_the_list_of_batches(
    server: Server, pages: Pages, manager: BatchManager
) -> None:
    other = manager.create(STAGED.name, title='Other', source='pairs')
    pages.follow.on(other)
    manager.remove(other)
    seen = server.state()

    pages.tick()  # Follow finds its batch gone

    polled = server.poll(seen.version, server.start)
    assert pages.follow.label is None
    assert polled is not None
    assert polled.batches > seen.batches

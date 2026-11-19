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
from ess.apps.batch.web import Page, Pages, create_app
from ess.apps.batch.web.models import CellEdit, Cells, Edit, Next, Reduce, State
from ess.apps.batch.web.page import Problem
from ess.apps.testing import FakeFiles

from .toys import COLUMNS, SOURCES, STAGED, STAGED_NAMES, Gate, Measure

BINS = ['binning', 'bins']


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def pages(manager: BatchManager, files: CorrectedFiles) -> Pages:
    return Pages(manager, files=files, marks=Marks)


@pytest.fixture
def page(pages: Pages, label: str) -> Page:
    """The page of ``label``, the batch that follows."""
    pages.manager.follow(label)
    return pages.page(label)


def opened(pages: Pages, label: str) -> TestClient:
    """A client of the page of ``label``, which asks for ``api/...`` as it does."""
    return TestClient(create_app(pages), base_url=f'http://testserver/batches/{label}/')


def statuses(state: State) -> dict[str, str]:
    return {row.name: row.status for row in state.rows}


def bins(page: Page, value: int) -> None:
    """Set the number of bins in the form, and apply it."""
    page.edit_settings(Edit(path=BINS, op='set', value=value))
    page.apply_settings()


def done(page: Page, client: Client) -> State:
    client.wait(client.records())
    return page.state()


def test_a_step_shows_new_rows_and_runs_and_reduces_the_rows_that_arrived(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    before = page.state()
    sample(1, 'water')
    sample(2, 'oil', partner=False)

    gate.close()
    page.pages.tick()

    after = page.state()
    assert after.version > before.version
    assert page.pages.runs().version > before.runs  # type: ignore[operator]
    assert len(page.pages.runs().rows) == 3
    assert statuses(after) == {'water': 'running', 'oil': 'incomplete'}
    assert after.follow is not None
    assert after.follow.failure is None
    gate.open()
    assert statuses(done(page, client)) == {'water': 'done', 'oil': 'incomplete'}


def versions(page: Page) -> tuple[int, int]:
    return page.version, page.pages.runs_version


def correct_and_poll(page: Page) -> None:
    """Correct a run, and poll the page at once, as the page does after one."""
    run = page.pages.runs().rows[0].key
    page.pages.correct(CellEdit(keys=[run], column='sample', value='tea'))
    page.poll(page.version, page.start)


@pytest.mark.parametrize(
    'action',
    [
        lambda page: None,
        lambda page: page.scan(),
        lambda page: correct_and_poll(page),
    ],
    ids=['nothing', 'rescan', 'correction'],
)
def test_a_step_that_finds_nothing_new_changes_no_version(
    page: Page, client: Client, sample: Measure, action: Any
) -> None:
    sample(1, 'water')
    page.pages.tick()
    client.wait(client.records())
    page.pages.tick()  # sees the result that finished
    action(page)
    before = versions(page)

    page.pages.tick()
    page.pages.tick()

    assert versions(page) == before
    assert page.poll(before[0], page.start) is None


def changed_behind_the_page(page: Page, what: str, fake: FakeFiles) -> None:
    """Change one thing a step shows, and nothing else, but not by an action."""
    if what == 'runs':
        fake.measure(9, 1.0, role='background')
    elif what == 'rows':
        page.batch.rows = page.batch.rows.edit('1', {'thickness': 2.0})
    elif what == 'refusals':
        page.batch.settings = {**page.batch.settings, 'binning': {'bins': 0}}
    elif what == 'failure':
        fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))


@pytest.mark.parametrize('what', ['runs', 'rows', 'refusals', 'failure'])
def test_a_step_that_changes_one_thing_the_page_shows_tells_the_page(
    page: Page, client: Client, sample: Measure, fake: FakeFiles, what: str
) -> None:
    sample(1, 'water')
    page.follow(True)
    client.wait(client.records())
    page.pages.tick()
    seen = page.version

    changed_behind_the_page(page, what, fake)
    page.pages.tick()

    assert page.poll(seen, page.start) is not None


def test_a_step_after_a_result_finished_tells_the_page(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    page.pages.tick()
    running = page.state().version
    gate.open()
    client.wait(client.records())

    page.pages.tick()

    shown = page.poll(running, page.start)
    assert shown is not None
    assert statuses(shown) == {'water': 'done'}


class FinishingClient:
    """
    Delegates to a client, and lets the reductions in flight finish right after
    the first read of their status that says they run: a result that finishes
    just after the page looked.
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
        staged=STAGED_NAMES,
    )
    label = manager.create(STAGED.name, title='Scan', source='pairs', columns=COLUMNS)
    sample(1, 'water')
    pages = Pages(manager)
    pages.manager.follow(label)
    http = opened(pages, label)
    page = pages.page(label)
    gate.close()
    finishing.finishing = True

    page.pages.tick()

    shown = http.get('/api/state', params={'after': page.version, 'start': page.start})
    assert shown.status_code == 200
    assert {r['status'] for r in shown.json()['rows']} == {'done'}


def test_a_step_reduces_a_row_whose_cell_changed_at_a_column_of_at(
    page: Page, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    page.pages.tick()
    first = done(page, client).rows[0].record

    fake.measure(1005, 2.0, role='transmission', sample='water')
    page.pages.tick()

    assert done(page, client).rows[0].record != first


def test_a_run_that_a_step_finds_can_be_chosen_in_a_cell_without_a_runs_view(
    manager: BatchManager, label: str, sample: Measure
) -> None:
    pages = Pages(manager)
    pages.manager.follow(label)
    page = pages.page(label)
    page.pages.tick()
    sample(1, 'water', partner=False)  # a row that waits, so no record is made

    page.pages.tick()

    assert option(page, 'sample', 1) == 'run 1 (sample)'


def test_a_step_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.pages.tick()
    first = done(page, client).rows[0].record

    bins(page, 3)
    sample(2, 'oil')
    page.pages.tick()

    shown = {row.name: row for row in done(page, client).rows}
    assert (shown['water'].record, shown['water'].earlier) == (first, True)
    assert (shown['oil'].status, shown['oil'].earlier) == ('done', False)
    page.reduce(Reduce(which='earlier'))
    assert not done(page, client).rows[0].earlier


def test_a_row_the_step_could_not_submit_shows_why(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    bins(page, 0)

    page.follow(True)

    (row,) = page.state().rows
    assert row.status == 'incomplete'
    assert row.reason.startswith('Follow could not reduce it: ')
    assert 'greater than or equal to 1' in row.reason
    assert client.records() == []
    assert statuses(page.follow(False)) == {'water': 'ready'}


def test_apply_while_following_reduces_nothing_and_the_next_step_uses_it(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.follow(True)
    done(page, client)
    sample(2, 'oil')
    bins(page, 0)
    page.pages.tick()
    before = ids(client)

    page.edit_settings(Edit(path=BINS, op='set', value=3))
    applied = page.apply_settings()

    assert ids(client) == before
    oil = {row.name: row for row in applied.rows}['oil']
    assert (oil.status, oil.reason) == ('ready', '')
    page.pages.tick()
    shown = {row.name: row for row in done(page, client).rows}
    assert (shown['water'].earlier, shown['oil'].status) == (True, 'done')
    assert client.latest('scan', '2').request.params['binning'] == {'bins': 3}


def test_a_done_row_reads_outdated_while_follow_is_refused_the_settings(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.follow(True)
    done(page, client)
    sample(2, 'oil')
    bins(page, 0)

    page.pages.tick()

    shown = {row.name: row for row in page.state().rows}
    assert (shown['water'].status, shown['oil'].status) == ('outdated', 'incomplete')
    assert shown['oil'].reason.startswith('Follow could not reduce it: ')


def test_a_step_that_raises_is_said_and_the_next_step_runs(
    page: Page,
    client: Client,
    sample: Measure,
    fake: FakeFiles,
    files: CorrectedFiles,
) -> None:
    sample(1, 'water')
    nameless = fake.measure(7, 1.0, role='transmission')  # the source needs a name

    page.pages.tick()
    failed = page.state().follow
    files.correct(nameless, sample='oil')
    page.pages.tick()

    assert failed is not None
    assert failed.failure == "Follow could not step: 'sample'"
    assert page.state().follow.failure is None  # type: ignore[union-attr]
    assert statuses(done(page, client)) == {'water': 'done'}


def test_a_step_waits_for_a_request_that_holds_the_lock(
    page: Page, sample: Measure
) -> None:
    sample(1, 'water')
    stepped = threading.Event()

    with page.lock:
        thread = threading.Thread(target=lambda: (page.pages.tick(), stepped.set()))
        thread.start()
        assert not stepped.wait(0.2)
        assert page.state().rows == []
    thread.join()

    assert stepped.is_set()
    assert [row.name for row in page.state().rows] == ['water']


def test_turning_follow_on_steps_at_once_and_off_stops(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')

    gate.close()
    on = page.follow(True)
    sample(2, 'oil')
    off = page.follow(False)
    gate.open()

    assert on.follow is not None
    assert (on.follow.on, on.notice) == (True, 'Following.')
    assert statuses(on) == {'water': 'running'}
    assert (off.follow.on, off.notice) == (False, 'Stopped following.')  # type: ignore[union-attr]
    assert statuses(off) == {'water': 'running'}


def test_a_batch_without_a_source_offers_no_follow(
    pages: Pages, manager: BatchManager
) -> None:
    page = pages.page(manager.create(STAGED.name, title='By hand', columns=COLUMNS))

    assert page.state().follow is None
    with pytest.raises(Problem, match='no source'):
        page.follow(True)


def option(page: Page, column: str, run: int) -> str:
    """The label of the option of a run in the editor of ``column``."""
    (shown,) = (c for c in page.state().columns if c.name == column)
    assert shown.editor is not None
    return next(o for o in shown.editor.options if o.startswith(f'run {run}'))


def test_a_refusal_of_a_running_row_is_said_once_and_not_kept(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    page.follow(True)
    request = Reduce(which='selected', keys=['1'])

    with pytest.raises(Problem, match=r'Nothing was reduced: .*still running'):
        page.reduce(request)
    gate.open()
    after = done(page, client)

    assert statuses(after) == {'water': 'done'}
    assert after.refused == []


def ids(client: Client) -> set[str]:
    return {r.id for r in client.records()}


def test_follow_leaves_a_row_changed_by_hand_to_the_person(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    page.follow(True)
    done(page, client)
    before = ids(client)

    for column, run in [('sample', 2), ('transmission', 1002)]:
        value = option(page, column, run)
        edited = page.edit(CellEdit(keys=['1'], column=column, value=value))
        page.pages.tick()

    assert ids(client) == before
    assert statuses(edited) == {'water': 'outdated', 'oil': 'done'}
    assert edited.action.label == 'Reduce 1 outdated'
    page.reduce(Reduce(which='remaining'))
    page.pages.tick()
    assert len(ids(client) - before) == 1
    water, oil = (client.latest('scan', k).request.params for k in ('1', '2'))
    assert (water['sample'], water['transmission']) == (
        oil['sample'],
        oil['transmission'],
    )


@pytest.mark.parametrize('following', [True, False])
def test_a_row_set_by_hand_says_that_follow_leaves_it_to_the_person(
    page: Page, sample: Measure, following: bool
) -> None:
    sample(1, 'water')
    page.scan()
    page.edit(CellEdit(keys=['1'], column='thickness', value=2.0))

    shown = page.follow(following)

    (row,) = shown.rows
    assert row.status == 'ready'
    assert row.reason == (
        'Set by hand, so Follow leaves it to you: reduce it.' * following
    )
    assert shown.action.label == 'Reduce 1 remaining'


def test_a_row_a_person_reduced_after_an_edit_follows_the_files_again(
    page: Page, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    page.follow(True)
    done(page, client)
    other = option(page, 'sample', 2)
    page.edit(CellEdit(keys=['1'], column='sample', value=other))
    page.reduce(Reduce(which='remaining'))
    edited = done(page, client).rows[0].record

    fake.measure(1005, 2.0, role='transmission', sample='water')
    page.pages.tick()

    assert statuses(done(page, client)) == {'water': 'done', 'oil': 'done'}
    record = client.latest('scan', '1')
    assert record.id != edited
    assert (
        record.request.params['sample']
        == client.latest('scan', '2').request.params['sample']
    )


def test_a_cell_reverted_after_a_reduction_is_followed_at_the_next_step(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    page.follow(True)
    done(page, client)
    for column, run in [('sample', 2), ('transmission', 1002)]:
        value = option(page, column, run)
        page.edit(CellEdit(keys=['1'], column=column, value=value))
    page.reduce(Reduce(which='remaining'))
    done(page, client)

    page.revert(Cells(keys=['1'], column='sample'))
    page.pages.tick()

    water, oil = (client.latest('scan', k).request.params for k in ('1', '2'))
    assert water['sample'] != oil['sample']  # the scan's
    assert water['transmission'] == oil['transmission']  # still set by hand


def test_an_edit_not_reduced_leaves_its_row_to_the_person_when_a_file_arrives(
    page: Page, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    page.follow(True)
    done(page, client)
    page.edit(CellEdit(keys=['1'], column='thickness', value=2.0))
    before = ids(client)

    fake.measure(1005, 2.0, role='transmission', sample='water')
    page.pages.tick()

    (row,) = page.state().rows
    assert ids(client) == before
    assert (row.status, row.reason) == (
        'outdated',
        'Transmission and Thickness changed.'
        ' Set by hand, so Follow leaves it to you: reduce it.',
    )


@pytest.mark.parametrize('value', [3, None], ids=['changed', 'missing'])
def test_follow_reduces_with_the_batch_s_settings_whatever_the_form_holds(
    page: Page, client: Client, sample: Measure, value: int | None
) -> None:
    page.follow(True)
    page.edit_settings(Edit(path=BINS, op='set', value=value))
    sample(1, 'water')

    page.pages.tick()

    assert statuses(done(page, client)) == {'water': 'done'}
    assert client.latest('scan', '1').request.params['binning'] == {'bins': 2}


def test_a_step_that_raises_anywhere_is_said_and_follow_stays_on(
    page: Page, pages: Pages, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    on = page.follow(True)
    pages.tick()
    failed = page.state().follow
    fake.list = listed
    pages.tick()

    assert on.follow is not None
    assert (on.follow.on, on.notice) == (True, 'Following.')
    assert failed is not None
    assert 'half written' in (failed.failure or '')
    assert statuses(done(page, client)) == {'water': 'done'}


class Unsayable(Exception):
    """An exception that cannot be put in words."""

    def __str__(self) -> str:
        raise ValueError('no words')

    __repr__ = __str__


def test_a_tick_whose_step_raises_what_cannot_be_said_lets_the_next_tick_run(
    page: Page, pages: Pages, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(Unsayable())

    pages.tick()
    fake.list = listed
    pages.tick()

    assert statuses(done(page, client)) == {'water': 'done'}


def test_a_tick_does_nothing_while_not_following(
    page: Page, pages: Pages, sample: Measure
) -> None:
    pages.manager.follow(None)
    sample(1, 'water')

    pages.tick()

    assert page.state().rows == []


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
    page: Page, pages: Pages, client: Client, sample: Measure
) -> None:
    lock = WatchedLock()
    pages.lock = lock  # type: ignore[assignment]
    timer = threading.Thread(target=pages.tick)

    with pages.lock:  # a request that runs while the timer fires
        timer.start()
        assert lock.waited.wait(30)  # the tick waits for the lock
        sample(1, 'water')
        page.follow(False)
    timer.join()

    assert page.state().rows == []
    assert client.records() == []


def test_the_app_steps_while_following_and_stops_with_the_app(
    manager: BatchManager,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')
    manager.follow(label)

    with opened(Pages(manager, interval=0.01), label) as http:
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)
        state: dict[str, Any] = http.get('/api/state').json()

    assert state['follow']['on']
    assert [row['name'] for row in state['rows']] == ['water']
    assert not any(t.name == 'every' for t in threading.enumerate())


def test_a_parent_that_runs_the_lifespan_of_the_app_it_mounts_steps_follow(
    manager: BatchManager,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')
    manager.follow(label)
    batch = create_app(Pages(manager, interval=0.01))
    parent = FastAPI(lifespan=lambda _: batch.router.lifespan_context(batch))
    parent.mount('/batch', batch)

    with TestClient(parent):
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)

    assert [(r.label, r.member) for r in client.records()] == [(label, '1')]


# One Follow for all batches


def test_follow_turned_on_on_one_page_shows_on_the_other_which_batch_follows(
    page: Page, pages: Pages, manager: BatchManager
) -> None:
    other = pages.page(
        manager.create(STAGED.name, title='Other', source='pairs', columns=COLUMNS)
    )
    seen = page.version

    on = other.follow(True)

    shown = page.poll(seen, page.start)
    assert shown is not None
    assert shown.follow is not None
    assert (shown.follow.on, shown.follow.other) == (False, 'Other')
    assert on.follow is not None
    assert (on.follow.on, on.follow.other) == (True, None)
    off = page.follow(False)  # a page that does not follow stops nothing
    assert (off.notice, pages.manager.following) == ('', other.label)


def test_follow_resumes_after_a_restart(
    page: Page,
    label: str,
    client: Client,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    manager = BatchManager(
        client, sources=SOURCES, definitions=definitions, staged=STAGED_NAMES
    )
    again = Pages(manager)
    sample(1, 'water')

    again.tick()

    shown = done(again.page(label), client)
    assert shown.follow is not None
    assert shown.follow.on
    assert statuses(shown) == {'water': 'done'}


def test_a_tick_steps_the_batch_that_follows_without_a_page_open(
    pages: Pages, label: str, client: Client, sample: Measure
) -> None:
    pages.manager.follow(label)
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [('scan', '1')]


def test_a_tick_after_follow_moved_steps_the_batch_that_follows_now(
    page: Page, pages: Pages, manager: BatchManager, client: Client, sample: Measure
) -> None:
    other = manager.create(STAGED.name, title='Other', source='pairs')
    pages.manager.follow(other)
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [(other, '1')]


def test_the_failure_of_a_step_shows_on_the_page_of_the_batch_that_follows_only(
    page: Page, pages: Pages, manager: BatchManager, fake: FakeFiles
) -> None:
    other = pages.page(manager.create(STAGED.name, title='Other', source='pairs'))
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    pages.tick()

    following, shown = page.state().follow, other.state().follow
    assert following is not None
    assert following.failure == 'Follow could not step: half written'
    assert shown is not None
    assert shown.failure is None


def test_follow_stays_on_the_batch_that_ended_and_the_rows_that_moved_wait(
    page: Page, pages: Pages, manager: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    page.follow(True)
    done(page, client)

    made = page.start_next(Next(key='2', title='Afternoon')).label
    pages.tick()

    following = pages.page(made)
    assert manager.following == page.label
    assert statuses(following.state()) == {'oil': 'ready'}
    assert client.records(label=made) == []
    following.follow(True)
    assert statuses(done(following, client)) == {'oil': 'done'}


def test_why_follow_refused_rows_of_one_batch_is_not_said_on_the_next_that_follows(
    page: Page, pages: Pages, manager: BatchManager, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    bins(page, 0)
    refused = page.follow(True)  # Follow cannot submit water with these settings
    other = pages.page(
        manager.create(STAGED.name, title='Other', source='pairs', columns=COLUMNS)
    )
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    shown = other.follow(True)

    assert statuses(refused) == {'water': 'incomplete'}
    assert shown.follow is not None
    assert shown.follow.failure == 'Follow could not step: half written'
    assert statuses(shown) == {'water': 'ready'}


def test_a_step_that_cannot_make_its_batch_is_a_failure_that_its_page_says(
    pages: Pages, label: str, fake: FakeFiles, sample: Measure
) -> None:
    pages.manager.follow(label)
    sample(1, 'water')
    fake.measure(7, 1.0, role='transmission')  # the source needs a name

    pages.tick()

    assert pages.failure == "Follow could not step: 'sample'"
    with pytest.raises(Problem, match="Cannot show scan: 'sample'"):
        pages.page(label)


def test_a_correction_on_another_page_moves_the_version_of_the_runs_once(
    page: Page, pages: Pages, manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    other = pages.page(manager.create(STAGED.name, title='Other', source='pairs'))
    pages.tick()
    before = pages.runs_version

    run = pages.runs().rows[0].key
    pages.correct(CellEdit(keys=[run], column='sample', value='tea'))
    other.poll(other.version, other.start)
    pages.tick()
    pages.tick()

    assert pages.runs_version == before + 1


def test_the_counts_of_the_batch_that_follows_are_the_chips_of_its_page(
    page: Page, pages: Pages, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.follow(True)
    done(page, client)
    sample(2, 'oil')
    bins(page, 0)
    pages.tick()  # Follow cannot submit oil with these settings

    counted = pages.counts().rows[page.label]

    chips = {s: n for s, n in page.state().counts.items() if n and s != 'all'}
    assert counted == chips == {'outdated': 1, 'incomplete': 1}

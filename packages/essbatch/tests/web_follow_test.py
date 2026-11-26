# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Follow on the page of a batch: a step of the batch that follows adds the rows
of its source that are new and filled, and reduces the rows not reduced yet.
The tests call :meth:`Pages.tick` in place of the timer, except one, which
runs the app's timer.
"""

import functools
import threading
import time
from typing import Any

import pytest
from ess.batch import BatchManager, CorrectedFiles, FolderDefinitions, Intake
from ess.batch.web import Page, Pages, create_app
from ess.batch.web.api import every
from ess.batch.web.errors import Rejected
from ess.batch.web.models import CellEdit, Edit, NewBatch, Reduce, State
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ess.apps import Client, Status
from ess.apps.testing import FakeFiles

from . import toys
from .toys import SOURCES, STAGED, STAGED_NAMES, Gate, Measure

BINS = ['binning', 'bins']


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def pages(intake: Intake, files: CorrectedFiles) -> Pages:
    return Pages(intake, files=files, marks=Marks)


@pytest.fixture
def label(intake: Intake) -> str:
    """The batch ``scan`` of the conftest, made to follow."""
    return toys.from_pairs(intake, STAGED.name, title='Scan', label='scan', follow=True)


@pytest.fixture
def page(pages: Pages, label: str) -> Page:
    """The page of ``label``, the batch that follows."""
    return pages.page(label)


def opened(pages: Pages, label: str) -> TestClient:
    """A client of the page of ``label``, which asks for ``api/...`` as it does."""
    return TestClient(create_app(pages), base_url=f'http://testserver/batches/{label}/')


def statuses(state: State) -> dict[str, str]:
    return {row.name: row.status for row in state.rows}


def reasons(state: State) -> dict[str, str]:
    return {row.name: row.reason for row in state.rows}


def bins(page: Page, value: int) -> None:
    """Set the number of bins in the form, and apply it."""
    page.edit_settings(Edit(path=BINS, op='set', value=value))
    page.apply_settings()


def done(page: Page, client: Client) -> State:
    client.wait(client.records())
    return page.state()


def test_a_step_shows_new_rows_and_runs_and_reduces_them(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    before = page.state()
    sample(1, 'water')
    sample(2, 'oil', partner=False)

    gate.close()
    page.pages.tick()

    after = page.state()
    assert after.revision > before.revision
    assert page.pages.runs.shown().revision > before.runs  # type: ignore[operator]
    assert len(page.pages.runs.shown().rows) == 3
    # oil waits for its transmission run
    assert statuses(after) == {'water': 'running'}
    assert (after.follow.failure, after.follow.waiting) == (None, 1)
    gate.open()
    assert statuses(done(page, client)) == {'water': 'done'}


def test_a_row_that_waits_is_added_and_reduced_once_it_is_filled(
    page: Page, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'oil', partner=False)
    page.pages.tick()
    assert page.state().rows == []

    fake.measure(1001, 1.0, role='transmission', sample='oil')
    page.pages.tick()

    shown = done(page, client)
    assert (statuses(shown), shown.follow.waiting) == ({'oil': 'done'}, 0)


def revisions(page: Page) -> tuple[int, int]:
    return page.revision, page.pages.runs.revision


def correct_and_poll(page: Page) -> None:
    """Correct a run, and poll the page at once, as the page does after one."""
    run = page.pages.runs.shown().rows[0].key
    page.pages.runs.correct(CellEdit(keys=[run], column='sample', value='tea'))
    page.poll(page.revision, page.start)


@pytest.mark.parametrize(
    'action',
    [
        lambda page: None,
        lambda page: page.relist(),
        lambda page: correct_and_poll(page),
    ],
    ids=['nothing', 'rescan', 'correction'],
)
def test_a_step_that_finds_nothing_new_changes_no_revision(
    page: Page, client: Client, sample: Measure, action: Any
) -> None:
    sample(1, 'water')
    page.pages.tick()
    client.wait(client.records())
    page.pages.tick()  # sees the result that finished
    action(page)
    before = revisions(page)

    page.pages.tick()
    page.pages.tick()

    assert revisions(page) == before
    assert page.poll(before[0], page.start) is None


def changed_behind_the_page(page: Page, what: str, fake: FakeFiles) -> None:
    """Change one thing a step shows, and nothing else, but not by an action."""
    if what == 'runs':
        fake.measure(9, 1.0, role='background')
    elif what == 'rows':
        toys.edit(page.batch, '1', thickness=2.0)
    elif what == 'refusals':
        page.batch.settings = {**page.batch.settings, 'binning': {'bins': 0}}
    elif what == 'failure':
        fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))


@pytest.mark.parametrize('what', ['runs', 'rows', 'refusals', 'failure'])
def test_a_step_that_changes_one_thing_the_page_shows_tells_the_page(
    page: Page, client: Client, sample: Measure, fake: FakeFiles, what: str
) -> None:
    sample(1, 'water')
    if what == 'refusals':
        # A row without a result that a step refuses with refused settings: a
        # cell that the workspace refuses when it resolves the row makes it
        # incomplete, but it is ready as given, and no cell is resolved while
        # the settings are refused.
        sample(2, 'oil')
        page.batch.put(toys.rows(client))
        page.edit(CellEdit(keys=['2'], column='thickness', value=-1.0))
    page.pages.tick()
    client.wait(client.records())
    page.pages.tick()
    seen = page.revision

    changed_behind_the_page(page, what, fake)
    page.pages.tick()

    assert page.poll(seen, page.start) is not None


def test_a_step_after_a_result_finished_tells_the_page(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    page.pages.tick()
    running = page.state().revision
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
        definitions=definitions,
        staged=STAGED_NAMES,
    )
    intake = Intake(manager, sources=SOURCES)
    label = toys.from_pairs(intake, STAGED.name, title='Scan', follow=True)
    sample(1, 'water')
    pages = Pages(intake)
    http = opened(pages, label)
    page = pages.page(label)
    gate.close()
    finishing.finishing = True

    page.pages.tick()

    shown = http.get('/api/state', params={'after': page.revision, 'start': page.start})
    assert shown.status_code == 200
    assert {r['status'] for r in shown.json()['rows']} == {'done'}


def test_a_step_leaves_a_done_row_that_a_person_changed_to_the_person(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.pages.tick()
    done(page, client)
    before = ids(client)

    page.edit(CellEdit(keys=['1'], column='thickness', value=2.0))
    page.pages.tick()

    shown = done(page, client)
    (row,) = shown.rows
    assert ids(client) == before
    assert (row.status, row.reason) == ('outdated', 'Thickness changed.')
    assert shown.action.label == 'Reduce 1 outdated'


def test_a_step_changes_no_row_that_the_table_has(
    page: Page, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    page.pages.tick()
    before = done(page, client).rows

    fake.measure(1005, 2.0, role='transmission', sample='water')
    page.pages.tick()

    assert done(page, client).rows == before


def test_a_run_that_a_step_finds_can_be_chosen_in_a_cell_without_a_runs_view(
    intake: Intake, label: str, sample: Measure
) -> None:
    pages = Pages(intake)
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
    assert (shown['water'].record, shown['water'].status) == (first, 'settings changed')
    assert shown['oil'].status == 'done'
    page.reduce(Reduce(which='settings changed'))
    assert done(page, client).rows[0].status == 'done'


def test_a_row_the_step_could_not_submit_shows_why(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    bins(page, 0)

    page.pages.tick()

    (row,) = page.state().rows
    assert row.status == 'ready'
    assert row.reason.startswith('Follow could not reduce it: ')
    assert 'greater than or equal to 1' in row.reason
    assert client.records() == []
    (stopped,) = page.stop_following().rows
    assert (stopped.status, stopped.reason) == ('ready', '')


def test_apply_while_following_reduces_nothing_and_the_next_step_uses_it(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.pages.tick()
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
    assert (shown['water'].status, shown['oil'].status) == ('settings changed', 'done')
    assert client.latest('scan', '2').request.params['binning'] == {'bins': 3}


def test_a_done_row_reads_outdated_while_follow_is_refused_the_settings(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.pages.tick()
    done(page, client)
    sample(2, 'oil')
    bins(page, 0)

    page.pages.tick()

    shown = {row.name: row for row in page.state().rows}
    assert (shown['water'].status, shown['oil'].status) == ('outdated', 'ready')
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
    assert failed.failure == "Follow could not step: KeyError: 'sample'"
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


def test_stop_following_ends_the_steps_and_leaves_the_rows_running(
    page: Page, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    page.pages.tick()
    sample(2, 'oil')

    off = page.stop_following()
    page.pages.tick()
    gate.open()

    assert (off.follow.on, off.notice) == (False, 'Stopped following.')
    assert statuses(off) == {'water': 'running'}
    assert statuses(done(page, client)) == {'water': 'done'}


def test_a_page_that_does_not_follow_stops_nothing(
    page: Page, pages: Pages, intake: Intake
) -> None:
    other = pages.page(intake.manager.create(STAGED.name, title='By hand').label)

    stopped = other.stop_following()

    assert (stopped.notice, intake.following) == ('', page.label)
    assert (stopped.follow.on, stopped.follow.source) == (False, None)
    assert page.state().follow.source == 'pairs'


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
    page.pages.tick()
    request = Reduce(which='selected', keys=['1'])

    with pytest.raises(Rejected, match=r'Nothing was reduced: .*still running'):
        page.reduce(request)
    gate.open()
    after = done(page, client)

    assert statuses(after) == {'water': 'done'}
    assert after.submit_errors == []


def ids(client: Client) -> set[str]:
    return {r.id for r in client.records()}


def test_a_row_a_person_put_without_a_result_is_reduced_by_the_next_step(
    page: Page, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.batch.put(toys.rows(client))
    page.edit(CellEdit(keys=['1'], column='thickness', value=2.0))

    page.pages.tick()

    assert statuses(done(page, client)) == {'water': 'done'}
    assert client.latest('scan', '1').request.params['thickness'] == 2.0


@pytest.mark.parametrize('value', [3, None], ids=['changed', 'missing'])
def test_follow_reduces_with_the_batch_s_settings_whatever_the_form_holds(
    page: Page, client: Client, sample: Measure, value: int | None
) -> None:
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

    pages.tick()
    failed = page.state().follow
    fake.list = listed
    pages.tick()

    assert failed.on
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
    pages.intake.stop()
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
        page.stop_following()
    timer.join()

    assert page.state().rows == []
    assert client.records() == []


def test_the_app_steps_while_following_and_stops_with_the_app(
    intake: Intake,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')

    with opened(Pages(intake, interval=0.01), label) as http:
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)
        state: dict[str, Any] = http.get('/api/state').json()

    assert state['follow']['on']
    assert [row['name'] for row in state['rows']] == ['water']
    assert not any(t.name == 'every' for t in threading.enumerate())


def test_a_parent_that_runs_the_lifespan_of_the_app_it_mounts_steps_follow(
    intake: Intake,
    label: str,
    sample: Measure,
    client: Client,
) -> None:
    sample(1, 'water')
    batch = create_app(Pages(intake, interval=0.01))
    parent = FastAPI(lifespan=lambda _: batch.router.lifespan_context(batch))
    parent.mount('/batch', batch)

    with TestClient(parent):
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)

    assert [(r.label, r.member) for r in client.records()] == [(label, '1')]


# One Follow for all batches


def test_a_batch_made_to_follow_ends_follow_on_the_page_of_the_one_before(
    page: Page, pages: Pages
) -> None:
    seen = page.revision

    made = pages.create(
        NewBatch(title='Other', spec=STAGED.name, source='pairs', follow=True)
    )

    shown = page.poll(seen, page.start)
    assert shown is not None
    assert shown.follow.on is False
    assert pages.page(made.label).state().follow.on
    assert pages.intake.following == made.label


def test_follow_resumes_after_a_restart(
    page: Page,
    label: str,
    client: Client,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    manager = BatchManager(client, definitions=definitions, staged=STAGED_NAMES)
    again = Pages(Intake(manager, sources=SOURCES))
    sample(1, 'water')

    again.tick()

    shown = done(again.page(label), client)
    assert shown.follow is not None
    assert shown.follow.on
    assert statuses(shown) == {'water': 'done'}


def test_a_tick_steps_the_batch_that_follows_without_a_page_open(
    pages: Pages, label: str, client: Client, sample: Measure
) -> None:
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [('scan', '1')]


def test_a_tick_after_follow_moved_steps_the_batch_that_follows_now(
    page: Page, pages: Pages, intake: Intake, client: Client, sample: Measure
) -> None:
    other = toys.from_pairs(intake, STAGED.name, title='Other', follow=True)
    sample(1, 'water')

    pages.tick()

    client.wait(client.records())
    assert [(r.label, r.member) for r in client.records()] == [(other, '1')]


def test_metadata_that_the_source_refuses_at_a_step_is_said_at_its_fields(
    page: Page, pages: Pages, fake: FakeFiles
) -> None:
    error = toys.refused_metadata()
    fake.list = lambda selector: (_ for _ in ()).throw(error)

    pages.tick()

    following = page.state().follow
    assert following is not None
    assert following.failure == (
        f'Follow could not step: the source pairs: {toys.REFUSED_METADATA}'
    )


def test_the_failure_of_a_step_shows_on_the_page_of_the_batch_that_follows_only(
    page: Page, pages: Pages, intake: Intake, fake: FakeFiles
) -> None:
    other = pages.page(intake.create(STAGED.name, title='Other', source='pairs').label)
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    pages.tick()

    following, shown = page.state().follow, other.state().follow
    assert following is not None
    assert following.failure == 'Follow could not step: half written'
    assert shown is not None
    assert shown.failure is None


@pytest.fixture
def unmarked(intake: Intake, label: str) -> tuple[Pages, Page]:
    """
    Pages without a Runs view, and the page of a batch that does not follow,
    once a step on ``label`` has run.
    """
    pages = Pages(intake)
    other = pages.page(intake.create(STAGED.name, title='Other', source='pairs').label)
    pages.tick()
    return pages, other


def test_a_run_that_a_step_finds_reaches_the_pickers_of_another_page(
    unmarked: tuple[Pages, Page], sample: Measure
) -> None:
    pages, other = unmarked
    seen = other.revision
    sample(1, 'water', partner=False)  # a row that waits, so no record is made

    pages.tick()

    shown = other.poll(seen, other.start)
    assert shown is not None
    (column,) = (c for c in shown.columns if c.name == 'sample')
    assert column.editor is not None
    assert 'run 1 (sample)' in column.editor.options


def test_files_that_a_step_cannot_list_are_said_on_another_page_until_listed(
    unmarked: tuple[Pages, Page], fake: FakeFiles
) -> None:
    pages, other = unmarked
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    pages.tick()
    failed = other.poll(other.revision, other.start)
    fake.list = listed
    pages.tick()
    listed_again = other.poll(other.revision, other.start)

    assert failed is not None
    assert failed.files_problem == 'The files cannot be listed: half written'
    assert listed_again is not None
    assert listed_again.files_problem is None


def test_the_failure_of_the_batch_that_followed_is_not_said_on_the_next_that_follows(
    page: Page, pages: Pages, fake: FakeFiles
) -> None:
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))
    pages.tick()
    fake.list = listed

    new = NewBatch(title='Next', spec=STAGED.name, source='pairs', follow=True)
    made = pages.page(pages.create(new).label)

    assert page.state().follow.on is False
    assert made.state().follow.failure is None


@pytest.mark.parametrize('made', ['on the page', 'in code'])
def test_why_follow_refused_rows_of_one_batch_is_not_said_on_the_next_that_follows(
    page: Page,
    pages: Pages,
    intake: Intake,
    sample: Measure,
    fake: FakeFiles,
    client: Client,
    made: str,
) -> None:
    sample(1, 'water')
    bins(page, 0)
    pages.tick()  # Follow cannot submit water with these settings
    refused = page.state()

    if made == 'on the page':
        new = NewBatch(title='Other', spec=STAGED.name, source='pairs', follow=True)
        other = pages.page(pages.create(new).label)
    else:
        other = pages.page(
            toys.from_pairs(intake, STAGED.name, title='Other', follow=True)
        )
    # No step runs on the other batch: its first step raises.
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))
    pages.tick()

    assert reasons(refused)['water'].startswith('Follow could not reduce it: ')
    assert reasons(other.state()) == {'water': ''}


def test_a_correction_on_another_page_moves_the_version_of_the_runs_once(
    page: Page, pages: Pages, intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')
    other = pages.page(intake.create(STAGED.name, title='Other', source='pairs').label)
    pages.tick()
    before = pages.runs.revision

    run = pages.runs.shown().rows[0].key
    pages.runs.correct(CellEdit(keys=[run], column='sample', value='tea'))
    other.poll(other.revision, other.start)
    pages.tick()
    pages.tick()

    assert pages.runs.revision == before + 1


def test_the_counts_of_the_batch_that_follows_are_the_chips_of_its_page(
    page: Page, pages: Pages, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    page.pages.tick()
    done(page, client)
    sample(2, 'oil')
    bins(page, 0)
    pages.tick()  # Follow cannot submit oil with these settings

    counted = pages.counts().rows[page.label]

    chips = {s: n for s, n in page.state().counts.items() if n and s != 'all'}
    assert counted == chips == {'outdated': 1, 'ready': 1}


def test_every_calls_the_actions_in_turn_until_the_block_ends() -> None:
    called: list[int] = []
    all_called = threading.Event()

    def call(i: int) -> None:
        called.append(i)
        if i == 2:
            all_called.set()

    with every(0, [functools.partial(call, i) for i in range(3)]):
        assert all_called.wait(10)
    with every(60, [functools.partial(call, 3)]):
        pass

    assert called == [0, 1, 2]

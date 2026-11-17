# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Follow on the batch page: a step of the server scans as "Rescan runs" does, and
reduces the rows that arrived. The tests call :meth:`Server.step` in place of
the timer, except one, which runs the app's timer.
"""

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ess.apps import Client
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import Server, create_app
from ess.apps.batch.web.models import CellEdit, Edit, Reduce, State
from ess.apps.testing import FakeFiles

from .toys import Gate, Measure

AT = frozenset({'sample', 'transmission'})
BINS = ['binning', 'bins']


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def server(batch: Batch, files: CorrectedFiles) -> Server:
    return Server(batch, name='name', files=files, marks=Marks, at=AT)


def statuses(state: State) -> dict[str, str]:
    return {row.name: row.status for row in state.rows}


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
    server.step()

    after = server.state()
    assert after.version > before.version
    assert server.runs().version > before.runs  # type: ignore[operator]
    assert len(server.runs().rows) == 3
    assert statuses(after) == {'water': 'running', 'oil': 'incomplete'}
    assert after.follow is not None
    assert after.follow.stepped is not None
    gate.open()
    assert statuses(done(server, client)) == {'water': 'done', 'oil': 'incomplete'}


def test_a_step_that_finds_nothing_new_changes_no_version(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.step()
    client.wait(client.records())
    server.step()  # sees the result that finished
    before = (server.state().version, server.runs().version)

    server.step()
    server.step()

    assert (server.state().version, server.runs().version) == before
    assert server.poll(before[0], server.start) is None


def test_a_step_after_a_result_finished_tells_the_page(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    gate.close()
    server.step()
    running = server.state().version
    gate.open()
    client.wait(client.records())

    server.step()

    shown = server.poll(running, server.start)
    assert shown is not None
    assert statuses(shown) == {'water': 'done'}


def test_a_step_reduces_a_row_whose_cell_changed_at_a_column_of_at(
    server: Server, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    server.step()
    first = done(server, client).rows[0].record

    fake.measure(1005, 2.0, role='transmission', sample='water')
    server.step()

    assert done(server, client).rows[0].record != first


def test_a_step_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.step()
    first = done(server, client).rows[0].record

    server.edit_settings(Edit(path=BINS, op='set', value=3))
    sample(2, 'oil')
    server.step()

    shown = {row.name: row for row in done(server, client).rows}
    assert (shown['water'].record, shown['water'].earlier) == (first, True)
    assert (shown['oil'].status, shown['oil'].earlier) == ('done', False)
    server.reduce(Reduce(which='earlier'))
    assert not done(server, client).rows[0].earlier


def test_a_row_the_step_could_not_submit_shows_why(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.edit_settings(Edit(path=BINS, op='set', value=0))

    server.follow(True)

    (row,) = server.state().rows
    assert row.status == 'incomplete'
    assert row.reason.startswith('Follow could not reduce it: ')
    assert 'greater than or equal to 1' in row.reason
    assert client.records() == []
    assert statuses(server.follow(False)) == {'water': 'ready'}


def test_a_done_row_reads_outdated_while_follow_is_refused_the_settings(
    server: Server, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    server.follow(True)
    done(server, client)
    sample(2, 'oil')
    server.edit_settings(Edit(path=BINS, op='set', value=0))

    server.step()

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

    server.step()
    failed = server.state().follow
    files.correct(nameless, sample='oil')
    server.step()

    assert failed is not None
    assert failed.failure == "The scan failed: 'sample'"
    assert server.state().follow.failure is None  # type: ignore[union-attr]
    assert statuses(done(server, client)) == {'water': 'done'}


def test_a_step_waits_for_a_request_that_holds_the_lock(
    server: Server, sample: Measure
) -> None:
    sample(1, 'water')
    stepped = threading.Event()

    with server.lock:
        thread = threading.Thread(target=lambda: (server.step(), stepped.set()))
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


def test_a_page_without_at_does_not_follow(batch: Batch) -> None:
    server = Server(batch)

    assert server.state().follow is None
    with pytest.raises(ValueError, match='at'):
        Server(batch, follow=True)


def option(server: Server, column: str, run: int) -> str:
    """The label of the option of a run in the editor of ``column``."""
    (shown,) = (c for c in server.state().columns if c.name == column)
    assert shown.editor is not None
    return next(o for o in shown.editor.options if o.startswith(f'run {run}'))


def test_an_edit_at_a_column_of_at_is_reduced_at_the_next_step(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    server.step()
    done(server, client)

    other = option(server, 'transmission', 1002)
    server.edit(CellEdit(keys=['1'], column='transmission', value=other))
    server.edit(CellEdit(keys=['2'], column='thickness', value=2.0))
    gate.close()
    server.step()

    assert statuses(server.state()) == {'water': 'running', 'oil': 'outdated'}
    gate.open()


def test_a_tweak_at_a_column_of_at_says_that_follow_reduces_it_again(
    server: Server, client: Client, sample: Measure, gate: Gate
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    server.follow(True)
    done(server, client)
    other = option(server, 'transmission', 1002)
    server.edit_tweak('1', Edit(path=['transmission'], op='set', value=other))

    gate.close()
    notice = server.submit_tweak('1').notice
    gate.open()

    assert notice == (
        'Reducing water alone. Its Transmission differs from its cells, so Follow'
        ' reduces it again with its cells once this is done.'
    )


def test_the_app_steps_while_following_and_stops_with_the_app(
    batch: Batch, sample: Measure, client: Client
) -> None:
    sample(1, 'water')
    app = create_app(batch, name='name', at=AT, follow=True, interval=0.01)

    with TestClient(app) as http:
        give_up = time.monotonic() + 10
        while not client.records() and time.monotonic() < give_up:
            time.sleep(0.01)
        state: dict[str, Any] = http.get('/api/state').json()

    assert state['follow']['on']
    assert [row['name'] for row in state['rows']] == ['water']
    assert not any(t.name == 'follow' for t in threading.enumerate())

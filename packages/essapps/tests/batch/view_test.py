# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The tab through its widgets and actions, on a real in-process workspace."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pandas as pd
import panel as pn
import pytest
from pydantic import BaseModel

from ess.apps import Client, Listing, Record, Template
from ess.apps.batch import Batch, CorrectedFiles, cli
from ess.apps.batch.frames import RESULT, read_table, rows_of, write_table
from ess.apps.batch.panel import BatchView, RunSheet
from ess.apps.batch.panel.demo import build, open_demo
from ess.apps.batch.panel.view import Draw
from ess.apps.batch.text import write_template
from ess.apps.panel import App
from ess.apps.testing import FakeFiles

from .toys import COLUMNS, STAGED, Gate, Measure, Params

pn.extension('tabulator')

SETTINGS = {'binning': {'bins': 2}}


def rows_shown(view: BatchView) -> dict[str, dict[str, str]]:
    frame = view.table.widget.value
    return {row['Row']: dict(row) for row in frame.to_dict('records')}


def results(view: BatchView) -> dict[str, str]:
    return {k: row['Result'] for k, row in rows_shown(view).items()}


def view_of(batch: Batch, draw: Draw | None = None) -> BatchView:
    """A view of ``batch`` after a scan, with :data:`SETTINGS`."""
    view = BatchView(batch, info=('name',), draw=draw)
    view.scan()
    view.settings.set(SETTINGS, Listing(batch.client))
    return view


@pytest.fixture
def measured(sample: Measure) -> None:
    """Rows 1 and 2 with a transmission run, row 3 without one."""
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)


@pytest.fixture
def view(batch: Batch, measured: None) -> BatchView:
    return view_of(batch)


def done(view: BatchView, client: Client) -> None:
    client.wait(client.records(label=view.batch.label))
    view.refresh(Listing(client))


@pytest.fixture
def reduced(view: BatchView, client: Client) -> BatchView:
    view.reduce_all()
    done(view, client)
    return view


def test_a_scan_shows_the_rows_with_their_info_and_labels(view: BatchView) -> None:
    shown = rows_shown(view)

    assert shown['1']['name'] == 'water'
    assert shown['1']['Sample'] == 'run 1'
    assert shown['3']['Transmission'] == ''
    assert results(view) == {'1': 'not reduced', '2': 'not reduced', '3': 'incomplete'}


def test_reduce_all_runs_the_ready_rows_and_says_so(
    view: BatchView, client: Client
) -> None:
    records = view.reduce_all()
    done(view, client)

    assert sorted(records) == ['1', '2']
    assert results(view) == {'1': 'completed', '2': 'completed', '3': 'incomplete'}
    assert view.message.object == 'Submitted 2 rows.'
    assert view.summary.object == '2 completed, 1 incomplete'


def test_settings_refused_are_named_in_the_message(
    view: BatchView, client: Client
) -> None:
    view.settings.set({'binning': {'bins': 0}}, Listing(client))

    assert view.reduce_all() == {}
    assert 'Refused' in view.message.object
    assert 'binning' in view.message.object


def test_reduce_selected_names_the_empty_cell_of_a_row_picked(
    view: BatchView,
) -> None:
    view.table.select_keys(['3'])

    assert view.reduce_selected() == {}
    assert '3: transmission: empty' in view.message.object


def test_reduce_remaining_runs_only_what_is_left(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['1'])
    view.reduce_selected()
    done(view, client)

    assert sorted(view.reduce_remaining()) == ['2']


def test_cancel_shows_as_cancelled(view: BatchView, client: Client, gate: Gate) -> None:
    gate.close()
    view.reduce_all()

    view.cancel()
    gate.open()
    done(view, client)

    assert results(view) == {'1': 'cancelled', '2': 'cancelled', '3': 'incomplete'}


def test_updating_writes_only_the_cell_changed(view: BatchView, client: Client) -> None:
    view.table.select_keys(['1'])

    view.row.set({**view.batch.rows['1'].values, 'thickness': 3.0}, Listing(client))
    view.update()

    assert view.batch.rows.edits == {'1': {'thickness': 3.0}}


def test_a_row_without_a_partner_can_be_updated(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['3'])

    view.row.set({**view.batch.rows['3'].values, 'thickness': 3.0}, Listing(client))
    view.update()

    assert view.batch.rows['3'].values['thickness'] == 3.0


def test_updating_several_rows_sets_the_cell_of_each(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['1', '2'])

    view.row.set({'thickness': 5.0}, Listing(client))
    view.update()

    assert view.batch.rows['1'].values['thickness'] == 5.0
    assert view.batch.rows['2'].values['thickness'] == 5.0
    assert 'sample' in view.batch.rows['2'].values


def test_a_status_change_does_not_reload_the_editor(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['1'])
    view.row.set({**view.batch.rows['1'].values, 'thickness': 3.0}, Listing(client))
    view.reduce_selected()

    done(view, client)

    assert view.row.held()[0]['thickness'] == 3.0


def test_removing_and_undoing(view: BatchView) -> None:
    view.table.select_keys(['1'])
    view.remove()
    assert '1' not in rows_shown(view)

    view.restore()
    assert '1' in rows_shown(view)


def test_undoing_the_edits_of_a_row(view: BatchView, client: Client) -> None:
    view.table.select_keys(['1'])
    view.row.set({**view.batch.rows['1'].values, 'thickness': 3.0}, Listing(client))
    view.update()

    view.reset()

    assert 'thickness' not in view.batch.rows['1'].values


def test_adding_a_row_by_name(view: BatchView) -> None:
    view.key.value = 'mine'
    view.add()

    assert results(view)['mine'] == 'incomplete'


def test_adding_a_name_that_is_there_is_refused_in_the_message(view: BatchView) -> None:
    view.key.value = '1'
    view.add()

    assert 'a row has this key' in view.message.object


def test_the_filter_shows_only_rows_of_that_state(view: BatchView) -> None:
    view.filter.value = 'incomplete'

    assert list(rows_shown(view)) == ['3']
    assert view.summary.object == '1 incomplete, 2 not reduced'


def test_a_failed_row_says_why_when_selected(view: BatchView, client: Client) -> None:
    view.settings.set({'binning': {'bins': 1}}, Listing(client))
    view.reduce_all()
    done(view, client)

    view.table.select_keys(['1'])

    assert results(view)['1'] == 'failed'
    assert 'one bin' in view.details.object


def test_draw_gets_only_completed_results_of_the_selection(
    batch: Batch, client: Client, measured: None
) -> None:
    drawn: list[list[str]] = []

    def draw(records: Mapping[str, Record]) -> Any:
        drawn.append(sorted(records))
        return pn.pane.Markdown('drawn')

    view = view_of(batch, draw)
    view.table.select_keys(['2'])
    view.reduce_selected()
    done(view, client)

    view.table.select_keys(['1', '2'])
    assert drawn == [['2']]
    assert len(view.view.objects) == 1


def test_use_settings_shows_those_of_the_selected_result(
    reduced: BatchView, client: Client
) -> None:
    reduced.settings.set({'binning': {'bins': 9}}, Listing(client))

    reduced.table.select_keys(['1'])
    reduced.use_settings()

    assert reduced.settings.params['binning'] == {'bins': 2}


def test_a_tweak_becomes_the_result_of_its_row(
    reduced: BatchView, client: Client
) -> None:
    reduced.table.select_keys(['1'])
    params = dict(reduced.tweak.params)
    params['thickness'] = 4.0
    reduced.tweak.set(params, Listing(client))

    record = reduced.submit_tweak()
    done(reduced, client)

    assert reduced.batch.state()['1'].record == record
    assert results(reduced)['1'] == 'completed'


def test_a_cell_with_something_missing_is_kept_and_named(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['1'])
    kept = view.batch.rows['1'].values['sample']
    cells = {k: v for k, v in view.batch.rows['1'].values.items() if k != 'sample'}

    view.row.set({**cells, 'thickness': 3.0}, Listing(client))
    view.update()

    assert view.batch.rows['1'].values['sample'] == kept
    assert view.batch.rows['1'].values['thickness'] == 3.0
    assert 'Not written' in view.message.object
    assert 'sample' in view.message.object


def test_a_column_chosen_is_written_to_all_selected_even_at_its_default(
    view: BatchView,
) -> None:
    view.batch.rows = view.batch.rows.edit('1', {'thickness': 2.0}).edit(
        '2', {'thickness': 3.0}
    )
    view.table.select_keys(['1', '2'])

    view.write.value = 'thickness'
    view.update()

    assert view.batch.rows['1'].values['thickness'] == 1.0
    assert view.batch.rows['2'].values['thickness'] == 1.0
    assert view.write.value == ''


def test_changing_a_setting_shows_the_results_as_stale(reduced: BatchView) -> None:
    reduced.settings.panel().select(pn.widgets.IntInput)[0].value = 3

    assert results(reduced) == {'1': 'stale', '2': 'stale', '3': 'incomplete'}


def test_nothing_changed_says_so(view: BatchView) -> None:
    view.table.select_keys(['1', '2'])

    view.update()

    assert 'No cell changed' in view.message.object


def test_a_tweak_starts_from_the_rows_cells_and_its_latest_result(
    view: BatchView, client: Client
) -> None:
    view.table.select_keys(['1'])
    view.reduce_selected()
    done(view, client)
    view.batch.rows = view.batch.rows.edit('1', {'thickness': 7.0})
    view.refresh(Listing(client))
    assert view.tweak.params['thickness'] == 7.0

    view.settings.set({'binning': {'bins': 3}}, Listing(client))
    view.reduce_selected()
    done(view, client)

    assert view.tweak.params['binning'] == {'bins': 3}


def test_a_row_the_editor_cannot_show_still_says_why(view: BatchView) -> None:
    view.batch.rows = view.batch.rows.edit('3', {'bogus': 1})
    view.refresh(Listing(view.batch.client))

    view.table.select_keys(['3'])

    assert 'cannot show' in view.message.object
    assert 'not a column' in view.details.object


def test_a_source_that_raises_says_so_in_the_message(client: Client) -> None:
    def broken(_: Any) -> list[Any]:
        raise KeyError('sample')

    batch = Batch(client, STAGED, columns=COLUMNS, label='x', source=broken)
    view = BatchView(batch)

    view.scan()

    assert 'The scan failed' in view.message.object


def test_a_drawing_that_raises_says_so_in_its_place(
    batch: Batch, client: Client, measured: None
) -> None:
    def draw(_: Any) -> Any:
        raise RuntimeError('released')

    view = view_of(batch, draw)
    view.table.select_keys(['1'])
    view.reduce_selected()
    done(view, client)

    assert 'Cannot draw' in view.view.objects[0].object


def test_a_scan_offers_new_files_in_the_editor(
    view: BatchView, client: Client, fake: FakeFiles
) -> None:
    run = fake.measure(9, 9.0, role='sample', sample='new')

    view.scan()

    pickers = view.row.panel().select(pn.widgets.Select)
    assert any(run in picker.values for picker in pickers)


class Marks(BaseModel):
    role: str | None = None


def test_a_correction_on_the_run_sheet_reaches_the_rows(
    batch: Batch,
    client: Client,
    files: CorrectedFiles,
    fake: FakeFiles,
    sample: Measure,
) -> None:
    sample(1, 'water', partner=False)
    partner = fake.measure(2, 1.0, role='sample', sample='water')
    view = BatchView(batch, info=('name',))
    view.scan()
    sheet = RunSheet(files, client, Marks, then=view.scan)

    sheet.table.select_keys([partner])
    sheet.form.set({'role': 'transmission'}, Listing(client))
    sheet.correct()

    assert results(view) == {'1': 'not reduced'}
    frame = sheet.table.widget.value
    assert frame.set_index('File').loc['run 2', 'Role'] == 'transmission'


def test_a_field_chosen_on_the_run_sheet_is_written_at_its_default(
    client: Client, files: CorrectedFiles, fake: FakeFiles
) -> None:
    runs = [fake.measure(n, 1.0, role='sample') for n in (1, 2)]
    sheet = RunSheet(files, client, Marks)
    sheet.table.select_keys(runs)

    sheet.write.value = 'role'
    sheet.correct()

    assert [files.metadata(r)['role'] for r in runs] == [None, None]


def test_the_app_shows_its_tabs_first_and_refreshes_them(
    view: BatchView, client: Client, batch: Batch
) -> None:
    app = App(client, tabs={'Batch': view})

    assert view.table.widget in app.panel()[0].objects
    batch.reduce(view.settings.params, ['1'])
    client.wait(client.records(label='scan'))
    app.poll()
    assert results(view)['1'] == 'completed'


def demo_view(directory: Path, samples: int = 4) -> BatchView:
    """The batch tab of the demo on ``directory``; close its client."""
    view = build(*open_demo(directory, samples)).tabs['Batch']
    assert isinstance(view, BatchView)
    return view


def test_the_demo_builds_and_reduces(tmp_path: Path) -> None:
    view = demo_view(tmp_path)

    with view.batch.client as client:
        records = view.reduce_all()
        assert set(client.wait(records).values()) == {'completed'}


# The files of the command


def test_rows_loaded_from_a_table_take_the_place_of_the_scan_under_the_edits(
    view: BatchView, tmp_path: Path
) -> None:
    view.batch.rows = view.batch.rows.edit('1', {'thickness': 3.0})
    path = tmp_path / 'rows.csv'
    table = {'key': ['1', 'new'], 'sample': ['1', '2'], 'transmission': ['', '']}
    write_table(pd.DataFrame(table | {'thickness': ['', '']}), path)
    view.path.value = str(path)

    view.load_rows()

    assert results(view) == {'1': 'incomplete', 'new': 'incomplete'}
    assert view.batch.rows['1'].values['thickness'] == 3.0
    assert view.message.object == 'Loaded 2 rows.'


def test_a_table_that_lacks_a_column_is_refused_in_the_message(
    view: BatchView, tmp_path: Path
) -> None:
    path = tmp_path / 'rows.csv'
    write_table(pd.DataFrame({'key': ['a'], 'sample': ['1']}), path)
    view.path.value = str(path)

    view.load_rows()

    assert 'transmission' in view.message.object
    assert list(results(view)) == ['1', '2', '3']


def test_a_table_saved_has_the_results_and_its_rows_read_back(
    reduced: BatchView, tmp_path: Path
) -> None:
    reduced.path.value = str(tmp_path / 'rows.csv')

    reduced.save_rows()

    table = read_table(tmp_path / 'rows.csv')
    assert list(table['status']) == ['completed', 'completed', 'incomplete']
    assert all(table['record'][:2])
    assert set(RESULT) < set(table.columns)
    rows = rows_of(table, Params, COLUMNS)
    assert [(r.key, r.values) for r in rows] == [
        (r.key, r.values) for r in reduced.batch.rows
    ]


def test_settings_saved_as_a_template_are_loaded_back(
    view: BatchView, client: Client, tmp_path: Path
) -> None:
    saved = view.settings.params
    view.path.value = str(tmp_path / 'settings.yaml')
    view.save_settings()
    view.settings.set({'binning': {'bins': 9}}, Listing(client))

    view.load_settings()

    assert view.settings.params == saved
    assert view.message.object == 'Loaded the settings.'


def test_a_template_with_other_blanks_is_refused_in_the_message(
    view: BatchView, tmp_path: Path
) -> None:
    path = tmp_path / 'settings.yaml'
    write_template(Template(STAGED, params={'binning': {'bins': 9}}), path)
    view.path.value = str(path)

    view.load_settings()

    assert 'blanks' in view.message.object
    assert view.settings.params['binning'] == SETTINGS['binning']


def test_export_writes_the_files_of_the_completed_rows_selected(
    reduced: BatchView, tmp_path: Path
) -> None:
    reduced.table.select_keys(['1', '3'])
    reduced.path.value = str(tmp_path / 'out')

    reduced.export_selected()

    assert sorted(p.name for p in (tmp_path / 'out').iterdir()) == ['1']
    assert (tmp_path / 'out' / '1' / 'total.h5').is_file()


def test_the_command_reduces_what_the_demo_saved_and_the_demo_shows_it(
    tmp_path: Path,
) -> None:
    view = demo_view(tmp_path, samples=3)
    with view.batch.client:
        for name, save in (
            ('rows.csv', view.save_rows),
            ('s.yaml', view.save_settings),
        ):
            view.path.value = str(tmp_path / name)
            save()

    status = cli.main(
        [
            *('-w', str(tmp_path)),
            *('--bind', 'ess.apps.batch.panel.demo:BINDINGS'),
            *('reduce', str(tmp_path / 's.yaml'), str(tmp_path / 'rows.csv')),
            *('--label', 'demo', '-o', str(tmp_path / 'done.csv')),
        ]
    )

    assert status == 0
    view = demo_view(tmp_path)
    with view.batch.client:
        assert results(view) == dict.fromkeys(['1', '3', '5'], 'completed')

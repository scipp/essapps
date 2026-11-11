# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A tab that reduces a batch: the settings, a table of the rows, and an editor.

The view only places widgets and connects their events: what a row holds and
where it stands comes from :class:`~ess.apps.batch.Batch`. The settings form and
the row form are one :class:`~ess.apps.panel.Form` with complementary fields, the
values and the blanks of the batch's template.

The editor writes only the cells that differ from what it was showing, so
updating one cell of a row, or of several rows at once, leaves the others as
the scan gave them. A cell with something missing is not written, and is named
in the message. The status of a selected row changes while a person edits
it, so the editor is loaded again only when the selection or the row's cells
change. Changes not yet written are lost when the selection changes.

The files are those of the command ``essapps-batch`` (see :mod:`..cli`): a
table of rows (:mod:`..frames`), a template of the settings (:mod:`..text`),
and a folder of exported outputs (:mod:`..export`). A table loaded takes the
place of a scan, so the edits stay on top, and a table saved has the result
columns of :func:`..frames.with_results`, the status as the table shows it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd
import panel as pn

from ess.apps import FieldError, Listing, Record, SpecId, Status, SubmitError
from ess.apps.panel import Form, Table, describe
from ess.spec import field_types

from .. import Batch, Row, RowState, RowTable, frames, ready, remaining, summary
from ..export import export_table
from ..text import load_template, with_latest, with_named_files, write_template
from .cells import button, cell_texts, changed_cells, write_choice

type Draw = Callable[[Mapping[str, Record]], Any]
"""Draws the completed results of the rows selected, by key; returns a pane."""

_RESULT = 'Result'
_ALL = 'all'
_FILTERS = (
    _ALL,
    'not reduced',
    'incomplete',
    'pending',
    'completed',
    'failed',
    'cancelled',
    'stale',
)


def _refused(errors: Iterable[FieldError], heading: str = 'Refused') -> str:
    return f'{heading}:\n\n' + '\n'.join(f'- {e}' for e in errors)


class BatchView:
    """
    A tab for :class:`~ess.apps.panel.App` over ``batch``.

    ``info`` names the entries of :attr:`Row.info` to show as columns, and
    ``draw`` draws the results of the rows selected (see :data:`Draw`).
    """

    def __init__(
        self,
        batch: Batch,
        *,
        info: Sequence[str] = (),
        draw: Draw | None = None,
    ) -> None:
        self.batch = batch
        self._info = tuple(info)
        self._draw = draw
        self._listing = Listing(batch.client)
        self._states: dict[str, RowState] = {}
        self._loaded: tuple[tuple[str, ...], list[Any]] | None = None
        self._tweak_loaded: tuple[str, Any, str | None] | None = None
        self._shown: dict[str, Any] = {}
        self._drawn: tuple[tuple[str, str], ...] = ()
        model, listing = batch.spec.params, self._listing
        self.settings = Form(model, listing, fields=batch.settings_fields)
        self.settings.watch(self._show)  # changed settings make results stale
        self.row = Form(model, listing, fields=batch.columns)
        self.tweak = Form(model, listing)
        self._titles = {
            c: field_types.title(c, model.model_fields[c]) for c in batch.columns
        }
        self.table = Table[str](
            ['Row', *self._info, *self._titles.values(), _RESULT],
            self._selected,
            selectable=True,
        )
        self.key = pn.widgets.TextInput(label='Name of a new row')
        self.filter = pn.widgets.Select(label='Show', options=list(_FILTERS))
        self.filter.param.watch(lambda _: self._show(), 'value')
        self.summary = pn.pane.Markdown('')
        self.message = pn.pane.Markdown('')
        self.details = pn.pane.Markdown('')
        self.view = pn.Column(sizing_mode='stretch_width')
        self._actions = pn.Row(
            *([] if batch.source is None else [button('Scan files', self.scan)]),
            button('Reduce all', self.reduce_all, 'primary'),
            button('Reduce remaining', self.reduce_remaining, 'primary'),
            button('Reduce selected', self.reduce_selected),
            button('Cancel running', self.cancel),
            button('Use settings of selected', self.use_settings),
        )
        self.write = write_choice(self._titles)
        self._edits = pn.Row(
            self.write,
            button('Add row', self.add),
            button('Update selected', self.update),
            button('Remove selected', self.remove),
            button('Undo edits of selected', self.reset),
            button('Bring back removed rows', self.restore),
        )
        self._tweak_button = button('Submit for selected row', self.submit_tweak)
        self.path = pn.widgets.TextInput(
            label='File or folder', placeholder='rows.csv, settings.yaml, exported'
        )
        self._files = pn.Row(
            button('Load rows', self.load_rows),
            button('Save rows', self.save_rows),
            button('Load settings', self.load_settings),
            button('Save settings', self.save_settings),
            button('Export selected', self.export_selected),
        )
        self._show()

    # What is shown

    def refresh(self, listing: Listing) -> None:
        """Offer what ``listing`` has in the forms, and show the rows anew."""
        self._listing = listing
        for form in (self.settings, self.row, self.tweak):
            form.refresh(listing)
        self._show()

    def _list_again(self) -> None:
        self.refresh(Listing(self.batch.client, previous=self._listing))

    def _current_settings(self) -> dict[str, Any] | None:
        try:
            return self.settings.params
        except SubmitError:
            return None

    def _show(self) -> None:
        self._states = self.batch.state(self._current_settings())
        counts = summary(self._states)
        self.summary.object = ', '.join(f'{n} {text}' for text, n in counts.items())
        chosen = self.filter.value
        self.table.set(
            {
                row.key: self._cells(row)
                for row in self.batch.rows
                if chosen == _ALL or self._states[row.key].text == chosen
            }
        )
        self._selected()

    def _cells(self, row: Row) -> dict[str, str]:
        cells = cell_texts(row.values, self._titles, self._listing)
        info = {name: row.info.get(name, '') for name in self._info}
        return {'Row': row.key, **info, **cells, _RESULT: self._states[row.key].text}

    def _selected(self) -> None:
        keys = [k for k in self.table.selected_keys if k in self.batch.rows]
        try:
            self._load_editor(keys)
            self._load_tweak(keys)
        except (KeyError, TypeError, ValueError) as error:  # a cell no widget can show
            self.message.object = f'The editor cannot show a cell: {error}'
            self.row.set({}, self._listing)
            self._shown = self.row.held()[0]
        self._describe(keys)
        self._draw_selected(keys)

    def _load_editor(self, keys: list[str]) -> None:
        cells = [self.batch.rows[key].values for key in keys]
        if self._loaded == (tuple(keys), cells):
            return
        self._loaded = (tuple(keys), cells)
        self.row.set(cells[0] if len(keys) == 1 else {}, self._listing)
        self._shown = self.row.held()[0]
        if len(keys) == 1:
            self.key.value = keys[0]

    def _load_tweak(self, keys: list[str]) -> None:
        """Load the row's cells and what its result ran with, once per result."""
        if len(keys) != 1:
            return
        cells = self.batch.rows[keys[0]].values
        record = self._states[keys[0]].record
        source = (keys[0], cells, None if record is None else record.id)
        if self._tweak_loaded == source:
            return
        self._tweak_loaded = source
        params = self.batch.tweak_params(keys[0], record, self._current_settings())
        self.tweak.set(params, self._listing)

    def _describe(self, keys: list[str]) -> None:
        state = self._states[keys[0]] if len(keys) == 1 else None
        lines = [] if state is None else [f'- {e}' for e in state.problems]
        record = None if state is None else state.record
        if record is not None and record.id in self._listing.status:
            lines.append(describe(self.batch.client, self._listing, record))
        self.details.object = '\n\n'.join(lines)

    def _draw_selected(self, keys: list[str]) -> None:
        if self._draw is None:
            return
        states = [self._states[key] for key in keys]
        completed = {
            s.key: s.record
            for s in states
            if s.record is not None and s.status is Status.COMPLETED
        }
        drawn = tuple((k, r.id) for k, r in completed.items())
        if drawn != self._drawn:
            self._drawn = drawn
            try:
                self.view.objects = [self._draw(completed)] if completed else []
            except Exception as error:  # a drawing is code of the application
                self.view.objects = [pn.pane.Markdown(f'Cannot draw: {error!r}')]

    # What a person does

    @contextmanager
    def _showing_errors(self, failed: str) -> Iterator[None]:
        """Show in the message why the block was refused, or ``failed`` and why."""
        try:
            yield
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        except (OSError, ValueError) as error:
            self.message.object = f'{failed}: {error}'

    def _edit_rows(
        self, edit: Callable[[RowTable, str], RowTable], keys: Iterable[str]
    ) -> None:
        """Apply ``edit`` to the rows of ``keys`` in turn, and show the rows."""
        rows = self.batch.rows
        for key in keys:
            rows = edit(rows, key)
        self.batch.rows = rows
        self._show()

    def scan(self) -> None:
        """Make rows from the files now."""
        try:
            self.batch.scan()
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        except Exception as error:  # a source is code of the application
            self.message.object = f'The scan failed: {error!r}'
        self._list_again()

    def _reduce(
        self, keys_of: Callable[[dict[str, RowState]], Sequence[str]]
    ) -> dict[str, Record]:
        """Submit the rows that ``keys_of`` picks from the state of all."""
        records: dict[str, Record] = {}
        with self._showing_errors('Cannot submit'):
            settings = self.settings.params
            records = self.batch.reduce(settings, keys_of(self.batch.state(settings)))
            self.message.object = f'Submitted {len(records)} rows.'
        self._list_again()
        return records

    def reduce_all(self) -> dict[str, Record]:
        """Submit every row that is ready; their records by key."""
        return self._reduce(ready)

    def reduce_remaining(self) -> dict[str, Record]:
        """Submit the rows with no result, a failed one, or a stale one."""
        return self._reduce(remaining)

    def reduce_selected(self) -> dict[str, Record]:
        """Submit the rows selected."""
        return self._reduce(lambda _: self.table.selected_keys)

    def cancel(self) -> None:
        """Cancel the results still running."""
        self.batch.cancel()
        self._list_again()

    def use_settings(self) -> None:
        """Show the settings of the result of the row selected."""
        key = self.table.selected
        record = None if key is None else self._states[key].record
        if record is None:
            self.message.object = 'Select one row that has a result.'
            return
        self.settings.set(record.request.params, self._listing)
        self._show()

    def add(self) -> None:
        """Add a row with the name typed and the cells of the editor."""
        key = self.key.value.strip()
        if not key:
            self.message.object = 'Give the new row a name.'
            return
        with self._showing_errors('Cannot add the row'):
            self.batch.rows = self.batch.rows.add(Row(key, self.row.held()[0]))
        self._show()

    def update(self) -> None:
        """Write the cells changed in the editor to the rows selected."""
        given, missing = self.row.held()
        chosen = self.write.value
        changed = changed_cells(given, self._shown, chosen)
        skipped = [e for e in missing if e.loc[0] == chosen or e.loc[0] in self._shown]
        self.write.value = ''
        self.message.object = (
            _refused(skipped, 'Not written')
            if skipped
            else ('' if changed else 'No cell changed, so nothing was written.')
        )
        keys = self.table.selected_keys
        self._edit_rows(lambda rows, key: rows.edit(key, changed), keys)

    def remove(self) -> None:
        """Remove the rows selected."""
        self._edit_rows(RowTable.remove, self.table.selected_keys)

    def reset(self) -> None:
        """Forget the edits of the rows selected."""
        self._edit_rows(RowTable.reset, self.table.selected_keys)

    def restore(self) -> None:
        """Bring back every row that was removed."""
        self._edit_rows(RowTable.reset, self.batch.rows.removed)

    def submit_tweak(self) -> Record | None:
        """Submit the tweak form for the row selected, as that row's result."""
        key = self.table.selected
        if key is None:
            self.message.object = 'Select one row.'
            return None
        with self._showing_errors('Cannot submit'):
            record = self.batch.tweak(key, self.tweak.params)
            self.message.object = f'Submitted {key} by itself.'
            self._list_again()
            return record
        return None

    # The files of the command

    def load_rows(self) -> None:
        """Take the rows of the table at :attr:`path` in place of a scan."""
        batch = self.batch
        with self._showing_errors('Cannot load the rows'):
            table = frames.read_table(Path(self.path.value))
            rows = frames.rows_of(table, batch.spec.params, batch.columns)
            batch.rows = batch.rows.rescan(rows)
            self.message.object = f'Loaded {len(rows)} rows.'
        self._list_again()

    def _table(self, rows: Iterable[Row]) -> pd.DataFrame:
        """``rows`` as a table, with what came of each, as the table shows it."""
        table = frames.table_of(rows, self.batch.columns)
        return frames.with_results(self.batch.client, self._states, table)

    def save_rows(self) -> None:
        """Write the rows as a table to :attr:`path`, by its suffix."""
        with self._showing_errors('Cannot save the rows'):
            frames.write_table(self._table(self.batch.rows), Path(self.path.value))
            self.message.object = f'Saved the rows to {self.path.value}.'

    def load_settings(self) -> None:
        """Show the settings of the template at :attr:`path`."""
        batch = self.batch
        with self._showing_errors('Cannot load the settings'):
            template = load_template(Path(self.path.value))
            if template.spec != SpecId.of(batch.spec):
                raise ValueError(f'it is a template of {template.spec}')
            if set(template.blanks) != set(batch.columns):
                raise ValueError(f'its blanks are not {", ".join(batch.columns)}')
            settings = with_named_files(batch.spec.params, template.params)
            self.settings.set(with_latest(batch.client, settings), self._listing)
            self.message.object = 'Loaded the settings.'
        self._show()

    def save_settings(self) -> None:
        """Write the settings as a template to :attr:`path`."""
        with self._showing_errors('Cannot save the settings'):
            template = self.batch.template(self.settings.params)
            write_template(template, Path(self.path.value))
            self.message.object = f'Saved the settings to {self.path.value}.'

    def export_selected(self) -> None:
        """Export the completed rows selected to the folder :attr:`path`."""
        completed = [
            self.batch.rows[k]
            for k in self.table.selected_keys
            if self._states[k].status is Status.COMPLETED
        ]
        if not completed:
            self.message.object = 'Select rows that completed.'
            return
        folder = self.path.value
        with self._showing_errors('Cannot export'):
            export_table(self.batch.client, self._table(completed), Path(folder))
            self.message.object = f'Exported {len(completed)} rows to {folder}.'

    def panel(self) -> pn.Column:
        return pn.Column(
            pn.pane.Markdown(f'### {self.batch.spec.title}: settings of all rows'),
            self.settings.panel(),
            pn.pane.Markdown('### Rows'),
            pn.Row(self.filter, self.summary),
            self.table.widget,
            self._actions,
            self.message,
            self.details,
            self.view,
            pn.pane.Markdown('### Files, as essapps-batch reads and writes them'),
            self.path,
            self._files,
            pn.pane.Markdown('### Edit the rows selected, or add one'),
            self.key,
            self.row.panel(),
            self._edits,
            pn.pane.Markdown('### Tweak one row, with any parameter'),
            self.tweak.panel(),
            self._tweak_button,
            scroll=True,
            sizing_mode='stretch_both',
        )

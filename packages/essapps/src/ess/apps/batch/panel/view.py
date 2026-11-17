# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A tab that reduces a batch: the settings, a table of the rows, and an editor.

The view only places widgets and connects their events: what a row holds and
where it stands comes from :class:`~ess.apps.batch.Batch`. The settings form and
the row form are one :class:`~ess.apps.panel.Form` with complementary fields, the
values and the blanks of the batch's template.

The settings form is a draft of the batch's settings. It writes them to the
batch at each change that leaves nothing missing, and before a reduction or a
settings file is written, which it refuses while something is missing;
meanwhile the batch keeps the settings it had. When the batch's settings are
not those the form last showed or wrote, as after an assignment by code or a
change that the batch's definitions could not keep, the form shows them again
and writes nothing (:meth:`BatchView._show_settings`). Settings that the form
cannot show, such as those kept for another version of the spec, are named in
the message, and kept until a person changes a setting.

The editor writes only the cells that differ from what it was showing, so
updating one cell of a row, or of several rows at once, leaves the others as
the scan gave them. A cell with something missing is not written, and is named
in the message. The status of a selected row changes while a person edits
it, so the editor is loaded again only when the selection or the row's cells
change. Changes not yet written are lost when the selection changes.

The files are those of the command ``essapps-batch`` (see :mod:`..cli`): a
table of rows (:mod:`..frames`), a template of the settings (:mod:`..text`),
and a folder of exported outputs (:mod:`..export`). A table loaded is set by
hand on the rows of a scan (:meth:`~ess.apps.batch.Batch.put`), and a table
written has the result columns of :func:`..frames.batch_table`, the status as
the table shows it. These files are copies, for the command and for a person:
what the batch keeps over a restart is kept by its definitions
(:mod:`..definition`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import panel as pn

from ess.apps import FieldError, Listing, Record, Status, SubmitError
from ess.apps.panel import Form, Table, describe
from ess.spec import field_types

from .. import (
    EARLIER,
    Batch,
    Row,
    RowState,
    RowTable,
    earlier_settings,
    frames,
    left_to_reduce,
    ready,
    status_texts,
)
from ..export import export_table
from ..text import load_template, settings_of, write_template
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
    'outdated',
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
        self._texts: dict[str, str] = {}
        self._loaded: tuple[tuple[str, ...], list[Any]] | None = None
        self._tweak_loaded: tuple[str, Any, str | None] | None = None
        self._shown: dict[str, Any] = {}
        self._drawn: tuple[tuple[str, str], ...] = ()
        model, listing = batch.spec.params, self._listing
        self.settings = Form(model, listing, fields=batch.settings_fields)
        self._settings_shown: Mapping[str, Any] | None = None
        self._unfit: list[str] = []
        self._show_settings()
        self.settings.watch(self._settings_changed)
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
            button('Apply settings to all', self.apply_to_all),
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
            button('Write rows', self.write_rows),
            button('Load settings', self.load_settings),
            button('Write settings', self.write_settings),
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

    def _show_settings(self) -> None:
        """
        Show the batch's settings in the form, unless they are those that it
        last showed or wrote.
        """
        settings = self.batch.settings
        if settings == self._settings_shown:
            return
        # Settings kept for another version of the spec may not fit the form;
        # the batch keeps them, and the message names them, until a change.
        self._unfit = self.settings.set_fitting(settings, self._listing)
        self._settings_shown = settings

    def _give_settings(self) -> None:
        """
        Give the batch the settings of the form. Raises ``SubmitError``, and
        gives none, if one is missing. A form filled from settings older than
        the batch's is not written: it shows the batch's.
        """
        if self.batch.settings != self._settings_shown:
            self._show_settings()
            return
        given, missing = self.settings.held()
        if missing:
            raise SubmitError(missing)
        try:
            self.batch.settings = given
        except OSError:
            self._settings_shown = None  # the form shows the batch's again
            self._show_settings()
            raise
        self._settings_shown = self.batch.settings
        self._unfit = []

    def _settings_changed(self) -> None:
        try:
            self._give_settings()
        except SubmitError:
            pass  # the form shows what is missing
        except OSError as error:
            self.message.object = f'The settings were not kept: {error}'
        self._show()  # changed settings make results outdated

    def _show(self) -> None:
        self._show_settings()
        unfit = '\n'.join(
            ['Settings that this form cannot show:\n', *(f'- {u}' for u in self._unfit)]
            if self._unfit
            else []
        )
        if unfit:
            self.message.object = unfit
        try:
            self._states = self.batch.state()
        except SubmitError as error:  # settings refused: shown without them
            refused = _refused(error.errors)
            self.message.object = '\n\n'.join(filter(None, [unfit, refused]))
            self._states = self.batch.results()
        self._texts = status_texts(self._states, self.batch.columns)
        counts = Counter(self._text(key) for key in self._states)
        self.summary.object = ', '.join(
            f'{n} {text}' for text, n in sorted(counts.items())
        )
        chosen = self.filter.value
        self.table.set(
            {
                row.key: self._cells(row)
                for row in self.batch.rows
                if chosen == _ALL or self._text(row.key) == chosen
            }
        )
        self._selected()

    def _cells(self, row: Row) -> dict[str, str]:
        cells = cell_texts(row.values, self._titles, self._listing)
        info = {name: row.info.get(name, '') for name in self._info}
        return {'Row': row.key, **info, **cells, _RESULT: self._texts[row.key]}

    def _text(self, key: str) -> str:
        """The status of a row without its mark, as the filter and the counts read."""
        return self._texts[key].removesuffix(f', {EARLIER}')

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
        row = self._states[keys[0]]
        source = (keys[0], cells, None if row.record is None else row.record.id)
        if self._tweak_loaded == source:
            return
        self._tweak_loaded = source
        params = self.batch.tweak_params(row)
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
        with self._showing_errors('The change was not kept'):
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

    def _reduce(self, keys_of: Callable[[], Sequence[str]]) -> dict[str, Record]:
        """Submit the rows that ``keys_of`` picks."""
        records: dict[str, Record] = {}
        with self._showing_errors('Cannot submit'):
            self._give_settings()
            records = self.batch.reduce(keys_of())
            self.message.object = f'Submitted {len(records)} rows.'
        self._list_again()
        return records

    def reduce_all(self) -> dict[str, Record]:
        """Submit every row that is ready; their records by key."""
        return self._reduce(lambda: ready(self.batch.state()))

    def reduce_remaining(self) -> dict[str, Record]:
        """
        Submit the rows with no result, a failed one, or an outdated one, but
        not those reduced with earlier settings (:func:`left_to_reduce`).
        """
        return self._reduce(
            lambda: left_to_reduce(self.batch.state(), self.batch.columns)
        )

    def apply_to_all(self) -> dict[str, Record]:
        """Submit the rows reduced with earlier settings (:func:`earlier_settings`)."""
        return self._reduce(
            lambda: earlier_settings(self.batch.state(), self.batch.columns)
        )

    def reduce_selected(self) -> dict[str, Record]:
        """Submit the rows selected."""
        return self._reduce(lambda: self.table.selected_keys)

    def cancel(self) -> None:
        """Cancel the results still running."""
        self.batch.cancel()
        self._list_again()

    def use_settings(self) -> None:
        """Show the settings of the result of the row selected."""
        key = self.table.selected
        row = None if key is None else self._states[key]
        if row is None or row.record is None or row.spec_changed:
            self.message.object = 'Select one row that has a result of this workflow.'
            return
        self._show_settings()  # what the form shows next is a change of these
        self.settings.set(row.record.request.params, self._listing)
        self._settings_changed()

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
        """Set the rows of the table at :attr:`path` by hand."""
        batch = self.batch
        if batch.source is not None:
            try:
                batch.scan()  # the table is set on top of the rows of the files
            except Exception as error:  # a source is code of the application
                self.message.object = f'Not loaded, since the scan failed: {error!r}'
                self._list_again()
                return
        with self._showing_errors('Cannot load the rows'):
            table = frames.read_table(Path(self.path.value))
            rows = frames.rows_of(table, batch.spec.params, batch.columns)
            batch.put(rows)
            self.message.object = f'Loaded {len(rows)} rows.'
        self._list_again()

    def write_rows(self) -> None:
        """Write the rows as a table to :attr:`path`, by its suffix."""
        with self._showing_errors('Cannot write the rows'):
            frames.write_table(
                frames.batch_table(self.batch, self._states), Path(self.path.value)
            )
            self.message.object = f'Wrote the rows to {self.path.value}.'

    def load_settings(self) -> None:
        """Show the settings of the template at :attr:`path`."""
        with self._showing_errors('Cannot load the settings'):
            template = load_template(Path(self.path.value))
            self._show_settings()  # what the form shows next is a change of these
            self.settings.set(settings_of(self.batch, template), self._listing)
            self.message.object = 'Loaded the settings.'
        self._settings_changed()

    def write_settings(self) -> None:
        """Write the settings as a template to :attr:`path`."""
        with self._showing_errors('Cannot write the settings'):
            self._give_settings()
            write_template(self.batch.template(), Path(self.path.value))
            self.message.object = f'Wrote the settings to {self.path.value}.'

    def export_selected(self) -> None:
        """Export the completed rows selected to the folder :attr:`path`."""
        completed = [
            k
            for k in self.table.selected_keys
            if self._states[k].status is Status.COMPLETED
        ]
        if not completed:
            self.message.object = 'Select rows that completed.'
            return
        folder = self.path.value
        with self._showing_errors('Cannot export'):
            table = frames.batch_table(self.batch, self._states, completed)
            export_table(self.batch.client, table, Path(folder))
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

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
the scan gave them. The status of a selected row changes while a person edits
it, so the editor is loaded again only when the selection or the row's cells
change. Changes not yet written are lost when the selection changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import panel as pn

from ess.apps import FieldError, Listing, Record, Status, SubmitError
from ess.apps.panel import Form, Table, describe
from ess.spec import field_types

from .. import Batch, Row, RowState, cell_text, ready, remaining, summary
from ..table import EMPTY

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


def _refused(errors: tuple[FieldError, ...]) -> str:
    return 'Refused:\n\n' + '\n'.join(f'- {e}' for e in errors)


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
        self._shown: dict[str, Any] = {}
        self._drawn: tuple[tuple[str, str], ...] = ()
        spec = batch.spec
        model, listing = spec.params, self._listing
        self.settings = Form(model, listing, fields=batch.settings_fields)
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
            *(
                self._button(label, action, color)
                for label, action, color in (
                    ('Scan files', self.scan, 'default'),
                    ('Reduce all', self.reduce_all, 'primary'),
                    ('Reduce remaining', self.reduce_remaining, 'primary'),
                    ('Reduce selected', self.reduce_selected, 'default'),
                    ('Cancel running', self.cancel, 'default'),
                    ('Use settings of selected', self.use_settings, 'default'),
                )
                if action != self.scan or batch.source is not None
            )
        )
        self._edits = pn.Row(
            self._button('Add row', self.add),
            self._button('Update selected', self.update),
            self._button('Remove selected', self.remove),
            self._button('Undo edits of selected', self.reset),
        )
        self._tweak_button = self._button('Submit for selected row', self.submit_tweak)
        self._show()

    @staticmethod
    def _button(
        label: str, action: Callable[[], Any], color: str = 'default'
    ) -> pn.widgets.Button:
        button = pn.widgets.Button(label=label, color=color)
        button.on_click(lambda _: action())
        return button

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
        cells = {
            title: cell_text(row.values[c], self._listing) if c in row.values else ''
            for c, title in self._titles.items()
        }
        info = {name: row.info.get(name, '') for name in self._info}
        return {'Row': row.key, **info, **cells, _RESULT: self._states[row.key].text}

    def _selected(self) -> None:
        keys = [k for k in self.table.selected_keys if k in self.batch.rows]
        self._load_editor(keys)
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
            record = self._states[keys[0]].record
            settings = self._current_settings() or {}
            given = cells[0] if record is None else record.request.params
            self.tweak.set(
                {**settings, **given} if record is None else given, self._listing
            )

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
            self.view.objects = [self._draw(completed)] if completed else []

    # What a person does

    def scan(self) -> None:
        """Make rows from the files now."""
        try:
            self.batch.scan()
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        self._show()

    def _reduce(
        self, keys_of: Callable[[dict[str, RowState]], Sequence[str]]
    ) -> dict[str, Record]:
        """Submit the rows that ``keys_of`` picks from the state of all."""
        records: dict[str, Record] = {}
        try:
            settings = self.settings.params
            records = self.batch.reduce(settings, keys_of(self.batch.state(settings)))
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        else:
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
        try:
            self.batch.rows = self.batch.rows.add(Row(key, self.row.held()[0]))
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        self._show()

    def update(self) -> None:
        """Write the cells changed in the editor to the rows selected."""
        given = self.row.held()[0]
        changed = {
            column: given.get(column, EMPTY)
            for column in self.batch.columns
            if given.get(column, EMPTY) != self._shown.get(column, EMPTY)
        }
        rows = self.batch.rows
        for key in self.table.selected_keys:
            rows = rows.edit(key, changed)
        self.batch.rows = rows
        self._show()

    def remove(self) -> None:
        """Remove the rows selected."""
        rows = self.batch.rows
        for key in self.table.selected_keys:
            rows = rows.remove(key)
        self.batch.rows = rows
        self._show()

    def reset(self) -> None:
        """Forget the edits of the rows selected."""
        rows = self.batch.rows
        for key in self.table.selected_keys:
            rows = rows.reset(key)
        self.batch.rows = rows
        self._show()

    def submit_tweak(self) -> Record | None:
        """Submit the tweak form for the row selected, as that row's result."""
        key = self.table.selected
        if key is None:
            self.message.object = 'Select one row.'
            return None
        try:
            record = self.batch.tweak(key, self.tweak.params)
        except SubmitError as error:
            self.message.object = _refused(error.errors)
            return None
        self.message.object = f'Submitted {key} by itself.'
        self._list_again()
        return record

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

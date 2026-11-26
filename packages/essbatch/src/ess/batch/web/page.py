# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The page of one batch, without HTTP: :class:`Page`.

This module imports neither ``fastapi`` nor ``starlette``, so that another
view of a batch can call the same actions and render the same
:class:`~.models.State`.

A :class:`Page` holds the settings draft (:class:`~.draft.Draft`), the submit
errors of the last reduction, and its revision. It reads everything else from
:class:`~.pages.Pages` and the batch layer. An action is a method decorated
with ``_action``: it holds the lock of all pages, increments the revision, and
returns the state. An action that changes rows builds a new batch table and
passes it to :meth:`~ess.batch.Batch.set_table`.

While the settings form holds a change, the page rejects a reduction, the
download of the settings, and a change of which parameters are columns,
including "Load table". A reduction would use settings other than those
shown, and a change of the parameter columns rebuilds the form, which would
drop the change.

If a reduction raises :class:`~ess.apps.errors.SubmitError`, the errors that a
cell or a setting can show stay in the state, and the others form the message
of the :class:`~.errors.Rejected`. An error in the state stays until its
value changes, the settings are applied, or a reduction succeeds. The state of
the batch shows most invalid cells already, as problems of their rows. Only a
submission finds a reference to a record that the workspace does not know, or
to an output whose type does not fit the cell.

How the browser polls the state: ``README.md`` in this folder.
"""

from __future__ import annotations

import functools
import io
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from ess.apps.errors import FieldError, SubmitError, error_text
from ess.apps.fields import UNSET, Fields
from ess.apps.records import Record, Status

from .. import definition, frames
from ..definition import OutputOptions
from ..export import export_table
from ..reduction import Batch
from ..state import RowState, publishable, to_reduce
from ..table import KEY, NAME, cell_of, read_table, row_names, write_table
from ..templates import checked_settings, load_template, settings_text
from . import forms, plots, view
from .draft import Draft
from .errors import Rejected, reject_on_error, reject_on_write_error, request_error_text
from .models import (
    Cell,
    CellEdit,
    Column,
    Edit,
    HeaderEdit,
    HeaderShown,
    MakeSetting,
    Plot,
    Reduce,
    Row,
    RowDetail,
    SourceRows,
    State,
    Value,
)
from .tempfiles import named_file, read_file

if TYPE_CHECKING:
    from .pages import Pages


@dataclass(frozen=True)
class Shown:
    """A result to draw: that of row ``key``, called ``name``, in ``color``."""

    key: str
    name: str
    record: Record
    outdated: bool
    color: str


type Draw = Callable[[Sequence[Shown]], dict[str, Any]]
"""Draws results as a Plotly figure."""

_ENDED = (Status.FAILED, Status.CANCELLED)
_DRAWN = 12
"""How many results the plot shows when no row is selected."""


def _rows(n: int) -> str:
    return f'{n} row{"s" * (n != 1)}'


def _locked[**P, T](method: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(method)
    def locked(*args: P.args, **kwargs: P.kwargs) -> T:
        with args[0]._held():  # type: ignore[attr-defined]
            return method(*args, **kwargs)

    return locked


def _action[**P](method: Callable[P, str | None]) -> Callable[P, State]:
    """
    Make ``method`` an action: it increments the revision and returns the
    state, with the notice that ``method`` returns.
    """

    @functools.wraps(method)
    def action(*args: P.args, **kwargs: P.kwargs) -> State:
        page: Page = args[0]  # type: ignore[assignment]
        with page._held():
            try:
                with reject_on_write_error():
                    notice = method(*args, **kwargs)
            finally:
                page._new_revision()
            return page.state(notice=notice or '')

    return action


class Page:
    """
    The page of one batch for one person, without HTTP.

    Parameters
    ----------
    pages:
        What the pages of all batches share.
    label:
        The label of the batch.
    """

    def __init__(self, pages: Pages, label: str) -> None:
        self.pages = pages
        self.label = label
        pages.runs.list_again()
        self._columns: tuple[str, ...] | None = None
        self._fit()
        # The errors of the last failed submission that a cell or setting shows.
        self._submit_errors: list[FieldError] = []
        self.revision = 1
        self.start = uuid.uuid4().hex[:8]
        # What :meth:`_live` returned at the last change of the revision.
        self._seen = self._live()

    @property
    def batch(self) -> Batch:
        """The batch, from the manager at each use."""
        return self.pages.intake.manager.batch(self.label)

    @property
    def lock(self) -> AbstractContextManager[object]:
        """The lock of all pages."""
        return self.pages.lock

    @contextmanager
    def _held(self) -> Iterator[None]:
        """
        Hold the lock of all pages, with the forms fit to the parameter
        columns of the batch.

        Raises :class:`~.errors.NotFound` if the batch was removed since the
        page was handed out, such as by a request that ran between the check
        of the label in :meth:`Pages.page <.pages.Pages.page>` and this one.
        """
        with self.lock:
            self.pages.check(self.label)
            self._fit()
            yield

    def _fit(self) -> None:
        """
        Rebuild the cell fields and the settings form if the parameter columns
        changed, such as by "Load table". The form then shows the batch's
        settings.
        """
        batch = self.batch
        if batch.parameter_columns == self._columns:
            return
        self._columns = batch.parameter_columns
        self._cells = Fields(
            batch.spec.params, self.pages.runs.listing, fields=batch.parameter_columns
        )
        self.draft = Draft(batch, self.pages.runs.listing)
        self._titles = view.titles(batch.spec.params)

    def _follows(self) -> bool:
        """Whether this batch is the one that follows."""
        return self.pages.intake.following == self.label

    # Reading

    def _live(self) -> tuple[object, ...]:
        """
        A value per thing the page shows that can change without an action of
        this page: a revision counter, or the value itself.
        """
        pages = self.pages
        return (
            self.batch.revision,
            pages.runs.listing.cursor,
            pages.runs.listing.revision,
            pages.intake.manager.revision,
            pages.intake.following,
            pages.last_step.token(self.label),
        )

    def _new_revision(self) -> None:
        self.revision += 1
        self._seen = self._live()

    def _list_again(self) -> None:
        """List the files again and refresh the pickers of the cells and the form."""
        self.pages.runs.list_again()
        self.draft.refresh(self.pages.runs.listing)
        self._cells.refresh(self.pages.runs.listing)

    def _show_settings(self) -> None:
        """Show the batch's settings in the form, which then holds no change."""
        self.draft.reset(self.batch.settings, self.pages.runs.listing)

    def _states(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """The state of the rows ``keys``, or of all rows."""
        return self.batch.shown(keys)

    @_locked
    def poll(self, after: int, start: str = '') -> State | None:
        """The state, or ``None`` if it is still that of revision ``after``."""
        # The feed catches a record that ended after the last listing but
        # before ``_seen`` was read: ``_live`` holds the new value already, and
        # the listing lacks the record.
        changes = self.batch.client.changes(after=self.pages.runs.listing.cursor)
        # A table changed by another caller may name files that the listing lacks.
        if changes.records or self._live() != self._seen:
            self._list_again()
            self._new_revision()
        if after == self.revision and start == self.start:
            return None
        return self.state()

    def _table(self, keys: Iterable[str] = ()) -> pd.DataFrame:
        """A copy of the batch table. Raises ``Rejected`` if a key has no row."""
        frame = self.batch.table()
        for key in keys:
            if key not in frame.index:
                raise Rejected(f'There is no row {key}.')
        return frame

    def _set_table(self, table: pd.DataFrame) -> None:
        """Set the batch table. Raises ``Rejected`` if the batch rejects it."""
        try:
            self.batch.set_table(table)
        except ValueError as error:
            raise Rejected(f'{request_error_text(error)}.') from None

    @_locked
    def state(self, notice: str = '') -> State:
        """Everything the page shows except a row's details, the plot and the runs."""
        batch = self.batch
        columns = batch.parameter_columns
        states, unusable = self._states()
        unsubmitted = self.pages.last_step.unsubmitted(self.label, states)
        failures = self._failures(states)
        frame = batch.table()
        requested = batch.cells()
        named = row_names(frame, columns)
        notes = [n for n in frame.columns if n not in columns and n != NAME]
        titles = self._titles
        published = definition.published(
            self.pages.intake.manager.definitions, self.label
        )
        may = set(publishable(states))
        rows = [
            self._row(
                key,
                cells,
                requested[key],
                named[key],
                notes,
                states[key],
                unsubmitted.get(key, ''),
                failures,
                published,
                key in may,
            )
            for key, cells in frame.to_dict('index').items()
        ]
        missing = self.draft.params_and_missing()[1]
        # An error of the batch's settings is marked at its field only while
        # the form shows the invalid value.
        errors = [*missing, *self.draft.at_applied(unusable)]
        problem = view.settings_problem(errors, self.draft.unfit, titles)
        unapplied = self.draft.unapplied()
        marks = self.draft.at_applied(
            e for e in (*unusable, *self._submit_errors) if not e.request_key
        )
        empty = batch.empty()
        header = self.pages.intake.manager.headers()[self.label]
        return State(
            revision=self.revision,
            start=self.start,
            runs=self.pages.runs.revision,
            batches=self.pages.batches().rows,
            label=self.label,
            title=header.title,
            header=HeaderShown(
                workflow=batch.spec.title,
                spec=batch.spec.name,
                description=header.description,
            ),
            application=self.pages.title,
            folder=self.pages.folder,
            notes=[view.note(n) for n in notes],
            columns=[
                Column(
                    name=c,
                    title=titles[c],
                    editor=forms.editor(self._cells.fields[c]),
                    from_source=c in batch.source_columns,
                    # as a cell that holds it shows it
                    default=self._cell_text(c, cell_of(empty[c])) if c in empty else '',
                )
                for c in columns
            ],
            rows=rows,
            counts=view.counts(states),
            action=view.action(states, unapplied or problem, unapplied),
            settings=forms.tree(self.draft.fields, marks, columns),
            settings_problem=problem,
            settings_unapplied=unapplied is not None,
            may_be_columns=[
                n for n in forms.edited(self.draft.fields) if n not in columns
            ],
            submit_errors=[view.refusal(e, named, titles) for e in self._submit_errors],
            follow=self.pages.last_step.shown(self.label),
            files_problem=self.pages.runs.listing.failure,
            output=view.output(
                self.pages.writing.options(self.label),
                self.pages.writing.writers(self.label),
                batch.spec,
                self.pages.publisher is not None,
            ),
            notice=notice,
        )

    def _failures(self, states: Mapping[str, RowState]) -> dict[str, str]:
        ended = [
            s.record
            for s in states.values()
            if s.record is not None and s.record_status in _ENDED
        ]
        reasons = self.batch.client.failure(ended)
        return {r.id: view.failure(why) for r, why in zip(ended, reasons, strict=True)}

    def _cell_text(self, name: str, cell: Any) -> str:
        """What a cell of the column ``name`` that holds ``cell`` shows."""
        listing = self.pages.runs.listing
        return view.cell_text(listing, self.batch.spec.params, name, cell)

    def _row(
        self,
        key: str,
        cells: Mapping[str, Any],
        requested: Mapping[str, Any],
        name: str,
        notes: Sequence[str],
        state: RowState,
        unsubmitted: str,
        failures: Mapping[str, str],
        published: Mapping[str, str],
        may_publish: bool,
    ) -> Row:
        titles = self._titles
        problems = view.problems(key, (*state.problems, *self._submit_errors), titles)
        record = state.record
        failure = '' if record is None else failures.get(record.id, '')
        reason, changed = view.reason(state, problems, unsubmitted, failure, titles)
        # An empty cell is shown empty, not as the setting or default that the
        # request uses.
        given = {c: requested[c] for c in requested if c in state.overrides}
        pid = '' if record is None else published.get(record.id, '')
        return Row(
            key=key,
            name=name,
            notes={n: '' if cells[n] is None else str(cells[n]) for n in notes},
            status=state.status,
            reason=reason,
            changed=changed,
            record=None if record is None else record.id,
            result=view.result(state),
            published=pid,
            publishable=may_publish and not pid,
            cells={
                c: Cell(
                    value=forms.cell(self._cells.fields[c], given.get(c, UNSET)),
                    text=view.text(self.pages.runs.listing, given.get(c, UNSET)),
                    problem=problems.get(c),
                )
                for c in self.batch.parameter_columns
            },
        )

    def _placed(self, error: FieldError, keys: Iterable[str]) -> bool:
        """Whether a cell of a row of ``keys``, or a setting, can show ``error``."""
        if not error.loc:
            return False
        if error.request_key is None:
            return str(error.loc[0]) in self.draft.fields.fields
        return (
            error.request_key in keys
            and str(error.loc[0]) in self.batch.parameter_columns
        )

    # The rows

    @_action
    def relist(self) -> str:
        """List the files again. No row changes."""
        self._list_again()
        return 'Listed the runs again.'

    @_action
    def edit(self, edit: CellEdit) -> str:
        """Set one column of rows ``edit.keys`` to the value sent."""
        value = view.read(self._cells, edit)
        frame = self._table(edit.keys)
        # As objects, so that the batch, not pandas, rejects an invalid cell.
        column = frame[edit.column].astype(object)
        column.loc[edit.keys] = (
            None if value is UNSET or value is None else cell_of(value)
        )
        self._set_table(frame.assign(**{edit.column: column}))
        self._forget(edit.keys, edit.column)
        if len(edit.keys) < 2:
            return ''
        title = self._cells.titles[edit.column]
        return f'{title} set on {_rows(len(edit.keys))}.'

    def _forget(self, keys: Sequence[str], column: str) -> None:
        """Drop the submit errors at ``column`` of the rows ``keys``."""
        self._submit_errors = [
            e
            for e in self._submit_errors
            if not (e.request_key in keys and e.loc and str(e.loc[0]) == column)
        ]

    @_action
    def remove(self, keys: Sequence[str]) -> str:
        """Remove rows from the table."""
        self._set_table(self._table(keys).drop(index=list(keys)))
        return f'Removed {_rows(len(keys))}.'

    @_action
    def add_empty_row(self, key: str) -> str:
        """Add an empty row with ``key``."""
        key = key.strip()
        if not key:
            raise Rejected('Give the new row a key.')
        frame = self._table()
        self._set_table(frame.reindex(pd.Index([*frame.index, key], name=KEY)))
        return f'Added {key}: fill in its cells.'

    @_locked
    def available_rows(self) -> SourceRows:
        """
        The rows of the row source that the batch table lacks
        (:meth:`~ess.batch.Intake.available_rows`).

        Raises :class:`~.errors.Rejected` if the source raises.
        """
        with reject_on_error('The rows of the source cannot be listed'):
            given = self.pages.intake.available_rows(self.label)
        return view.listed(given, self.batch.source_columns)

    @_action
    def add_rows(self, keys: Sequence[str]) -> str:
        """
        Add the rows ``keys`` of the row source, as the source makes them now
        (:meth:`~ess.batch.Intake.add_rows`).

        Raises :class:`~.errors.Rejected` if the ``Intake`` or the source
        raises.
        """
        with reject_on_error('No row was added'):
            self.pages.intake.add_rows(self.label, keys)
        # The rows may refer to files that the listing lacks.
        self._list_again()
        return f'Added {_rows(len(keys))}.'

    # Columns and settings

    @_locked
    def values(self, name: str) -> list[Value]:
        """
        The distinct values of the column ``name``, each with a row that has
        it, for "Make this a setting" (:meth:`~ess.batch.Batch.values`).
        """
        try:
            values = self.batch.values(name)
        except ValueError as error:
            raise Rejected(f'{request_error_text(error)}.') from None
        return [
            Value(key=key, text=self._cell_text(name, cell))
            for key, cell in values.items()
        ]

    def _settled(self) -> None:
        """
        Raise :class:`~.errors.Rejected` while the form holds a change, which
        a change of the parameter columns would drop.
        """
        if unapplied := self.draft.unapplied():
            raise Rejected(f'Nothing was changed: {unapplied.lower()} first.')

    def _reshape(self, name: str, change: Callable[[], None]) -> None:
        """
        Call ``change`` of the parameter columns at ``name``, then drop the
        submit errors at ``name``.
        """
        self._settled()
        try:
            change()
        except ValueError as error:
            raise Rejected(
                f'Nothing was changed: {request_error_text(error)}.'
            ) from None
        self._submit_errors = [
            e for e in self._submit_errors if not (e.loc and e.loc[0] == name)
        ]

    @_action
    def make_column(self, name: str) -> str:
        """
        Make the parameter ``name`` a column with empty cells
        (:meth:`~ess.batch.Batch.make_column`).
        """
        title = self._titles.get(name, name)
        self._reshape(name, lambda: self.batch.make_column(name))
        taken = 'the setting' if name in self.batch.settings else 'the default'
        return f'{title} is a column: a row takes {taken} until its cell is set.'

    @_action
    def make_setting(self, made: MakeSetting) -> str:
        """
        Make a column a setting, with the value of one row or of all rows
        (:meth:`~ess.batch.Batch.make_setting`).
        """
        title = self._titles.get(made.name, made.name)
        self._reshape(made.name, lambda: self.batch.make_setting(made.name, made.key))
        return f'{title} is a setting: one value for every row.'

    # Reducing

    @_action
    def reduce(self, request: Reduce) -> str:
        """Submit the rows asked for, all or none."""
        frame = self._table(request.keys)
        if unapplied := self.draft.unapplied():
            raise Rejected(f'Nothing was reduced: {unapplied.lower()}.')
        try:
            keys = (
                request.keys
                if request.which == 'selected'
                else to_reduce(self.batch.state(), request.which)
            )
            records = self.batch.reduce(keys)
        except SubmitError as error:
            self._submit_errors = [
                e for e in error.errors if self._placed(e, frame.index)
            ]
            unplaced = [e for e in error.errors if e not in self._submit_errors]
            named = row_names(frame, self.batch.parameter_columns)
            raise _NotReduced(
                [view.refusal(e, named, self._titles).text for e in unplaced],
                len(self._submit_errors),
            ) from None
        self._submit_errors = []
        self._list_again()
        n = len(records)
        return f'Reducing {_rows(n)}.' if n else ''

    @_action
    def cancel(self) -> str:
        self.batch.cancel()
        return 'Cancelled the rows still running.'

    # Follow

    @_action
    def stop_following(self) -> str:
        """
        Stop Follow if this batch follows (:meth:`~ess.batch.Intake.stop`).

        The page has no action to resume it.
        """
        if not self._follows():
            return ''  # another batch follows, or none
        self.pages.intake.stop()
        return 'Stopped following.'

    def after_step(self) -> None:
        """
        Show what a step of Follow on this batch changed.

        Lists the files again, since new rows may refer to files that the
        pickers lack. The revision changes only if something the page shows
        changed, so that a polling page receives nothing new otherwise.
        """
        with self.lock:
            try:
                self._list_again()
            finally:
                if self._live() != self._seen:
                    self._new_revision()

    # Settings

    @_action
    def edit_settings(self, edit: Edit) -> None:
        """Change the settings form. The batch's settings stay until Apply."""
        try:
            self.draft.edit(edit)
        except (KeyError, ValueError) as error:
            raise Rejected(request_error_text(error)) from None

    @_action
    def apply_settings(self) -> str:
        """Set the batch's settings to those of the form, unless one is missing."""
        given, missing = self.draft.params_and_missing()
        if missing:
            raise Rejected(
                f'Not applied: {view.field_errors_text(missing, self._titles)}.'
            )
        self.batch.settings = given
        self._show_settings()
        # An error of a setting belonged to the settings replaced, and Follow
        # tries every row again with these at its next step.
        self._submit_errors = [e for e in self._submit_errors if e.request_key]
        self.pages.last_step.clear_submit_errors(self.label)
        return 'Applied the settings.'

    @_action
    def discard_settings(self) -> None:
        """Show the batch's settings in the form again."""
        self._show_settings()

    def _fill(self, params: Mapping[str, Any]) -> None:
        """
        Show ``params`` in the form, all or none, except those at source
        columns (:meth:`~ess.batch.Batch.settable`). Raises as
        :meth:`Fields.set`.
        """
        self.draft.fill(self.batch.settable(params), self.pages.runs.listing)

    @_action
    def use_settings(self, key: str) -> str:
        """
        Show the settings that the result of row ``key`` was reduced with
        (:meth:`~ess.batch.Batch.settings_of`).
        """
        frame = self._table([key])
        name = row_names(frame, self.batch.parameter_columns)[key]
        try:
            settings = self.batch.settings_of(key)
        except ValueError as error:
            raise Rejected(f'{request_error_text(error)}.') from None
        self._fill(settings)
        return (
            f'The form shows the settings {name} was reduced with. Apply to use them.'
        )

    # The header

    @_action
    def change(self, edit: HeaderEdit) -> str:
        """
        Set the title and description of the batch
        (:meth:`~ess.batch.BatchManager.change`).

        Raises :class:`~.errors.Rejected`, and changes nothing, if the
        manager raises ``ValueError``.
        """
        try:
            self.pages.intake.manager.change(
                self.label, title=edit.title, description=edit.description
            )
        except ValueError as error:
            raise Rejected(
                f'The batch was not changed: {request_error_text(error)}.'
            ) from None
        return 'Saved.'

    # The output

    @_action
    def set_output_form(self, fields: Mapping[str, Any]) -> str:
        """
        Set the output options of the batch from the fields of the tab
        "Output" (:meth:`~..writing.Writing.set_options`).
        """
        with reject_on_error('Not applied'):
            self.pages.writing.set_options(
                self.label, OutputOptions.model_validate(fields)
            )
        return 'Applied the form.'

    @_action
    def publish(self, keys: Sequence[str]) -> str:
        """
        Publish the result of each row of ``keys`` with the output options
        (:meth:`~..publish.Publisher.publish`).

        If the publisher rejects the request, nothing is published. If an
        upload fails, the datasets published before it stay published, and
        the message lists them.
        """
        if (publisher := self.pages.publisher) is None:
            raise Rejected('This application publishes to no catalogue.')
        if not keys:
            raise Rejected('Select the rows to publish.')
        with reject_on_error('Not published'):
            pids = publisher.publish(self.label, keys)
        return f'Published {_rows(len(pids))}: {", ".join(pids.values())}.'

    # One row

    @_locked
    def row(self, key: str) -> RowDetail:
        """
        The parameters that the result of row ``key`` was reduced with, and
        those that differ from what the row would use now.
        """
        self._table([key])
        state = self._states([key])[0][key]
        record = state.record
        if record is None or state.spec_changed:
            # an unbound spec's params are not this one's
            return RowDetail(key=key, reduced_with=None, created=None, differs=[])
        given = Fields(self.batch.spec.params, self.pages.runs.listing)
        given.set(record.request.params, self.pages.runs.listing)
        return RowDetail(
            key=key,
            reduced_with=forms.tree(given),
            created=record.created,
            differs=[n for n in given.fields if n in (state.changed or ())],
        )

    # The result

    @_locked
    def plot(self, keys: Sequence[str]) -> Plot:
        """
        The figure of the results among the rows ``keys``, or of the first
        results if none is given, and a note of what it shows.
        """
        frame = self.batch.table()
        order = {key: i for i, key in enumerate(frame.index)}
        states, _ = self._states(
            sorted({k for k in keys if k in order}, key=order.__getitem__)
            if keys
            else None
        )
        done = [
            s
            for s in states.values()
            if s.record is not None and s.record_status is Status.COMPLETED
        ]
        if keys:
            chosen = done
            note = '' if chosen else 'None of the rows selected has a result.'
        else:
            chosen = done[:_DRAWN]
            note = 'No results yet. Reduce rows to see them here.' if not done else ''
            if len(done) > _DRAWN:
                note = f'The first {_DRAWN} of {len(done)}. Select rows to choose.'
        draw = self.pages.draw.get(self.batch.spec.name)
        if not chosen or draw is None:
            return Plot(figure=None, note=note)
        colors = plots.colors([order[s.key] for s in chosen])
        named = row_names(frame, self.batch.parameter_columns)
        shown = [
            Shown(s.key, named[s.key], s.record, s.status == 'outdated', c)
            for s, c in zip(chosen, colors, strict=True)
            if s.record is not None
        ]
        try:
            return Plot(
                figure=draw(shown),
                note=note,
                colors={s.key: s.color for s in shown},
            )
        except Exception as error:  # a drawing is code of the application
            return Plot(figure=None, note=f'Cannot draw: {error_text(error)}')

    # Files

    @_locked
    def table_file(self, suffix: str) -> bytes:
        """The batch table with what came of each row, as ``.csv`` or ``.xlsx``."""
        batch = self.batch
        table = frames.with_results(batch, self._states()[0])
        with named_file(f'rows{suffix}') as path:
            write_table(table, path)
            return path.read_bytes()

    @_action
    def load_table(self, name: str, content: bytes) -> str:
        """
        Replace the batch table with the rows of a table file.

        Rejected while the form holds a change, since the parameter columns
        may change.
        """
        self._settled()
        try:
            params = self.batch.spec.params
            frame = read_file(name, content, lambda path: read_table(path, params))
            with (
                reject_on_write_error()
            ):  # an error of the definitions, not of the file
                self.batch.set_table(frame)
        except (ValueError, OSError) as error:
            raise Rejected(f'Cannot load {name}: {request_error_text(error)}') from None
        finally:
            # The rows may refer to files that the listing lacks, even if the
            # table is then rejected.
            self._list_again()
        return f'Loaded {_rows(len(frame))} from {name}.'

    @_locked
    def settings_file(self) -> bytes:
        """The batch's settings as a YAML template, those at the columns included."""
        if unapplied := self.draft.unapplied():
            raise Rejected(f'{unapplied} first.')
        if missing := self.draft.params_and_missing()[1]:
            raise Rejected(f'Fill in the settings first: {SubmitError(missing)}')
        return settings_text(self.batch).encode()

    @_action
    def load_settings(self, name: str, content: bytes) -> str:
        """Show the settings of a YAML template in the form, all or none."""
        cannot = f'Cannot load {name}:'
        with named_file('settings.yaml', content) as path:
            try:
                template = load_template(path)
            except (OSError, ValueError):
                raise Rejected(
                    f'{cannot} it is not a settings file saved from this page.'
                ) from None
        try:
            batch = self.batch
            self._fill(checked_settings(batch.client, batch.spec, template))
        except (ValueError, KeyError) as error:
            raise Rejected(
                f'{cannot} {forms.message(request_error_text(error))}.'
            ) from None
        return f'The form shows the settings of {name}. Apply to use them.'

    @_locked
    def results_file(self, keys: Sequence[str]) -> bytes:
        """
        A zip of the results of the done rows among ``keys``, or of all rows,
        written with the output options (:meth:`~..writing.Writing.by_options`),
        with the rows as ``results.csv``.
        """
        batch = self.batch
        frame = batch.table()
        known = [k for k in keys if k in frame.index]
        states, _ = self._states(known if keys else None)
        done = [k for k, s in states.items() if s.record_status is Status.COMPLETED]
        if not done:
            raise Rejected('None of these rows has a result.')
        buffer = io.BytesIO()
        with named_file('results') as folder:
            table = frames.with_results(batch, states, done)
            with reject_on_error('Not downloaded'):
                write, _ = self.pages.writing.by_options(self.label)
                export_table(batch.client, table, folder, write)
            write_table(table, folder / 'results.csv')
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(folder.parent))
        return buffer.getvalue()


class _NotReduced(Rejected):
    """
    A failed submission, with the errors that no cell or setting shows, such
    as a row still running. The state holds the others.
    """

    def __init__(self, said: list[str], placed: int) -> None:
        if placed:
            said.append(
                'one value is refused.'
                if placed == 1
                else f'{placed} values are refused.'
            )
        super().__init__(f'Nothing was reduced: {"; ".join(said)}')

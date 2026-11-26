# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the page of one batch does, without HTTP: :class:`Page`. :mod:`.api`
serves it. This module imports neither ``fastapi`` nor ``starlette``, so
another view of a batch can call the same actions and render the same
:class:`~.models.State`.

A :class:`Page` holds what a person changes on it: the form of the
settings and the refusals. It asks :class:`~.pages.Pages` for the rest: the
:class:`~ess.batch.Batch` of its label with its batch table and settings, kept
by the manager, the corrections of runs, and Follow. The batch and the
corrections keep their part in their definitions, if given them
(:mod:`..definition`); a change that the definitions cannot keep is not made,
and answers a :class:`~.errors.Rejected`.

The page shows the batch table as it is kept, without its source and
without the files: a cell that names a file that is not found, or that the
file source cannot read, is a problem of its row. Each action that changes
rows builds a frame with one change and gives it to
:meth:`~ess.batch.Batch.set_table`: an edit of cells, a row added or removed,
a table file loaded, which replaces the batch table. A cell is plain
data (:mod:`..table`): a value that a picker gives is kept as
:func:`~..table.cell_of` makes it, and shown as the listing names it. A row is
named by its note ``name`` if the table has one, else by its key, and "Add
row" takes its key. "List runs again" lists the files again, for the pickers
and the Runs view, and changes no row. If the file source raises, the page
shows the batch table, the results and the plots all the same: the pickers offer
no file, the Runs view is empty, and the state says why
(:attr:`~.models.State.files_problem`).

The form of the settings is a draft of the batch's settings
(:class:`~.draft.Draft`). An edit changes
the form only; :meth:`Page.apply_settings` writes it to the batch, and is
refused while something is missing; :meth:`Page.discard_settings` shows the
batch's settings again. "Load settings" and "Put these settings in the form"
fill the form, all or none. While the form holds a change, a reduction by a person
and a download of the settings are refused, so that nothing is reduced or
saved with settings other than those shown. The form has a field per
parameter, also one that is a column: its value is the setting that each row
with an empty cell takes, and a column may have none, so its field may be left
unset (:func:`.forms.params_and_missing`). Settings that the form cannot
show, such as those kept for another version of the spec, are named in the
state's ``settings_problem`` and kept until Apply. A ``Page`` writes the
batch's settings only at Apply and at "Make this a setting": the form does
not show an assignment by code while the server runs. The parameter columns of
the batch table are the blanks of the template, so a table file loaded with
other columns, or a column made a setting or back, changes the parameters of
the cells and
of the form: the page builds both again when the columns of the batch
change, and the form then shows the batch's settings. So "Make this a
column", "Make this a setting" and "Load table" are refused while the form
holds a change, which the new form would drop.

The page in the browser holds only what it shows: the selection, the
filter, the tab. Each action returns the :class:`~.models.State` after it, and
the browser polls the state about once a second with the ``revision`` it has,
to see results arrive. The revision counts the changes made here, and those
that the batch, the manager, the runs and Follow count, which a poll compares
(such as :attr:`~ess.batch.Batch.revision`). ``start`` tells a page that
the server started again, so that the same revision may be another state.

A reduction refused raises :class:`~.errors.Rejected`. It says the reasons
that no cell or setting shows. The others stay in the state until the value of each is
changed, a setting's by Apply, or a reduction succeeds, so that the page marks
each at its cell, or at its setting while the form shows the batch's settings.
The state of the batch reports most refusals of a cell as problems of its row,
but not those that only a submission finds, since they read the records: a
reference to a record that the workspace does not know, or to an output whose
type does not fit the cell.
Any other request that cannot be done raises :class:`~.errors.Rejected`
too, with what to tell the person; :mod:`.errors` says how a refusal of
the batch layer reads, so the page copies no rule. A ``Page`` serves one
batch to one person, and takes one request at a time, under the lock of all
pages.

Statuses are named in a scientist's words (:attr:`RowState.status
<ess.batch.RowState.status>`): ``ready``, ``running``, ``done``,
``settings changed``, ``outdated``, ``failed``, ``cancelled`` and ``incomplete``. A
change of settings applies to rows not reduced yet: a done row whose result
differs from its request only at settings
(:func:`~ess.batch.with_changed_settings`) reads ``settings changed``, reduced with
earlier settings, and "Reduce" leaves it
(:func:`~ess.batch.left_to_reduce`) until a person reduces it with the
settings now. Its result counts as one of a done row: the plot and the
downloads take it. A failed or cancelled row stays so when the settings change,
since it needs reducing either way. The rows read as with the batch's
settings, whatever the form holds. While the workflow refuses the
batch's settings, no result can be said to be current, so a done row is
outdated.

The tab "Batch" shows the header of the batch, and changes its title and
description with the manager (:meth:`Page.change`). Removing a batch is
:meth:`Pages.remove <.pages.Pages.remove>`; a request to a page of a batch
removed answers :class:`~.errors.NotFound`.

The tab "Output" shows the output options of the batch
(:class:`~..writing.Writing`) in a form: the format its results are written
in, and what is said of them. The browser holds a change of it until Apply
(:meth:`Page.set_output_form`), and refuses a download meanwhile, so that the
zip of :meth:`Page.results_file` is what the form shows. Kept output options
whose format the application lacks, such as those kept by the command line
with other writers, write nothing: the tab says why
(:attr:`~.models.OutputShown.problem`) until a person applies a format that
the application has. An application given a catalogue publishes by the same
output options (:meth:`Page.publish`).

A batch follows from the time it is made, if a person chooses Follow in
"New batch" (:meth:`Pages.create <.pages.Pages.create>`). Its page then has
the action "Stop following" (:meth:`Page.stop_following`); the page offers
no action to let a stopped batch follow again
(:meth:`~ess.batch.Intake.follow`), and the page of a batch that does
not follow has no action of Follow. While a batch follows, :meth:`Pages.tick
<.pages.Pages.tick>` steps it every ``interval`` seconds
(:meth:`~ess.batch.Intake.step`), which adds the rows of its
source that are new and complete, and reduces the rows that
:func:`~ess.batch.not_reduced` gives, the rows that are ready and have
no result, under the lock of all pages, with the batch's settings, whatever
the form holds; then :meth:`Page.after_step` shows what the step changed, if
the page is open; a page made later shows the batch as it is then. The
page of the batch that follows says what the last step raised, why rows were
not reduced, at each such row, whose status stays that of its state, and how
many rows of the source are incomplete; the next step runs all the same. A
row with a result is left to a person, also one that is outdated. An action
submits only what a person asks to reduce, and the next step reduces what the
rule gives after it. Apply forgets why Follow could not reduce rows, since its
next step tries them with the settings applied. Which batch follows is kept in
the definitions, so a restart resumes.
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
    An action: it changes the revision, and returns the state with what the
    action says it did.
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
    What the API does for the page of one batch for one person, without HTTP.
    It owns, and only it writes, the draft of the settings, the refusals of
    the last reduction, and the revision. See the module for the rules.

    Parameters
    ----------
    pages:
        What the pages share: the manager of the batch, the ``Intake`` of its
        rows and Follow, the lock, the listing of the files, and what the
        application gives every page.
    label:
        The label of the batch.
    """

    def __init__(self, pages: Pages, label: str) -> None:
        self.pages = pages
        self.label = label
        pages.runs.list_again()
        self._columns: tuple[str, ...] | None = None
        self._fit()
        # The reasons of the last reduction refused that a cell or a setting shows.
        self._submit_errors: list[FieldError] = []
        self.revision = 1
        self.start = uuid.uuid4().hex[:8]
        # What :meth:`_live` gave at the last change of the revision.
        self._seen = self._live()

    @property
    def batch(self) -> Batch:
        """The batch, asked of the manager at each use."""
        return self.pages.intake.manager.batch(self.label)

    @property
    def lock(self) -> AbstractContextManager[object]:
        """The lock of all pages."""
        return self.pages.lock

    @contextmanager
    def _held(self) -> Iterator[None]:
        """
        The lock of all pages, taken for a request, with the forms fit to the
        columns of the batch; raises :class:`~.errors.NotFound` if the batch
        was removed since the page was handed out, such as by a request that
        ran between this one's check of the label and its action
        (:meth:`Pages.check <.pages.Pages.check>`).
        """
        with self.lock:
            self.pages.check(self.label)
            self._fit()
            yield

    def _fit(self) -> None:
        """
        Build the fields of the cells and the form of the settings again if
        the columns of the batch changed, such as by a table loaded; the form
        then shows the batch's settings.
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
        What the page shows that changes without an action, each by what
        counts its changes or as its value: the batch (its table and its
        template), the records listed, the files listed and why they cannot
        be (:attr:`SafeListing.revision <.runs.SafeListing.revision>`), the
        headers of the batches, which batch follows, and what Follow said of
        this batch (:meth:`LastStep.token <.last_step.LastStep.token>`).
        A record that changed since the records listed is in the feed that
        :meth:`poll` reads.
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
        """
        List again (:meth:`Runs.list_again <.runs.Runs.list_again>`), and
        give the pickers and the forms what is listed now.
        """
        self.pages.runs.list_again()
        self.draft.refresh(self.pages.runs.listing)
        self._cells.refresh(self.pages.runs.listing)

    def _show_settings(self) -> None:
        """Show the batch's settings in the form, which then holds no change."""
        self.draft.reset(self.batch.settings, self.pages.runs.listing)

    def _states(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The state of the rows of ``keys``, or of all, with the batch's settings;
        and, if those are refused, the reasons, and the rows not compared
        (:meth:`~ess.batch.Batch.shown`).
        """
        return self.batch.shown(keys)

    @_locked
    def poll(self, after: int, start: str = '') -> State | None:
        """The state, or ``None`` if it is still that of revision ``after``."""
        # The feed catches a record that ended after the last relist but before
        # ``_seen`` was read, which ``_live`` holds already and the listing lacks.
        changes = self.batch.client.changes(after=self.pages.runs.listing.cursor)
        # A table changed by another caller may name files that the listing lacks.
        if changes.records or self._live() != self._seen:
            self._list_again()
            self._new_revision()
        if after == self.revision and start == self.start:
            return None
        return self.state()

    def _table(self, keys: Iterable[str] = ()) -> pd.DataFrame:
        """
        The batch table, a copy; a ``Rejected`` unless it has a row of each of
        ``keys``.
        """
        frame = self.batch.table()
        for key in keys:
            if key not in frame.index:
                raise Rejected(f'There is no row {key}.')
        return frame

    def _set_table(self, table: pd.DataFrame) -> None:
        """Make ``table`` the batch table; what the batch refuses is a ``Rejected``."""
        try:
            self.batch.set_table(table)
        except ValueError as error:
            raise Rejected(f'{request_error_text(error)}.') from None

    @_locked
    def state(self, notice: str = '') -> State:
        """Everything the page shows but a row's details, the plot, and the runs."""
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
        # A refusal of the batch's settings is said, and marked at its field,
        # while the form shows the value refused.
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
        # An empty cell is shown empty, not as the setting or the default that
        # a request takes.
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
        """Whether a cell of a row of ``keys`` or a setting shows ``error``."""
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
        """List the files again, for the pickers and the Runs view; no row changes."""
        self._list_again()
        return 'Listed the runs again.'

    @_action
    def edit(self, edit: CellEdit) -> str:
        """Set one column of rows, as its field reads the value sent."""
        value = view.read(self._cells, edit)
        frame = self._table(edit.keys)
        # As objects, so that the batch, not pandas, refuses a cell.
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
        """Forget the reasons of a refusal at ``column`` of rows ``keys``."""
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
        """Add an empty row of ``key``; what the batch refuses is a ``Rejected``."""
        key = key.strip()
        if not key:
            raise Rejected('Give the new row a key.')
        frame = self._table()
        self._set_table(frame.reindex(pd.Index([*frame.index, key], name=KEY)))
        return f'Added {key}: fill in its cells.'

    @_locked
    def available_rows(self) -> SourceRows:
        """
        The rows of the source that the batch table lacks, those incomplete marked
        (:meth:`~ess.batch.Intake.available_rows`), for :meth:`add_rows`;
        what the source refuses is a :class:`~.errors.Rejected`
        (:func:`~.errors.reject_on_error`).
        """
        with reject_on_error('The rows of the source cannot be listed'):
            given = self.pages.intake.available_rows(self.label)
        return view.listed(given, self.batch.source_columns)

    @_action
    def add_rows(self, keys: Sequence[str]) -> str:
        """
        Add the rows of ``keys`` as the source makes them now, so that Follow
        does not add them again once removed
        (:meth:`~ess.batch.Intake.add_rows`). What ``Intake`` or the
        source refuses is a :class:`~.errors.Rejected`
        (:func:`~.errors.reject_on_error`).
        """
        with reject_on_error('No row was added'):
            self.pages.intake.add_rows(self.label, keys)
        # The rows may name files that the listing lacks.
        self._list_again()
        return f'Added {_rows(len(keys))}.'

    # Columns and settings

    @_locked
    def values(self, name: str) -> list[Value]:
        """
        The different values of the column ``name``, each with a row that has
        it (:meth:`~ess.batch.Batch.values`), for "Make this a setting".
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
        Raise :class:`~.errors.Rejected` while the form holds a change: a
        change of which parameters are columns builds the form again, which
        would drop it.
        """
        if unapplied := self.draft.unapplied():
            raise Rejected(f'Nothing was changed: {unapplied.lower()} first.')

    def _reshape(self, name: str, change: Callable[[], None]) -> None:
        """
        Make ``change`` of the columns at the parameter ``name``, unless the
        form holds a change, which the new form would drop; then forget the
        refusals at ``name``.
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
        Make the parameter ``name`` a column with empty cells, which take the
        setting (:meth:`~ess.batch.Batch.make_column`).
        """
        title = self._titles.get(name, name)
        self._reshape(name, lambda: self.batch.make_column(name))
        taken = 'the setting' if name in self.batch.settings else 'the default'
        return f'{title} is a column: a row takes {taken} until its cell is set.'

    @_action
    def make_setting(self, made: MakeSetting) -> str:
        """
        Make a column a setting, of the value that a row takes, or that every
        row takes (:meth:`~ess.batch.Batch.make_setting`).
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
        Let none follow, if this batch follows
        (:meth:`~ess.batch.Intake.stop`); the page offers no action to
        let it follow again.
        """
        if not self._follows():
            return ''  # another batch follows, or none
        self.pages.intake.stop()
        return 'Stopped following.'

    def after_step(self) -> None:
        """
        Show what a step of Follow on this batch changed: list again once, and
        refresh the forms, since new rows may name files that they do not
        offer yet. A step that changes nothing the page shows, such as the
        rows, the runs, the records, what waits and why, or how it went,
        leaves the revision, so that a page polling it gets nothing new.
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
        """Change the form of the settings; the batch's stay until Apply."""
        try:
            self.draft.edit(edit)
        except (KeyError, ValueError) as error:
            raise Rejected(request_error_text(error)) from None

    @_action
    def apply_settings(self) -> str:
        """Give the batch the settings of the form, unless one is missing."""
        given, missing = self.draft.params_and_missing()
        if missing:
            raise Rejected(
                f'Not applied: {view.field_errors_text(missing, self._titles)}.'
            )
        self.batch.settings = given
        self._show_settings()
        # A refusal of a setting was a refusal of the settings replaced, and
        # Follow tries every row again with these at its next step.
        self._submit_errors = [e for e in self._submit_errors if e.request_key]
        self.pages.last_step.clear_submit_errors(self.label)
        return 'Applied the settings.'

    @_action
    def discard_settings(self) -> None:
        """Show the batch's settings in the form again."""
        self._show_settings()

    def _fill(self, params: Mapping[str, Any]) -> None:
        """
        Show ``params`` in the form, all or none, but those that the batch
        refuses (:meth:`~ess.batch.Batch.settable`); raises as
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
        Give the batch the title and description of ``edit``
        (:meth:`~ess.batch.BatchManager.change`). What the manager
        refuses is a :class:`~.errors.Rejected`, and changes nothing.
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
        Keep the form of the tab "Output", given as its fields
        (:meth:`~..writing.Writing.set_options`).
        """
        with reject_on_error('Not applied'):
            self.pages.writing.set_options(
                self.label, OutputOptions.model_validate(fields)
            )
        return 'Applied the form.'

    @_action
    def publish(self, keys: Sequence[str]) -> str:
        """
        Publish the result of each row of ``keys``, as the output options say
        (:meth:`~..publish.Publisher.publish`). A refusal publishes nothing;
        an upload that fails keeps those before it, which its message names
        and the rows then show.
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
        What the result of row ``key`` was reduced with: the value of each
        parameter that the row took, its own cells and the settings, and
        where its request differs from the row's now.
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
        Make the rows of a table file the batch table. Its columns may
        differ, so it is refused while the form holds a change (:meth:`_settled`).
        """
        self._settled()
        try:
            params = self.batch.spec.params
            frame = read_file(name, content, lambda path: read_table(path, params))
            with reject_on_write_error():  # a fault of the definitions, not of the file
                self.batch.set_table(frame)
        except (ValueError, OSError) as error:
            raise Rejected(f'Cannot load {name}: {request_error_text(error)}') from None
        finally:
            # The rows may name files that the listing lacks, even if the
            # table is then refused.
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
        A zip of the results of the done rows among ``keys``, or of all,
        written as the form of the tab "Output" says
        (:meth:`~..writing.Writing.by_options`), with the rows as
        ``results.csv``.
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
    A reduction refused. It says the reasons that no cell or setting shows,
    such as a row still running; those that one shows are in the state.
    """

    def __init__(self, said: list[str], placed: int) -> None:
        if placed:
            said.append(
                'one value is refused.'
                if placed == 1
                else f'{placed} values are refused.'
            )
        super().__init__(f'Nothing was reduced: {"; ".join(said)}')

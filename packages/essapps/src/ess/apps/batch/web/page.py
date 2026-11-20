# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the page of one batch does, without HTTP: :class:`Page`. :mod:`.api`
serves it. This module imports neither ``fastapi`` nor ``starlette``, so
another view of a batch can call the same actions and render the same
:class:`~.models.State`.

A :class:`Page` holds what a person changes on it: the form of the
settings and the refusals. It asks :class:`~.pages.Pages` for the rest: the
:class:`~ess.apps.batch.Batch` of its label with its table and settings, kept
by the manager, the corrections of runs, and Follow. The batch and the
corrections keep their part in their definitions, if given them
(:mod:`..definition`); a change that the definitions cannot keep is not made,
and answers a :class:`Problem`.

The page shows the table of the batch as it is kept, without its source and
without the files: a cell that names a file that is not found is a problem of
its row. Each action that changes rows builds a frame with one change and
gives it to :meth:`~ess.apps.batch.Batch.put`: an edit of cells, a row added
or removed, a table file loaded, which replaces the table. A cell is plain
data (:mod:`..table`): a value that a picker gives is kept as
:func:`~..table.cell_of` makes it, and shown as the listing names it. A row is
named by its note ``name`` if the table has one, else by its key, and "Add
row" takes its key. "List runs again" lists the files again, for the pickers
and the Runs view, and changes no row. If the file source raises, the page
shows the table, the results and the plots all the same: the pickers offer
no file, the Runs view is empty, and the state says why
(:attr:`~.models.State.files_problem`).

The form of the settings is a draft of the batch's settings. An edit changes
the form only; :meth:`Page.apply_settings` writes it to the batch, and is
refused while something is missing; :meth:`Page.discard_settings` shows the
batch's settings again. "Load settings" and "Put these settings in the form"
fill the form, all or none. While the form holds a change, a reduction by a person
and a download of the settings are refused, so that nothing is reduced or
saved with settings other than those shown. The form has a field per
parameter, also one that is a column: its value is the setting that each row
with an empty cell takes, and a column may have none, so its field may be left
unset (:func:`.forms.held`). Settings that the form cannot
show, such as those kept for another version of the spec, are named in the
state's ``settings_problem`` and kept until Apply. A ``Page`` writes the
batch's settings only at Apply and at "Make this a setting": the form does
not show an assignment by code while the server runs. The columns of the
table are the blanks of the template, so a table loaded with other columns,
or a column made a setting or back, changes the parameters of the cells and
of the form: the page builds both again when the columns of the batch
change, and the form then shows the batch's settings. So "Make this a
column" and "Make this a setting" are refused while the form holds a change,
which the new form would drop.

The page in the browser holds only what it shows: the selection, the
filter, the tab. Each action returns the :class:`~.models.State` after it, and
the browser polls the state about once a second with the ``version`` it has,
to see results arrive. The version counts the changes made here, and those
that the batch, the manager, the runs and Follow count, which a poll compares
(such as :attr:`~ess.apps.batch.Batch.version`). ``start`` tells a page that
the server started again, so that the same version may be another state.

A reduction refused raises :class:`Problem`. It says the reasons that no cell
or setting shows. The others stay in the state until the value of each is
changed, a setting's by Apply, or a reduction succeeds, so that the page marks
each at its cell, or at its setting while the form shows the batch's settings.
The state of the batch reports most refusals of a cell as problems of its row,
but not those that only a submission finds, since they read the records: a
reference to a record that the workspace does not know, or to an output whose
type does not fit the cell.
Any other request that cannot be done raises :class:`Problem` too, with what
to tell the person; a refusal of the batch layer (``ValueError``) is said as
it is, so the page copies no rule. A ``Page`` serves one batch to one person,
and takes one request at a time, under the lock of all pages.

Statuses are named in a scientist's words (:attr:`RowState.text
<ess.apps.batch.RowState.text>`): ``ready``, ``running``, ``done``,
``earlier``, ``outdated``, ``failed``, ``cancelled`` and ``incomplete``. A
change of settings applies to rows not reduced yet: a done row whose result
differs from its request only at settings
(:func:`~ess.apps.batch.earlier_settings`) reads ``earlier``, reduced with
earlier settings, and "Reduce" leaves it
(:func:`~ess.apps.batch.left_to_reduce`) until a person reduces it with the
settings now. Its result counts as one of a done row: the plot and the
downloads take it. A failed or cancelled row stays so when the settings change,
since it needs reducing either way. The rows read as with the batch's
settings, whatever the form holds. While the workflow refuses the
batch's settings, no result can be said to be current, so a done row is
outdated.

The tab "Batch" shows the header of the batch, and changes its title and
description with the manager (:meth:`Page.change`). Removing a batch is
:meth:`Pages.remove <.pages.Pages.remove>`; a request to a page of a batch
removed answers :class:`Missing`.

A batch follows from the time it is made, if a person chooses Follow in
"New batch" (:meth:`Pages.create <.pages.Pages.create>`). Its page then has
the action "Stop following" (:meth:`Page.stop_following`), after which the
batch never follows again; the page of a batch that does not follow has no
action of Follow. While a batch follows, :meth:`Pages.tick
<.pages.Pages.tick>` steps it every ``interval`` seconds
(:meth:`~ess.apps.batch.BatchManager.step`), which adds the rows of its
source that are new and filled, and reduces the rows that
:func:`~ess.apps.batch.not_reduced` gives, the rows that are ready and have
no result, under the lock of all pages, with the batch's settings, whatever
the form holds; then :meth:`Page.stepped` shows what the step changed. The
page of the batch that follows says what the last step raised, why rows were
not reduced, at each such row, whose status stays that of its state, and how
many rows of the source wait to be filled; the next step runs all the same. A
row with a result is left to a person, also one that is outdated. An action
submits only what a person asks to reduce, and the next step reduces what the
rule gives after it. Apply forgets why Follow could not reduce rows, since its
next step tries them with the settings applied. Which batch follows is kept in
the definitions, so a restart resumes.
"""

from __future__ import annotations

import functools
import io
import re
import tempfile
import uuid
import zipfile
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import ValidationError

from ess.spec import File, FileRef

from ...errors import FieldError, SubmitError
from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ...records import Record, Status, Template
from .. import frames
from ..definition import NotKept
from ..export import export_table
from ..reduction import Batch
from ..state import (
    RowState,
    left_to_reduce,
    ready,
    to_reduce,
)
from ..table import KEY, as_request, cell_of, read_table, write_table
from ..templates import checked_settings, load_template, write_template
from . import forms, plots
from .models import (
    Action,
    Cell,
    CellEdit,
    Column,
    Edit,
    Following,
    HeaderEdit,
    HeaderShown,
    MakeSetting,
    Plot,
    Reduce,
    Refused,
    Row,
    RowDetail,
    SourceRow,
    SourceRows,
    State,
    Value,
)

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

NAME = 'name'
"""The note that names a row on the page; a row without one is named by its key."""

_ORDER = (
    'ready',
    'running',
    'done',
    'earlier',
    'outdated',
    'failed',
    'cancelled',
    'incomplete',
)
_ENDED = (Status.FAILED, Status.CANCELLED)
_DRAWN = 12
"""How many results the plot shows when no row is selected."""


class _Listing(Listing):
    """
    What the client lists now, as :class:`Listing` gives it, but without files
    if the file source raises: a picker then offers none, the Runs view lists
    none, and :attr:`failure` says why.
    """

    failure: str | None = None

    def files(self, marker: File) -> dict[str, FileRef]:
        try:
            return super().files(marker)
        except Exception as error:  # the file source is code of the application
            return self._failed(error, {})

    def listed(self, selector: Selector) -> dict[FileRef, dict[str, Any]]:
        try:
            return super().listed(selector)
        except Exception as error:  # the file source is code of the application
            return self._failed(error, {})

    def _failed[T](self, error: Exception, empty: T) -> T:
        self.failure = f'The files cannot be listed: {error}'
        return empty


def names(frame: pd.DataFrame, columns: Collection[str]) -> dict[str, str]:
    """
    What the page calls each row of ``frame``, a table whose parameter columns
    are ``columns``, by key: its note :data:`NAME`, else its key.
    """
    if NAME not in frame.columns or NAME in columns:
        return {key: key for key in frame.index}
    return {
        key: key if pd.isna(name) else str(name) for key, name in frame[NAME].items()
    }


def listed(given: pd.DataFrame, fills: Collection[str]) -> SourceRows:
    """
    The rows of ``given``, a table that a source makes with a column
    ``waits``, and maybe a column ``taken``, as a dialog lists them: each
    named as :func:`names` names it, with the text of each note, the columns
    but ``fills``, and its mark.
    """
    named = names(given, ())
    marks = {'waits', 'taken'}
    shown = [c for c in given.columns if c not in {NAME, *fills, *marks}]

    def text(value: Any) -> str:
        return '' if pd.api.types.is_scalar(value) and pd.isna(value) else str(value)

    def mark(row: Mapping[str, Any]) -> str:
        if row['waits']:
            return 'waits'
        if 'taken' not in row:
            return ''
        return 'taken' if row['taken'] else 'left out'

    return SourceRows(
        columns=[note(c) for c in shown],
        rows=[
            SourceRow(
                key=key,
                name=named[key],
                cells={c: text(row[c]) for c in shown},
                mark=mark(row),
            )
            for key, row in given.to_dict('index').items()
        ],
    )


class Problem(Exception):
    """A request that cannot be done, with what to tell the person."""


class Missing(Exception):
    """No batch has the label asked for."""


def _say(title: str, message: str) -> str:
    """A reason at the field ``title``, as a sentence."""
    message = forms.message(message)
    if message in ('empty', 'missing', 'Field required'):
        return f'{title} is missing'
    if message.startswith('Input should be '):
        return f'{title} must be {message.removeprefix("Input should be ")}'
    return f'{title}: {message}'


def read(fields: Fields, edit: CellEdit) -> Any:
    """The value of ``edit`` as the field of its column reads it."""
    if edit.column not in fields.fields:
        raise Problem(f'There is no column {edit.column}.')
    try:
        return forms.read(fields.fields[edit.column], edit.value)
    except (KeyError, ValueError) as error:
        raise Problem(_say(fields.titles[edit.column], why(error))) from None


def why(error: Exception) -> str:
    """What an exception of a request says, without its type or quotes."""
    if isinstance(error, ValidationError):
        return '; '.join(
            _say('.'.join(map(str, e['loc'])), e['msg']) for e in error.errors()
        )
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error)


def _failure(text: str) -> str:
    """A failure reason without the name of the exception that made it."""
    return re.sub(r'^\w+(Error|Exception): ', '', text)


def _rows(n: int) -> str:
    return f'{n} row{"s" * (n != 1)}'


@contextmanager
def named_file(name: str, content: bytes | None = None) -> Iterator[Path]:
    """A path named ``name`` in a folder of its own, holding ``content`` if given."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / Path(name).name
        if content is not None:
            path.write_bytes(content)
        yield path


def read_file[T](name: str, content: bytes, read: Callable[[Path], T]) -> T:
    """
    What ``read`` gives for the file ``name`` that a person chose, holding
    ``content``. Raises ``ValueError`` for an empty file, and for what
    ``read`` raises, with ``name`` in place of the path read.
    """
    if not content:
        raise ValueError('the file is empty')
    with named_file(name, content) as path:
        try:
            return read(path)
        except (OSError, ValueError, KeyError) as error:
            said = why(error).replace(f'{path}: ', '').replace(str(path), name)
            raise ValueError(said) from None


def note(name: str) -> Column:
    """The column of the note ``name``, titled as the page shows it."""
    return Column(name=name, title=name.replace('_', ' ').capitalize())


def _locked[**P, T](method: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(method)
    def locked(*args: P.args, **kwargs: P.kwargs) -> T:
        with args[0]._held():  # type: ignore[attr-defined]
            return method(*args, **kwargs)

    return locked


def _action[**P](method: Callable[P, str | None]) -> Callable[P, State]:
    """
    An action: it changes the version, and returns the state with what the
    action says it did.
    """

    @functools.wraps(method)
    def action(*args: P.args, **kwargs: P.kwargs) -> State:
        page: Page = args[0]  # type: ignore[assignment]
        with page._held():
            try:
                notice = method(*args, **kwargs)
            except NotKept as error:
                raise Problem(f'The change was not kept: {error}') from None
            finally:
                page._new_version()
            return page.state(notice=notice or '')

    return action


class Page:
    """
    What the API does for the page of one batch, without HTTP. See the module
    for the rules.

    Parameters
    ----------
    pages:
        What the pages share: the manager of the batch, how the last step of
        Follow went, the lock, and what the application gives every page.
    label:
        The label of the batch.
    """

    def __init__(self, pages: Pages, label: str) -> None:
        self.pages = pages
        self.label = label
        self._listing = _Listing(self.batch.client)
        self._columns: tuple[str, ...] | None = None
        self._fit()
        self.refused: list[FieldError] = []
        self.version = 1
        pages.runs_listed(self._runs_seen())
        self.start = uuid.uuid4().hex[:8]
        # What :meth:`_live` gave at the last change of the version.
        self._seen = self._live()

    @property
    def batch(self) -> Batch:
        """The batch, asked of the manager at each use."""
        return self.pages.manager.batch(self.label)

    @property
    def lock(self) -> AbstractContextManager[object]:
        """The lock of all pages."""
        return self.pages.lock

    @contextmanager
    def _held(self) -> Iterator[None]:
        """
        The lock of all pages, taken for a request, with the forms fit to the
        columns of the batch; raises :class:`Missing` if the batch was removed
        since the page was handed out, such as by a request that ran between
        this one's check of the label and its action.
        """
        with self.lock:
            if self.label not in self.pages.manager.headers():
                raise Missing(f'There is no batch {self.label}.')
            self._fit()
            yield

    def _fit(self) -> None:
        """
        Build the fields of the cells and the form of the settings again if
        the columns of the batch changed, such as by a table loaded; the form
        then shows the batch's settings.
        """
        batch = self.batch
        if batch.columns == self._columns:
            return
        self._columns = batch.columns
        self._cells = Fields(batch.spec.params, self._listing, fields=batch.columns)
        self.settings = self._form()
        self._show_settings()

    def _follows(self) -> bool:
        """Whether this batch is the one that follows."""
        return self.pages.manager.following == self.label

    # Reading

    def _live(self) -> tuple[object, ...]:
        """
        What the page shows that changes without an action, each by what
        counts its changes: the batch (its table, its template and the
        records), the records listed, the runs and why they cannot be
        listed, the headers of the batches and which batch follows, and how
        the last step of Follow went.
        """
        pages = self.pages
        return (
            self.batch.version,
            self._listing.cursor,
            self._listing.failure,
            pages.runs_version,
            pages.manager.version,
            pages.last,
            pages.failure,
        )

    def _new_version(self) -> None:
        self.version += 1
        self._seen = self._live()

    def _relist(self) -> None:
        self._listing = _Listing(self.batch.client, previous=self._listing)
        for fields in (self.settings, self._cells):
            fields.refresh(self._listing)

    def _runs_seen(self) -> dict[FileRef, dict[str, Any]]:
        """The runs that the Runs view lists, with their metadata."""
        return self._listing.listed(Selector()) if self.pages.corrects_runs else {}

    def _form(self) -> Fields:
        """A form of the settings, a field per parameter, at their defaults."""
        return Fields(self.batch.spec.params, self._listing)

    def _form_held(self) -> tuple[dict[str, Any], list[FieldError]]:
        """
        What the form holds; a column left unset has no setting
        (:func:`.forms.held`).
        """
        return forms.held(self.settings, self.batch.columns)

    def _show_settings(self) -> None:
        """Show the batch's settings in the form, which then holds no change."""
        # Settings kept for another version of the spec may not fit the form;
        # the batch keeps them, and the page names them, until Apply.
        self._unfit = self.settings.set_fitting(self.batch.settings, self._listing)
        # What the form holds while it shows the batch's settings.
        self._shown = self._form_held()

    def _unapplied(self) -> str | None:
        """
        What a person does first while the form holds a change, since a
        reduction would use settings other than those shown; else ``None``.
        """
        held = self._form_held()
        if held == self._shown:
            return None
        return f'{"Fill in" if held[1] else "Apply"} or discard the settings'

    def _states(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The state of the rows of ``keys``, or of all, with the batch's settings;
        and, if those are refused, the reasons, and the rows not compared
        (:meth:`~ess.apps.batch.Batch.shown`).
        """
        return self.batch.shown(keys)

    @_locked
    def poll(self, after: int, start: str = '') -> State | None:
        """The state, or ``None`` if it is still that of version ``after``."""
        # The feed catches a record that ended after the last relist but before
        # ``_seen`` was read, which ``_live`` holds already and the listing lacks.
        changes = self.batch.client.changes(after=self._listing.cursor)
        # A table changed by another caller may name files that the listing lacks.
        if changes.records or self._live() != self._seen:
            self._relist()
            self._new_version()
        if after == self.version and start == self.start:
            return None
        return self.state()

    def _frame(self, keys: Iterable[str] = ()) -> pd.DataFrame:
        """The table, a copy; a ``Problem`` unless it has a row of each of ``keys``."""
        frame = self.batch.frame()
        for key in keys:
            if key not in frame.index:
                raise Problem(f'There is no row {key}.')
        return frame

    def _put(self, frame: pd.DataFrame) -> None:
        """Make ``frame`` the table; what the batch refuses is a ``Problem``."""
        try:
            self.batch.put(frame)
        except ValueError as error:
            raise Problem(f'{why(error)}.') from None

    def _setting_title(self, error: FieldError) -> str:
        return self.settings.titles.get(str(error.loc[0]), str(error.loc[0]))

    @_locked
    def state(self, notice: str = '') -> State:
        """Everything the page shows but a row's details, the plot, and the runs."""
        batch = self.batch
        columns = batch.columns
        states, unusable = self._states()
        unsubmitted = self.pages.unsubmitted(self.label, states)
        failures = self._failures(states)
        frame = batch.frame()
        requested = batch.cells()
        named = names(frame, columns)
        notes = [n for n in frame.columns if n not in columns and n != NAME]
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
            )
            for key, cells in frame.to_dict('index').items()
        ]
        counts = dict.fromkeys(_ORDER, 0)
        for s in states.values():
            counts[s.text] += 1
        given, missing = self._form_held()

        # A refusal of the batch's settings is said, and marked at its field,
        # while the form shows the value refused.
        def shown(error: FieldError) -> bool:
            name = str(error.loc[0]) if error.loc else ''
            return not name or given.get(name) == self._shown[0].get(name)

        errors = [*missing, *filter(shown, unusable)]
        said = [self._said(errors)] if errors else []
        if self._unfit:
            said.insert(0, f'Settings that this form cannot show: {_join(self._unfit)}')
        problem = '; '.join(said) or None
        unapplied = self._unapplied()
        marks = [e for e in (*unusable, *self.refused) if not e.entry and shown(e)]
        fields = batch.spec.params.model_fields
        header = self.pages.manager.headers()[self.label]
        return State(
            version=self.version,
            start=self.start,
            runs=self.pages.runs_version if self.pages.corrects_runs else None,
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
            notes=[note(n) for n in notes],
            columns=[
                Column(
                    name=c,
                    title=self._cells.titles[c],
                    editor=forms.editor(self._cells.fields[c]),
                    default=self._text(batch.settings[c])
                    if c in batch.settings
                    else ''
                    if fields[c].is_required()
                    else self._text(fields[c].get_default(call_default_factory=True)),
                )
                for c in columns
            ],
            rows=rows,
            counts={'all': len(states), **counts},
            action=self._primary(states, unapplied or problem, unapplied),
            settings=forms.tree(self.settings, marks, columns),
            settings_problem=problem,
            settings_changed=unapplied is not None,
            may_be_columns=[n for n in forms.edited(self.settings) if n not in columns],
            refused=[self._refused(e, named) for e in self.refused],
            follow=self._follow(),
            files_problem=self._listing.failure,
            notice=notice,
        )

    def _said(self, errors: Sequence[FieldError]) -> str:
        """Errors of the settings in words, each with its setting's title."""
        return '; '.join(
            _say(self._setting_title(e), e.msg) if e.loc else e.msg for e in errors
        )

    def _follow(self) -> Following:
        manager, pages = self.pages.manager, self.pages
        on, last = self._follows(), pages.last
        return Following(
            on=on,
            source=manager.source(self.label),
            interval=pages.interval,
            failure=pages.failure if on else None,
            waiting=len(last[1].waiting)
            if on and last is not None and last[0] == self.label
            else 0,
        )

    def _failures(self, states: Mapping[str, RowState]) -> dict[str, str]:
        ended = [
            s.record
            for s in states.values()
            if s.record is not None and s.status in _ENDED
        ]
        reasons = self.batch.client.failure(ended)
        return {
            r.id: _failure(why) if why else 'Cancelled.'
            for r, why in zip(ended, reasons, strict=True)
        }

    def _text(self, value: Any) -> str:
        """What a cell shows (:meth:`Listing.text`); nothing if it is unset."""
        return '' if value is UNSET else self._listing.text(value)

    def _outdated(self, state: RowState) -> str:
        """Why the result of a row is outdated: what changed since."""
        said = []
        if changed := self._changed(state, self.batch.spec.params.model_fields):
            said.append(f'{changed} changed.')
        if state.spec_changed:
            said.append('It was reduced by another version of the workflow.')
        return ' '.join(said)

    def _changed(self, state: RowState, names: Iterable[str]) -> str:
        """The titles of the parameters of ``names`` that changed, in words."""
        titles = {**self.settings.titles, **self._cells.titles}
        return _join([titles.get(n, n) for n in names if n in (state.changed or ())])

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
    ) -> Row:
        titles = {**self.settings.titles, **self._cells.titles}
        problems = {
            str(e.loc[0]): _say(titles.get(str(e.loc[0]), str(e.loc[0])), e.msg)
            for e in (*state.problems, *self.refused)
            if e.entry == key and e.loc
        }
        record = state.record
        status = state.text
        reason = changed = ''
        if unsubmitted:
            reason = unsubmitted
        elif status in ('failed', 'cancelled') and record is not None:
            reason = failures.get(record.id, '')
        elif problems:
            reason = '; '.join(problems.values())
        elif status == 'earlier':
            if named := self._changed(state, self.batch.spec.params.model_fields):
                changed = f'{named} changed'
            since = f': {changed} since' if changed else ''
            reason = (
                f'Reduced with earlier settings{since}. "Reduce" leaves such rows;'
                ' "Reduce rows of earlier settings" reduces them with the settings now.'
            )
        elif status == 'outdated' and state.outdated:
            reason = self._outdated(state)
        elif status == 'outdated':
            reason = 'The settings cannot be used, so this result may not match them.'
        # An empty cell is shown empty, not as the setting or the default that
        # a request takes.
        given = {c: requested[c] for c in requested if cells[c] is not None}
        result = None
        if record is not None and state.status is Status.COMPLETED:
            result = 'outdated' if status == 'outdated' else 'current'
        return Row(
            key=key,
            name=name,
            notes={n: '' if cells[n] is None else str(cells[n]) for n in notes},
            status=status,
            reason=reason,
            changed=changed,
            record=None if record is None else record.id,
            result=result,
            cells={
                c: Cell(
                    value=forms.cell(self._cells.fields[c], given.get(c, UNSET)),
                    text=self._text(given.get(c, UNSET)),
                    problem=problems.get(c),
                )
                for c in self.batch.columns
            },
        )

    def _primary(
        self,
        states: Mapping[str, RowState],
        blocked: str | None,
        label: str | None = None,
    ) -> Action:
        """What the button does now, or ``label`` if blocked; how far rows run."""
        left = [] if blocked else left_to_reduce(states)
        kinds = {states[k].text for k in left}
        n = len(left)
        if blocked:
            label = label or 'Check the settings'
        elif not left:
            label = 'Nothing left to reduce'
        elif kinds == {'failed'}:
            label = f'Retry {n} failed'
        elif kinds == {'cancelled'}:
            label = f'Retry {n} cancelled'
        elif kinds <= {'failed', 'cancelled'}:
            label = f'Retry {n}'
        elif kinds == {'outdated'}:
            label = f'Reduce {n} outdated'
        else:
            label = f'Reduce {n} remaining'
        return Action(
            label=label,
            blocked=blocked,
            remaining=n,
            again=len(ready(states)),
            running=sum(s.status is Status.PENDING for s in states.values()),
            progress=_progress(states),
        )

    def _placed(self, error: FieldError, keys: Iterable[str]) -> bool:
        """Whether a cell of a row of ``keys`` or a setting shows ``error``."""
        if not error.loc:
            return False
        if error.entry is None:
            return str(error.loc[0]) in self.settings.fields
        return error.entry in keys and str(error.loc[0]) in self.batch.columns

    def _refused(self, error: FieldError, named: Mapping[str, str]) -> Refused:
        if error.entry is None:
            text = (
                _say(self._setting_title(error), error.msg) if error.loc else error.msg
            )
        else:
            column = str(error.loc[0]) if error.loc else ''
            said = _say(self._cells.titles.get(column, column), error.msg)
            name = named.get(error.entry, error.entry)
            text = f'{name}: {said if error.loc else error.msg}'
        return Refused(
            entry=error.entry,
            loc=list(error.loc),
            message=error.msg,
            text=text,
        )

    # The rows

    @_action
    def relist(self) -> str:
        """List the files again, for the pickers and the Runs view; no row changes."""
        self._relist()
        self.pages.runs_listed(self._runs_seen())
        return 'Listed the runs again.'

    @_action
    def edit(self, edit: CellEdit) -> str:
        """Set one column of rows, as its field reads the value sent."""
        value = read(self._cells, edit)
        frame = self._frame(edit.keys)
        # As objects, so that the batch, not pandas, refuses a cell.
        column = frame[edit.column].astype(object)
        column.loc[edit.keys] = (
            None if value is UNSET or value is None else cell_of(value)
        )
        self._put(frame.assign(**{edit.column: column}))
        self._forget(edit.keys, edit.column)
        if len(edit.keys) < 2:
            return ''
        title = self._cells.titles[edit.column]
        return f'{title} set on {_rows(len(edit.keys))}.'

    def _forget(self, keys: Sequence[str], column: str) -> None:
        """Forget the reasons of a refusal at ``column`` of rows ``keys``."""
        self.refused = [
            e
            for e in self.refused
            if not (e.entry in keys and e.loc and str(e.loc[0]) == column)
        ]

    @_action
    def remove(self, keys: Sequence[str]) -> str:
        """Remove rows from the table."""
        self._put(self._frame(keys).drop(index=list(keys)))
        return f'Removed {_rows(len(keys))}.'

    @_action
    def add(self, key: str) -> str:
        """Add an empty row of ``key``; what the batch refuses is a ``Problem``."""
        key = key.strip()
        if not key:
            raise Problem('Give the new row a key.')
        frame = self._frame()
        self._put(frame.reindex(pd.Index([*frame.index, key], name=KEY)))
        return f'Added {key}: fill in its cells.'

    @_locked
    def offered(self) -> SourceRows:
        """
        The rows of the source that the table lacks, those that wait marked
        (:meth:`~ess.apps.batch.BatchManager.offered`), for :meth:`take`.
        """
        manager = self.pages.manager
        try:
            given = manager.offered(self.label)
        except Exception as error:  # a source is code of the application
            raise Problem(
                f'The rows of the source cannot be listed: {why(error)}.'
            ) from None
        return listed(given, manager.sources[manager.source(self.label)].fills)

    @_action
    def take(self, keys: Sequence[str]) -> str:
        """
        Add the rows of ``keys`` as the source makes them now, so that Follow
        does not add them again once removed
        (:meth:`~ess.apps.batch.BatchManager.take`).
        """
        try:
            self.pages.manager.take(self.label, keys)
        except NotKept:
            raise
        except Exception as error:  # a source is code of the application
            raise Problem(f'No row was added: {why(error)}.') from None
        # The rows may name files that the listing lacks.
        self._relist()
        return f'Added {_rows(len(keys))}.'

    # Columns and settings

    @_locked
    def values(self, name: str) -> list[Value]:
        """
        The different values of the column ``name``, each with a row that has
        it (:meth:`~ess.apps.batch.Batch.values`), for "Make this a setting".
        """
        try:
            values = self.batch.values(name)
        except ValueError as error:
            raise Problem(f'{why(error)}.') from None
        model = self.batch.spec.params
        return [
            Value(
                key=key,
                text=''
                if cell is None
                else self._text(as_request(model, {name: cell})[name]),
            )
            for key, cell in values.items()
        ]

    def _reshape(self, name: str, change: Callable[[], None]) -> None:
        """
        Make ``change`` of the columns at the parameter ``name``, unless the
        form holds a change, which the new form would drop; then forget the
        refusals at ``name``.
        """
        if unapplied := self._unapplied():
            raise Problem(f'Nothing was changed: {unapplied.lower()} first.')
        try:
            change()
        except ValueError as error:
            raise Problem(f'Nothing was changed: {why(error)}.') from None
        self.refused = [e for e in self.refused if not (e.loc and e.loc[0] == name)]

    @_action
    def make_column(self, name: str) -> str:
        """
        Make the parameter ``name`` a column with empty cells, which take the
        setting (:meth:`~ess.apps.batch.Batch.make_column`).
        """
        title = self.settings.titles.get(name, name)
        self._reshape(name, lambda: self.batch.make_column(name))
        return f'{title} is a column: a row takes the setting until its cell is set.'

    @_action
    def make_setting(self, made: MakeSetting) -> str:
        """
        Make a column a setting, of the value that a row takes, or that every
        row takes (:meth:`~ess.apps.batch.Batch.make_setting`).
        """
        title = self._cells.titles.get(made.name, made.name)
        self._reshape(made.name, lambda: self.batch.make_setting(made.name, made.key))
        return f'{title} is a setting: one value for every row.'

    # Reducing

    @_action
    def reduce(self, request: Reduce) -> str:
        """Submit the rows asked for, all or none."""
        frame = self._frame(request.keys)
        if unapplied := self._unapplied():
            raise Problem(f'Nothing was reduced: {unapplied.lower()}.')
        try:
            keys = (
                request.keys
                if request.which == 'selected'
                else to_reduce(self.batch.state(), request.which)
            )
            records = self.batch.reduce(keys)
        except SubmitError as error:
            self.refused = [e for e in error.errors if self._placed(e, frame.index)]
            unplaced = [e for e in error.errors if e not in self.refused]
            named = names(frame, self.batch.columns)
            raise _Refusal(
                [self._refused(e, named).text for e in unplaced], len(self.refused)
            ) from None
        self.refused = []
        self._relist()
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
        (:meth:`~ess.apps.batch.BatchManager.stop`); it cannot follow again.
        """
        if not self._follows():
            return ''  # another batch follows, or none
        self.pages.manager.stop()
        return 'Stopped following.'

    def stepped(self) -> None:
        """
        Show what a step of Follow on this batch changed: list again once, and
        refresh the forms, since new rows may name files that they do not
        offer yet. A step that changes nothing the page shows, such as the
        rows, the runs, the records, what waits and why, or how it went,
        leaves the version, so that a page polling it gets nothing new.
        """
        with self.lock:
            try:
                self._relist()
                self.pages.runs_listed(self._runs_seen())
            finally:
                if self._live() != self._seen:
                    self._new_version()

    # Settings

    @_action
    def edit_settings(self, edit: Edit) -> None:
        """Change the form of the settings; the batch's stay until Apply."""
        try:
            forms.apply(self.settings, edit)
        except (KeyError, ValueError) as error:
            raise Problem(why(error)) from None

    @_action
    def apply_settings(self) -> str:
        """Give the batch the settings of the form, unless one is missing."""
        given, missing = self._form_held()
        if missing:
            raise Problem(f'Not applied: {self._said(missing)}.')
        self.batch.settings = given
        self._show_settings()
        # A refusal of a setting was a refusal of the settings replaced, and
        # Follow tries every row again with these at its next step.
        self.refused = [e for e in self.refused if e.entry]
        if self._follows() and (last := self.pages.last) is not None:
            self.pages.last = (last[0], replace(last[1], refused={}))
        return 'Applied the settings.'

    @_action
    def discard_settings(self) -> None:
        """Show the batch's settings in the form again."""
        self._show_settings()

    def _fill(self, params: Mapping[str, Any]) -> None:
        """Show ``params`` in the form, all or none; raises as :meth:`Fields.set`."""
        form = self._form()
        form.set(params, self._listing)
        self.settings = form

    @_action
    def use_settings(self, key: str) -> str:
        """
        Show the settings that the result of row ``key`` was reduced with; at
        a column, the batch's setting, since the row's value there is its own
        or the setting.
        """
        name = names(self._frame([key]), self.batch.columns)[key]
        row = self._states([key])[0][key]
        if row.record is None:
            raise Problem(f'{name} has no result.')
        if row.spec_changed:
            raise Problem(
                f'{name} was reduced by another version of the workflow, '
                'whose settings this one does not take.'
            )
        batch = self.batch
        params = row.record.request.params
        self._fill(
            {
                **{n: v for n, v in params.items() if n not in batch.columns},
                **{n: v for n, v in batch.settings.items() if n in batch.columns},
            }
        )
        return (
            f'The form shows the settings {name} was reduced with. Apply to use them.'
        )

    # The header

    @_action
    def change(self, edit: HeaderEdit) -> str:
        """
        Give the batch the title and description of ``edit``
        (:meth:`~ess.apps.batch.BatchManager.change`). What the manager
        refuses is a :class:`Problem`, and changes nothing.
        """
        try:
            self.pages.manager.change(
                self.label, title=edit.title, description=edit.description
            )
        except ValueError as error:
            raise Problem(f'The batch was not changed: {why(error)}.') from None
        return 'Saved.'

    # One row

    @_locked
    def row(self, key: str) -> RowDetail:
        """What the result of row ``key`` was reduced with."""
        self._frame([key])
        state = self._states([key])[0][key]
        record = state.record
        if record is None or state.spec_changed:
            # another spec's params are not this one's
            return RowDetail(key=key, reduced_with=None, created=None, differs=[])
        batch = self.batch
        given = Fields(batch.spec.params, self._listing, fields=batch.settings_fields)
        given.set(record.request.params, self._listing)
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
        frame = self.batch.frame()
        order = {key: i for i, key in enumerate(frame.index)}
        states, _ = self._states(
            sorted({k for k in keys if k in order}, key=order.__getitem__)
            if keys
            else None
        )
        done = [
            s
            for s in states.values()
            if s.record is not None and s.status is Status.COMPLETED
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
        named = names(frame, self.batch.columns)
        shown = [
            Shown(s.key, named[s.key], s.record, s.text == 'outdated', c)
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
            return Plot(figure=None, note=f'Cannot draw: {error}')

    # Files

    @_locked
    def table_file(self, suffix: str) -> bytes:
        """The table with what came of each row, as a ``.csv`` or ``.xlsx`` file."""
        batch = self.batch
        table = frames.with_results(batch.client, self._states()[0], batch.frame())
        with named_file(f'rows{suffix}') as path:
            write_table(table, path)
            return path.read_bytes()

    @_action
    def load_table(self, name: str, content: bytes) -> str:
        """Make the table of a table file the table of the batch."""
        try:
            params = self.batch.spec.params
            frame = read_file(name, content, lambda path: read_table(path, params))
            self.batch.put(frame)
        except NotKept:
            raise  # a fault of the definitions, not of the file
        except (OSError, ValueError, KeyError) as error:
            raise Problem(f'Cannot load {name}: {why(error)}') from None
        finally:
            # The rows may name files that the listing lacks, even if the
            # table is then refused.
            self._relist()
        return f'Loaded {_rows(len(frame))} from {name}.'

    @_locked
    def settings_file(self) -> bytes:
        """The batch's settings as a YAML template, those at the columns included."""
        if unapplied := self._unapplied():
            raise Problem(f'{unapplied} first.')
        if missing := self._form_held()[1]:
            raise Problem(f'Fill in the settings first: {SubmitError(missing)}')
        batch = self.batch
        with named_file('settings.yaml') as path:
            write_template(Template(batch.spec, params=batch.settings), path)
            return path.read_bytes()

    @_action
    def load_settings(self, name: str, content: bytes) -> str:
        """Show the settings of a YAML template in the form, all or none."""
        cannot = f'Cannot load {name}:'
        with named_file('settings.yaml', content) as path:
            try:
                template = load_template(path)
            except (OSError, ValueError):
                raise Problem(
                    f'{cannot} it is not a settings file saved from this page.'
                ) from None
        try:
            batch = self.batch
            self._fill(checked_settings(batch.client, batch.spec, template))
        except (ValueError, KeyError) as error:
            raise Problem(f'{cannot} {forms.message(why(error))}.') from None
        return f'The form shows the settings of {name}. Apply to use them.'

    @_locked
    def results_file(self, keys: Sequence[str]) -> bytes:
        """A zip of the outputs of the done rows among ``keys``, or of all."""
        batch = self.batch
        frame = batch.frame()
        known = [k for k in keys if k in frame.index]
        states, _ = self._states(known if keys else None)
        done = [k for k, s in states.items() if s.status is Status.COMPLETED]
        if not done:
            raise Problem('None of these rows has a result.')
        buffer = io.BytesIO()
        with named_file('results') as folder:
            table = frames.with_results(batch.client, states, frame.loc[done])
            exported = export_table(batch.client, table, folder)
            write_table(exported, folder / 'results.csv')
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(folder.parent))
        return buffer.getvalue()


class _Refusal(Problem):
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


def _join(words: Sequence[str]) -> str:
    """``['a', 'b', 'c']`` as ``'a, b and c'``."""
    return ' and '.join([', '.join(words[:-1]), words[-1]] if len(words) > 1 else words)


def _progress(states: Mapping[str, RowState]) -> tuple[int, int] | None:
    """
    Of the rows whose results were submitted since the first still running,
    how many are finished and how many there are; ``None`` if none is running.
    """
    pending = [
        s.record for s in states.values() if s.status is Status.PENDING and s.record
    ]
    if not pending:
        return None
    since = min(r.created for r in pending)
    started = [
        s for s in states.values() if s.record is not None and s.record.created >= since
    ]
    return sum(s.status is not Status.PENDING for s in started), len(started)

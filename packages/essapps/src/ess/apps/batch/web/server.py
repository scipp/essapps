# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the batch page does, without HTTP: :class:`Server`. :mod:`.api` serves it.

The server holds everything a person changes: the rows of the
:class:`~ess.apps.batch.Batch`, the settings, the form of a reduction of one
row alone, and the corrections of runs. The page holds only what it shows: the
selection, the filter, the tab. Each action returns the :class:`~.models.State`
after it, and the page polls the state about once a second with the
``version`` it has, to see results arrive. The version counts the changes made
here and those of the client's records, which the server reads from
``client.changes`` at each poll. ``start`` tells a page that the server started
again, so that the same version may be another state.

A reduction refused raises :class:`Problem` and keeps its reasons in the state
until the value of each is changed or a reduction succeeds, so that the page
marks each at its cell or its setting. Any other request that cannot be done
raises :class:`Problem` too, with what to tell the person. One server serves
one batch to one person, and takes one request at a time.

Statuses are named in a scientist's words: a row that is ``not reduced`` is
``ready``, a ``pending`` one ``running``, a ``completed`` one ``done``, and a
``stale`` one ``outdated``. A failed or cancelled row stays so when the
settings change, since it needs reducing either way. While the settings are
incomplete, no result can be said to be current, so a done row is outdated.
"""

from __future__ import annotations

import functools
import io
import re
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ess.spec import FileRef

from ...errors import FieldError, SubmitError
from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ...records import Record, SpecId, Status
from .. import EMPTY, Batch, CorrectedFiles, RowState, frames, ready, remaining
from .. import Row as TableRow
from ..export import export_table
from ..text import load_template, with_latest, with_named_files, write_template
from . import forms, plots
from .models import (
    Action,
    Cell,
    CellEdit,
    Cells,
    Column,
    Edit,
    Plot,
    Reduce,
    Refused,
    Row,
    RowDetail,
    RunRow,
    Runs,
    State,
    Words,
)


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

_STATUS = {
    'not reduced': 'ready',
    'pending': 'running',
    'completed': 'done',
    'stale': 'outdated',
}
_ORDER = ('ready', 'running', 'done', 'outdated', 'failed', 'cancelled', 'incomplete')
_ENDED = (Status.FAILED, Status.CANCELLED)
_DRAWN = 12
"""How many results the plot shows when no row is selected."""


class Problem(Exception):
    """A request that cannot be done, with what to tell the person."""


def _say(title: str, message: str) -> str:
    """A reason at the field ``title``, as a sentence."""
    message = forms.message(message)
    if message in ('empty', 'missing', 'Field required'):
        return f'{title} is missing'
    if message.startswith('Input should be '):
        return f'{title} must be {message.removeprefix("Input should be ")}'
    return f'{title}: {message}'


def _why(error: Exception) -> str:
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


@contextmanager
def _file(name: str, content: bytes | None = None) -> Iterator[Path]:
    """A path named ``name`` in a folder of its own, holding ``content`` if given."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / Path(name).name
        if content is not None:
            path.write_bytes(content)
        yield path


def _locked[**P, T](method: Callable[P, T]) -> Callable[P, T]:
    @functools.wraps(method)
    def locked(*args: P.args, **kwargs: P.kwargs) -> T:
        with args[0].lock:  # type: ignore[attr-defined]
            return method(*args, **kwargs)

    return locked


def _action[**P](method: Callable[P, str | None]) -> Callable[P, State]:
    """
    An action: it changes the version, and returns the state with what the
    action says it did.
    """

    @functools.wraps(method)
    def action(*args: P.args, **kwargs: P.kwargs) -> State:
        server: Server = args[0]  # type: ignore[assignment]
        with server.lock:
            try:
                notice = method(*args, **kwargs)
            finally:
                server.version += 1
            return server.state(notice=notice or '')

    return action


class Server:
    """
    What the API does, without HTTP. See the module for the rules.

    Parameters
    ----------
    batch:
        The batch, scanned or not.
    title:
        The title of the page; the title of the spec if not given.
    words:
        What a row is called.
    name:
        The entry of :attr:`Row.info` that names a row; its key if ``None``.
    info:
        Other entries of :attr:`Row.info` to show as columns.
    draw:
        Draws results (see :data:`Draw`).
    files:
        The file source of the client, to correct runs in.
    marks:
        The fields of a run that a person corrects, as a pydantic model.
    folder:
        What the header names as the place of the data, such as a folder.
    """

    def __init__(
        self,
        batch: Batch,
        *,
        title: str | None = None,
        words: Words | None = None,
        name: str | None = None,
        info: Sequence[str] = (),
        draw: Draw | None = None,
        files: CorrectedFiles | None = None,
        marks: type[BaseModel] | None = None,
        folder: str = '',
    ) -> None:
        self.lock = threading.RLock()
        self.batch = batch
        self.title = title or batch.spec.title
        self.words = words or Words()
        self._name = name
        self._info = tuple(info)
        self._draw = draw
        self._files = files
        self._folder = folder
        self._listing = Listing(batch.client)
        model, listing = batch.spec.params, self._listing
        self.settings = Fields(model, listing, fields=batch.settings_fields)
        self._cells = Fields(model, listing, fields=batch.columns)
        self.tweak = Fields(model, listing)
        self._tweak_of: tuple[str, Any, str | None] | None = None
        self._marks = None if marks is None or files is None else Fields(marks, listing)
        # The rows whose result was reduced alone, as Batch keeps them too.
        self._alone: set[str] = set()
        self.refused: list[FieldError] = []
        self.loaded: str | None = None
        self.version = 1
        self.runs_version = 1
        self.start = uuid.uuid4().hex[:8]

    # Reading

    def _relist(self) -> None:
        self._listing = Listing(self.batch.client, previous=self._listing)
        for fields in (self.settings, self._cells, self.tweak, self._marks):
            if fields is not None:
                fields.refresh(self._listing)

    def _settings(self) -> dict[str, Any] | None:
        try:
            return self.settings.params
        except SubmitError:
            return None

    def _states(self) -> dict[str, RowState]:
        return self.batch.state(self._settings())

    def _status(self, state: RowState, settings_ok: bool) -> str:
        if state.status in _ENDED and not state.problems:
            return str(state.status)
        if (
            not settings_ok
            and state.status is Status.COMPLETED
            and state.key not in self._alone
        ):
            return 'outdated'
        return _STATUS.get(state.text, state.text)

    @_locked
    def poll(self, after: int, start: str = '') -> State | None:
        """The state, or ``None`` if it is still that of version ``after``."""
        if self.batch.client.changes(after=self._listing.cursor).records:
            self._relist()
            self.version += 1
        if after == self.version and start == self.start:
            return None
        return self.state()

    def _row_of(self, key: str) -> TableRow:
        if key not in self.batch.rows:
            raise Problem(f'There is no {self.words.one} {key}.')
        return self.batch.rows[key]

    def _named(self, row: TableRow) -> str:
        return (row.info.get(self._name) if self._name else None) or row.key

    def _setting_title(self, error: FieldError) -> str:
        return self.settings.titles.get(str(error.loc[0]), str(error.loc[0]))

    @_locked
    def state(self, notice: str = '') -> State:
        """Everything the page shows but a row's details, the plot, and the runs."""
        settings = self._settings()
        states = self.batch.state(settings)
        statuses = {k: self._status(s, settings is not None) for k, s in states.items()}
        failures = self._failures(states)
        scan = {row.key: row for row in self.batch.rows.scan}
        added = {row.key for row in self.batch.rows.added}
        rows = [
            self._row(
                row, settings, states[row.key], statuses[row.key], failures, scan, added
            )
            for row in self.batch.rows
        ]
        counts = dict.fromkeys(_ORDER, 0)
        for status in statuses.values():
            counts[status] += 1
        problem = None
        if settings is None:
            _, missing = self.settings.held()
            said = [_say(self._setting_title(e), e.msg) for e in missing if e.loc]
            problem = '; '.join(said) or 'The settings are incomplete.'
        fields = self.batch.spec.params.model_fields
        return State(
            version=self.version,
            start=self.start,
            runs=None if self._marks is None else self.runs_version,
            title=self.title,
            words=self.words,
            folder=self._folder,
            info=[
                Column(name=n, title=n.replace('_', ' ').capitalize())
                for n in self._info
            ],
            columns=[
                Column(
                    name=c,
                    title=self._cells.titles[c],
                    editor=forms.editor(self._cells.fields[c]),
                    default=''
                    if fields[c].is_required()
                    else self._text(fields[c].get_default(call_default_factory=True)),
                )
                for c in self.batch.columns
            ],
            rows=rows,
            counts={'all': len(states), **counts},
            removed=len(scan.keys() & self.batch.rows.removed),
            action=self._primary(states, statuses, problem),
            settings=forms.tree(
                self.settings, [e for e in self.refused if not e.entry]
            ),
            settings_problem=problem,
            refused=[self._refused(e) for e in self.refused],
            loaded=self.loaded,
            notice=notice,
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

    def _changed(
        self, row: TableRow, given: Mapping[str, Any], settings: Mapping[str, Any]
    ) -> list[str]:
        """
        The titles of the cells and ``settings`` that differ from those
        ``given`` to the reduction of ``row``; the settings count only if it
        was not reduced alone.
        """
        now = {} if row.key in self._alone else dict(settings)
        now.update({c: v for c, v in row.values.items() if c in self.batch.columns})
        titles = {**self.settings.titles, **self._cells.titles}
        return [
            titles.get(n, n)
            for n in now
            if forms.jsonable(given.get(n)) != forms.jsonable(now[n])
        ]

    def _row(
        self,
        row: TableRow,
        settings: Mapping[str, Any] | None,
        state: RowState,
        status: str,
        failures: Mapping[str, str],
        scan: Mapping[str, TableRow],
        added: set[str],
    ) -> Row:
        edits = self.batch.rows.edits.get(row.key, {})
        titles = self._cells.titles
        problems = {
            str(e.loc[0]): _say(titles.get(str(e.loc[0]), str(e.loc[0])), e.msg)
            for e in (*state.problems, *self.refused)
            if e.entry == row.key and e.loc
        }
        record = state.record
        reason = ''
        if status in ('failed', 'cancelled') and record is not None:
            reason = failures.get(record.id, '')
        elif problems:
            reason = '; '.join(problems.values())
        elif status == 'outdated' and state.stale and record is not None:
            changed = self._changed(row, record.request.params, settings or {})
            reason = f'{_join(changed)} changed.' if changed else 'Its cells changed.'
        elif status == 'outdated':
            reason = 'The settings are incomplete, so this result may not match them.'
        scanned = scan.get(row.key)
        cells = {
            c: Cell(
                value=forms.cell(self._cells.fields[c], row.values.get(c, UNSET)),
                text=self._text(row.values.get(c, UNSET)),
                edited=c in edits,
                scanned=''
                if scanned is None
                else self._text(scanned.values.get(c, UNSET)),
                problem=problems.get(c),
            )
            for c in self.batch.columns
        }
        result = None
        if record is not None and state.status is Status.COMPLETED:
            result = 'outdated' if status == 'outdated' else 'current'
        return Row(
            key=row.key,
            name=self._named(row),
            info={n: row.info.get(n, '') for n in self._info},
            status=status,
            reason=reason,
            added=row.key in added,
            alone=row.key in self._alone and record is not None,
            record=None if record is None else record.id,
            result=result,
            cells=cells,
        )

    def _primary(
        self,
        states: Mapping[str, RowState],
        statuses: Mapping[str, str],
        blocked: str | None,
    ) -> Action:
        """What the primary button does now, and how far the rows running are."""
        left = remaining(states)
        kinds = {statuses[k] for k in left}
        n = len(left)
        if blocked:
            label = 'Settings incomplete'
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

    def _placed(self, error: FieldError) -> bool:
        if not error.loc:
            return False
        if error.entry is None:
            return str(error.loc[0]) in self.settings.fields
        return (
            error.entry in self.batch.rows and str(error.loc[0]) in self.batch.columns
        )

    def _refused(self, error: FieldError) -> Refused:
        if error.entry is None:
            text = (
                _say(self._setting_title(error), error.msg) if error.loc else error.msg
            )
        else:
            rows = self.batch.rows
            named = (
                self._named(rows[error.entry]) if error.entry in rows else error.entry
            )
            column = str(error.loc[0]) if error.loc else ''
            said = _say(self._cells.titles.get(column, column), error.msg)
            text = f'{named}: {said if error.loc else error.msg}'
        return Refused(
            entry=error.entry,
            loc=list(error.loc),
            message=error.msg,
            text=text,
            placed=self._placed(error),
        )

    # The rows

    @_action
    def scan(self) -> str:
        """
        Make the rows from the files again, in place of a table loaded; what
        was set by hand stays.
        """
        self.runs_version += 1

        def scan() -> None:
            try:
                self.batch.scan()
            except SubmitError as error:
                raise Problem('; '.join(map(str, error.errors))) from None
            except Exception as error:  # a source is code of the application
                raise Problem(f'The scan failed: {error}') from None

        changes = self._scanned(scan)
        return f'Found {self.words.count(len(self.batch.rows))}. {changes}'.strip()

    def _incomplete(self) -> set[str]:
        return {k for k, s in self._states().items() if s.problems}

    def _scanned(self, scan: Callable[[], None]) -> str:
        """
        Call ``scan``, which makes the rows again, and say how many rows
        changed and which it left incomplete.
        """
        before = {r.key: dict(r.values) for r in self.batch.rows}
        incomplete = self._incomplete()
        scan()
        self.loaded = None
        changed = sum(before.get(r.key) != dict(r.values) for r in self.batch.rows)
        new = self._incomplete() - incomplete
        names = [self._named(r) for r in self.batch.rows if r.key in new]
        said = [f'{self.words.count(changed)} changed.' if changed else '']
        if len(names) == 1:
            said.append(f'{names[0]} is now incomplete.')
        elif len(names) in (2, 3):
            said.append(f'{_join(names)} are now incomplete.')
        elif names:
            said.append(f'{self.words.count(len(names))} are now incomplete.')
        return ' '.join(filter(None, said))

    def _read(self, fields: Fields, edit: CellEdit) -> Any:
        if edit.column not in fields.fields:
            raise Problem(f'There is no column {edit.column}.')
        try:
            return forms.read(fields.fields[edit.column], edit.value)
        except (KeyError, ValueError) as error:
            raise Problem(_say(fields.titles[edit.column], _why(error))) from None

    @_action
    def edit(self, edit: CellEdit) -> str:
        """Set one column of rows, as its field reads the value sent."""
        value = self._read(self._cells, edit)
        rows = self.batch.rows
        for key in edit.keys:
            self._row_of(key)
            rows = rows.edit(key, {edit.column: EMPTY if value is UNSET else value})
        self.batch.rows = rows
        self._forget(edit.keys, edit.column)
        if len(edit.keys) < 2:
            return ''
        title = self._cells.titles[edit.column]
        return f'{title} set on {self.words.count(len(edit.keys))}.'

    @_action
    def revert(self, cells: Cells) -> None:
        """Take the cells of ``column`` back to what the scan gave."""
        rows = self.batch.rows
        for key in cells.keys:
            self._row_of(key)
            rows = rows.reset(key, cells.column)
        self.batch.rows = rows
        self._forget(cells.keys, cells.column)

    def _forget(self, keys: Sequence[str], column: str) -> None:
        """Forget the reasons of a refusal at ``column`` of rows ``keys``."""
        self.refused = [
            e
            for e in self.refused
            if not (e.entry in keys and e.loc and str(e.loc[0]) == column)
        ]

    def _forget_settings(self, path: Sequence[str | int] = ()) -> None:
        """Forget the reasons of a refusal at the setting of ``path``, or all."""
        self.refused = [
            e
            for e in self.refused
            if e.entry or (path and tuple(e.loc[:1]) != tuple(path[:1]))
        ]

    @_action
    def remove(self, keys: Sequence[str]) -> str:
        """Remove rows; one added by hand is gone for good."""
        rows = self.batch.rows
        added = {row.key for row in rows.added}
        for key in keys:
            self._row_of(key)
            rows = rows.remove(key)
        self.batch.rows = rows
        notice = f'Removed {self.words.count(len(keys))}.'
        if gone := len(added.intersection(keys)):
            notice += f' {gone} added by hand cannot be brought back.'
        return notice

    @_action
    def restore(self) -> None:
        """Bring back every row of the scan that was removed."""
        rows = self.batch.rows
        for key in rows.removed:
            rows = rows.reset(key)
        self.batch.rows = rows

    @_action
    def add(self, key: str) -> str:
        key = key.strip()
        if not key:
            raise Problem(f'Give the new {self.words.one} a name.')
        if any(self._named(row) == key for row in self.batch.rows):
            raise Problem(f'There is a {self.words.one} named {key} already.')
        try:
            self.batch.rows = self.batch.rows.add(TableRow(key, {}, {}))
        except SubmitError as error:
            raise Problem(f'{key}: {error.errors[0].msg}.') from None
        return f'Added {key}: fill in its cells.'

    # Reducing

    @_action
    def reduce(self, request: Reduce) -> str:
        """Submit the rows asked for, all or none."""
        for key in request.keys:
            self._row_of(key)
        try:
            settings = self.settings.params
            states = self.batch.state(settings)
            keys = {
                'remaining': lambda: remaining(states),
                'all': lambda: ready(states),
                'selected': lambda: request.keys,
            }[request.which]()
            records = self.batch.reduce(settings, keys)
        except SubmitError as error:
            self.refused = list(error.errors)
            raise _Refusal(self.refused) from None
        self.refused = []
        self._alone -= records.keys()
        self._relist()
        n = len(records)
        return f'Reducing {self.words.count(n)}.' if n else ''

    @_action
    def cancel(self) -> str:
        self.batch.cancel()
        return f'Cancelled the {self.words.many} still running.'

    # Settings

    @_action
    def edit_settings(self, edit: Edit) -> None:
        try:
            forms.apply(self.settings, edit)
        except (KeyError, ValueError) as error:
            raise Problem(_why(error)) from None
        self._forget_settings(edit.path)

    @_action
    def use_settings(self, key: str) -> str:
        """Take the settings that the result of row ``key`` was reduced with."""
        name = self._named(self._row_of(key))
        record = self._states()[key].record
        if record is None:
            raise Problem(f'{name} has no result.')
        self.settings.set(record.request.params, self._listing)
        self._forget_settings()
        return f'All {self.words.many} now use the settings {name} was reduced with.'

    # One row

    def _load_tweak(self, key: str) -> None:
        """Load the row's cells and what its result ran with, once per result."""
        cells = self._row_of(key).values
        record = self._states()[key].record
        source = (key, cells, None if record is None else record.id)
        if self._tweak_of == source:
            return
        self._tweak_of = source
        base = (self._settings() or {}) if record is None else record.request.params
        rest = {k: v for k, v in base.items() if k not in self.batch.columns}
        self.tweak.set({**rest, **cells}, self._listing)

    @_locked
    def row(self, key: str) -> RowDetail:
        """What one row was reduced with, and its form for a reduction alone."""
        self._load_tweak(key)
        record = self._states()[key].record
        detail = RowDetail(
            key=key,
            reduced_with=None,
            created=None,
            differs=[],
            tweak=forms.tree(self.tweak),
        )
        if record is None:
            return detail
        given = Fields(
            self.batch.spec.params, self._listing, fields=self.batch.settings_fields
        )
        given.set(record.request.params, self._listing)
        now = self._settings() or {}
        detail.reduced_with = forms.tree(given)
        detail.created = record.created
        detail.differs = [
            n
            for n in self.batch.settings_fields
            if forms.jsonable(record.request.params.get(n))
            != forms.jsonable(now.get(n))
        ]
        return detail

    @_locked
    def edit_tweak(self, key: str, edit: Edit) -> RowDetail:
        self._load_tweak(key)
        try:
            forms.apply(self.tweak, edit)
        except (KeyError, ValueError) as error:
            raise Problem(_why(error)) from None
        return self.row(key)

    @_action
    def submit_tweak(self, key: str) -> str:
        """Reduce row ``key`` alone, with the parameters of its form."""
        self._load_tweak(key)
        try:
            self.batch.tweak(key, self.tweak.params)
        except SubmitError as error:
            raise Problem('; '.join(map(str, error.errors))) from None
        self._alone.add(key)
        self._relist()
        return f'Reducing {self._named(self.batch.rows[key])} alone.'

    # The result

    @_locked
    def plot(self, keys: Sequence[str]) -> Plot:
        """
        The figure of the results among the rows ``keys``, or of the first
        results if none is given, and a note of what it shows.
        """
        rows = {r.key: r for r in self.state().rows}
        done = {k: r for k, r in rows.items() if r.result is not None}
        many = self.words.many
        if keys:
            chosen = [r for k, r in done.items() if k in keys]
            note = '' if chosen else f'None of the {many} selected has a result.'
        else:
            chosen = list(done.values())[:_DRAWN]
            note = (
                f'No results yet. Reduce {many} to see them here.' if not done else ''
            )
            if len(done) > _DRAWN:
                note = f'The first {_DRAWN} of {len(done)}. Select {many} to choose.'
        if not chosen or self._draw is None:
            return Plot(figure=None, note=note)
        ids = [r.record for r in chosen if r.record is not None]
        records = {r.id: r for r in self.batch.client.records(ids=ids)}
        order = {k: i for i, k in enumerate(rows)}
        colors = plots.colors([order[r.key] for r in chosen])
        shown = [
            Shown(r.key, r.name, records[r.record], r.result == 'outdated', c)
            for r, c in zip(chosen, colors, strict=True)
            if r.record is not None
        ]
        try:
            return Plot(
                figure=self._draw(shown),
                note=note,
                colors={s.key: s.color for s in shown},
            )
        except Exception as error:  # a drawing is code of the application
            return Plot(figure=None, note=f'Cannot draw: {error}')

    # Runs

    def _marked(self) -> tuple[Fields, CorrectedFiles]:
        if self._marks is None or self._files is None:
            raise Problem('This application corrects no runs.')
        return self._marks, self._files

    @_locked
    def runs(self) -> Runs:
        """The runs, with what a person corrected of each."""
        marks, files = self._marked()
        rows = []
        for ref in files.list(Selector()):
            metadata = files.metadata(ref)
            original = files.source.metadata(ref)
            corrected = files.corrections(ref)
            cells = {
                name: Cell(
                    value=forms.cell(field, metadata.get(name, UNSET)),
                    text=self._text(metadata.get(name, UNSET)),
                    edited=name in corrected,
                    scanned=self._text(original.get(name, UNSET)),
                )
                for name, field in marks.fields.items()
            }
            label = self._listing.file_label(ref)
            rows.append(RunRow(key=ref.file, file=label, cells=cells))
        return Runs(
            version=self.runs_version,
            columns=[
                Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                for n, f in marks.fields.items()
            ],
            rows=rows,
        )

    def _rescan(self) -> str:
        """
        Show the runs as corrected now, and the rows made from them; say what
        changed.
        """
        self.runs_version += 1
        self._relist()

        def scan() -> None:
            try:
                self.batch.scan()
            except Exception as error:  # a source is code of the application
                raise Problem(f'Corrected, but the scan failed: {error}') from None

        return self._scanned(scan)

    @_action
    def correct(self, edit: CellEdit) -> str:
        """Correct a field of runs; the rows follow."""
        marks, files = self._marked()
        value = self._read(marks, edit)
        for key in edit.keys:
            fields = {edit.column: None if value is UNSET else value}
            files.correct(FileRef(file=key), **fields)
        changes = self._rescan()
        n = len(edit.keys)
        title = marks.titles[edit.column]
        return f'{title} corrected on {n} run{"s" * (n > 1)}. {changes}'.strip()

    @_action
    def revert_run(self, cells: Cells) -> str:
        """Take a field of runs back to what the files say."""
        _, files = self._marked()
        for key in cells.keys:
            files.reset(FileRef(file=key), cells.column)
        return self._rescan()

    # Files

    def _table(self, keys: Sequence[str] | None = None) -> Any:
        rows = [r for r in self.batch.rows if keys is None or r.key in keys]
        table = frames.table_of(rows, self.batch.columns)
        return frames.with_results(self.batch.client, self._states(), table)

    @_locked
    def table_file(self, suffix: str) -> bytes:
        """The rows as a table file, ``.csv`` or ``.xlsx``."""
        with _file(f'{self.words.many}{suffix}') as path:
            frames.write_table(self._table(), path)
            return path.read_bytes()

    @_action
    def load_table(self, name: str, content: bytes) -> str:
        """
        Take the rows of a table file in place of a scan, and forget what was
        set by hand on them, so that they show what the file says.
        """
        batch = self.batch
        with _file(name, content) as path:
            try:
                table = frames.read_table(path)
                rows = frames.rows_of(table, batch.spec.params, batch.columns)
            except SubmitError as error:
                raise Problem('; '.join(map(str, error.errors))) from None
            except (OSError, ValueError, KeyError) as error:
                raise Problem(f'Cannot load {name}: {_why(error)}') from None
        loaded = batch.rows.rescan(rows)
        for row in rows:
            loaded = loaded.reset(row.key, *batch.columns)
        batch.rows = loaded
        self.loaded = name
        self._relist()
        return f'Loaded {self.words.count(len(rows))} from {name}.'

    @_locked
    def settings_file(self) -> bytes:
        """The settings as a YAML template."""
        try:
            template = self.batch.template(self.settings.params)
        except SubmitError as error:
            raise Problem(f'Fill in the settings first: {error}') from None
        with _file('settings.yaml') as path:
            write_template(template, path)
            return path.read_bytes()

    @_action
    def load_settings(self, name: str, content: bytes) -> str:
        """Show the settings of a YAML template."""
        batch = self.batch
        cannot = f'Cannot load {name}:'
        with _file('settings.yaml', content) as path:
            try:
                template = load_template(path)
            except (OSError, ValueError):
                raise Problem(
                    f'{cannot} it is not a settings file saved from this page.'
                ) from None
        ours = SpecId.of(batch.spec).name
        if template.spec.name != ours:
            raise Problem(
                f'{cannot} it holds the settings of {template.spec.name}, not {ours}.'
            )
        if template.spec != SpecId.of(batch.spec) or set(template.blanks) != set(
            batch.columns
        ):
            raise Problem(f'{cannot} it was saved by another version of this page.')
        try:
            settings = with_named_files(batch.spec.params, template.params)
            self.settings.set(with_latest(batch.client, settings), self._listing)
        except (ValueError, KeyError) as error:
            raise Problem(f'{cannot} {forms.message(_why(error))}.') from None
        self._forget_settings()
        return f'Loaded the settings of {name}.'

    @_locked
    def results_file(self, keys: Sequence[str]) -> bytes:
        """A zip of the outputs of the done rows among ``keys``, or of all."""
        states = self._states()
        done = [
            k
            for k, s in states.items()
            if s.status is Status.COMPLETED and (not keys or k in keys)
        ]
        if not done:
            raise Problem(f'None of these {self.words.many} has a result.')
        buffer = io.BytesIO()
        with _file('results') as folder:
            exported = export_table(self.batch.client, self._table(done), folder)
            frames.write_table(exported, folder / 'results.csv')
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(folder.parent))
        return buffer.getvalue()


class _Refusal(Problem):
    """A reduction refused; its reasons are in the state."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        n = len(errors)
        super().__init__(
            'Nothing was reduced: '
            + ('one value is refused.' if n == 1 else f'{n} values are refused.')
        )


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

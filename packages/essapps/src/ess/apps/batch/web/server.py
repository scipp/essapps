# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the batch page does, without HTTP: :class:`Server`. :mod:`.api` serves it.

The server holds everything a person changes: the rows of the
:class:`~ess.apps.batch.Batch`, the settings, the form of a tweak, and the
corrections of runs. The page holds only what it shows: the selection, the
filter, the tab. So each action is a request, and the page shows the state that
``GET /api/state`` gives next. The page polls it about once a second with the
``version`` it has, and gets ``{'version': n}`` alone if nothing changed. The
version counts the changes made here and those of the client's records, which
the server reads from ``client.changes`` at each poll.

A reduction refused raises ``SubmitError``, which the API answers with a 422
of its errors, ``{entry, loc, message}``. The server keeps them until the next
change, so that the state marks each at its cell or its setting. Any other
request that cannot be done raises :class:`Problem`, with what to tell the
person. One server serves one batch to one person, and takes one request at a
time.

Statuses are named in a scientist's words: a row that is ``not reduced`` is
``ready``, a ``pending`` one ``running``, a ``completed`` one ``done``, and a
``stale`` one ``outdated``.
"""

from __future__ import annotations

import functools
import io
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_json

from ess.spec import FileRef, OutputRef
from ess.spec.refs import as_ref

from ...errors import FieldError, SubmitError
from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ...records import Record, SpecId, Status
from .. import EMPTY, Batch, CorrectedFiles, Row, RowState, frames, ready, remaining
from ..export import export_table
from ..text import load_template, with_latest, with_named_files, write_template
from . import forms

type Draw = Callable[[Mapping[str, Record]], dict[str, Any]]
"""Draws the completed results of rows, by key, as a Plotly figure."""

_STATUS = {
    'not reduced': 'ready',
    'pending': 'running',
    'completed': 'done',
    'stale': 'outdated',
}
_ORDER = ('ready', 'running', 'done', 'outdated', 'failed', 'cancelled', 'incomplete')
_PROBLEM = {'empty': 'missing'}
_DRAWN = 12
"""How many done rows the result shows when none is selected."""


def _status(state: RowState) -> str:
    return _STATUS.get(state.text, state.text)


class Problem(Exception):
    """A request that cannot be done, with what to tell the person."""


class CellEdit(BaseModel):
    """Set ``column`` of the rows ``keys`` to ``value``, a label for a picker."""

    keys: list[str]
    column: str
    value: Any = None


class Cells(BaseModel):
    """The cells of ``column`` of the rows ``keys``."""

    keys: list[str]
    column: str


class Keys(BaseModel):
    keys: list[str] = []


class ReduceRequest(BaseModel):
    """Reduce the rows remaining, all ready ones, or those of ``keys``."""

    which: Literal['remaining', 'all', 'selected'] = 'remaining'
    keys: list[str] = []


class NewRow(BaseModel):
    key: str


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


class Server:
    """
    What the API does, without HTTP. See the module for the rules.

    Parameters
    ----------
    batch:
        The batch, scanned or not.
    info:
        The entries of :attr:`Row.info` to show as columns.
    draw:
        Draws the results of rows (see :data:`Draw`).
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
        info: Sequence[str] = (),
        draw: Draw | None = None,
        files: CorrectedFiles | None = None,
        marks: type[BaseModel] | None = None,
        folder: str = '',
    ) -> None:
        self.lock = threading.RLock()
        self.batch = batch
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
        self._corrected: dict[FileRef, dict[str, Any]] = {}
        self._refused: list[FieldError] = []
        self._started: tuple[str, ...] = ()
        self.version = 1

    # Reading

    def _changed(self) -> None:
        self.version += 1

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

    @_locked
    def poll(self, after: int) -> dict[str, Any]:
        """The state, or the version alone if it is ``after``."""
        if self.batch.client.changes(after=self._listing.cursor).records:
            self._relist()
            self._changed()
        if after == self.version:
            return {'version': self.version}
        return self.state()

    @_locked
    def state(self) -> dict[str, Any]:
        """Everything the page shows, but for a row's details and the plot."""
        states = self._states()
        failures = self._failures(states.values())
        scan = {row.key: row for row in self.batch.rows.scan}
        rows = [
            self._row(row, states[row.key], failures, scan.get(row.key))
            for row in self.batch.rows
        ]
        counts = dict.fromkeys(_ORDER, 0)
        for state in states.values():
            counts[_status(state)] += 1
        removed = scan.keys() & self.batch.rows.removed
        fields = self.batch.spec.params.model_fields
        return {
            'version': self.version,
            'title': self.batch.spec.title,
            'folder': self._folder,
            'info': [
                {'name': n, 'title': n.replace('_', ' ').capitalize()}
                for n in self._info
            ],
            'columns': [
                {
                    'name': c,
                    'title': self._cells.titles[c],
                    'editor': forms.editor(self._cells.fields[c]),
                    'default': ''
                    if fields[c].is_required()
                    else self._text(fields[c].get_default(call_default_factory=True)),
                }
                for c in self.batch.columns
            ],
            'rows': rows,
            'counts': {'all': len(states), **counts},
            'removed': len(removed),
            'action': self._action(states),
            'settings': forms.tree(self.settings, self._at_settings()),
            'unplaced': [str(e) for e in self._refused if not self._placed(e)],
            'runs': self._runs(),
        }

    def _failures(self, states: Iterable[RowState]) -> dict[str, str]:
        ended = [
            s.record
            for s in states
            if s.record is not None and s.status in (Status.FAILED, Status.CANCELLED)
        ]
        reasons = self.batch.client.failure(ended)
        return {
            r.id: why or 'Cancelled.' for r, why in zip(ended, reasons, strict=True)
        }

    def _text(self, value: Any) -> str:
        """What a cell shows: a file or an output by its label."""
        if value is UNSET:
            return ''
        ref = as_ref(value)
        if isinstance(ref, OutputRef):
            return self._listing.output_label(ref)
        if isinstance(ref, FileRef):
            return self._listing.file_label(ref)
        if isinstance(value, list):
            return ', '.join(self._text(v) for v in value)
        if isinstance(value, dict):
            return to_json(value).decode()
        return 'none' if value is None else str(value)

    def _row(
        self,
        row: Row,
        state: RowState,
        failures: Mapping[str, str],
        scanned: Row | None,
    ) -> dict[str, Any]:
        edits = self.batch.rows.edits.get(row.key, {})
        problems = {
            e.loc[0]: _PROBLEM.get(e.msg, e.msg)
            for e in (*state.problems, *self._refused)
            if e.entry == row.key and e.loc
        }
        reason = ''
        if state.record is not None and state.record.id in failures:
            reason = failures[state.record.id]
        elif problems:
            titles = self._cells.titles
            reason = '; '.join(f'{titles.get(c, c)}: {m}' for c, m in problems.items())
        elif state.stale:
            reason = 'Its cells or the settings changed since it was reduced.'
        cells: dict[str, Any] = {}
        for c in self.batch.columns:
            value = row.values.get(c, UNSET)
            cells[c] = forms.cell(self._cells.fields[c], value)
            cells[f'{c}:text'] = self._text(value)
            if c in edits and scanned is not None:
                cells[f'{c}:scanned'] = self._text(scanned.values.get(c, UNSET))
        return {
            'key': row.key,
            **{n: row.info.get(n, '') for n in self._info},
            **cells,
            'edited': [c for c in edits if c in self.batch.columns],
            'problems': problems,
            'status': _status(state),
            'reason': reason,
            'record': None if state.record is None else state.record.id,
            'result': state.status is Status.COMPLETED,
        }

    def _action(self, states: Mapping[str, RowState]) -> dict[str, Any]:
        """What the primary button does now, and how far the rows running are."""
        left = remaining(states)
        outdated = [k for k in left if states[k].stale]
        running = [k for k, s in states.items() if s.status is Status.PENDING]
        if not left:
            label = 'Nothing left to reduce'
        elif len(outdated) == len(left):
            label = f'Reduce {len(left)} outdated'
        else:
            label = f'Reduce {len(left)} remaining'
        started = [k for k in self._started if k in states]
        finished = [k for k in started if states[k].status is not Status.PENDING]
        return {
            'label': label,
            'remaining': len(left),
            'ready': len(ready(states)),
            'running': len(running),
            'progress': [len(finished), len(started)] if running else None,
        }

    def _placed(self, error: FieldError) -> bool:
        if not error.loc:
            return False
        if error.entry is None:
            return error.loc[0] in self.settings.fields
        return error.entry in self.batch.rows and error.loc[0] in self.batch.columns

    def _at_settings(self) -> list[FieldError]:
        return [e for e in self._refused if e.entry is None]

    def refusal(self, error: SubmitError) -> dict[str, Any]:
        """
        What the page is told of a reduction refused: each error, and whether
        the state marks it at a cell or a setting.
        """
        return {
            'message': 'Nothing was reduced.',
            'errors': [
                {
                    'entry': e.entry,
                    'loc': list(e.loc),
                    'message': e.msg,
                    'placed': self._placed(e),
                    'text': str(e),
                }
                for e in error.errors
            ],
        }

    # The rows

    def _edited(self, notice: str = '') -> dict[str, Any]:
        self._refused = []
        self._changed()
        return {'notice': notice}

    @_locked
    def scan(self) -> dict[str, Any]:
        """Make the rows from the files again; what was set by hand stays."""
        try:
            self.batch.scan()
        except SubmitError as error:
            raise Problem('; '.join(map(str, error.errors))) from None
        except Exception as error:  # a source is code of the application
            raise Problem(f'The scan failed: {error}') from None
        return self._edited(f'Found {len(self.batch.rows)} samples.')

    @_locked
    def edit(self, edit: CellEdit) -> dict[str, Any]:
        """Set one column of rows, as its field reads the value sent."""
        field = self._cells.fields[edit.column]
        try:
            value = forms.read(field, edit.value)
        except (KeyError, ValueError) as error:
            raise Problem(f'{self._cells.titles[edit.column]}: {error}') from None
        rows = self.batch.rows
        for key in edit.keys:
            rows = rows.edit(key, {edit.column: EMPTY if value is UNSET else value})
        self.batch.rows = rows
        title = self._cells.titles[edit.column]
        many = f' on {len(edit.keys)} samples' if len(edit.keys) > 1 else ''
        return self._edited(f'{title} set{many}.' if many else '')

    @_locked
    def revert(self, cells: Cells) -> dict[str, Any]:
        """Take the cells of ``column`` back to what the scan gave."""
        rows = self.batch.rows
        for key in cells.keys:
            rest = {
                c: v for c, v in rows.edits.get(key, {}).items() if c != cells.column
            }
            rows = rows.reset(key)
            if rest:
                rows = rows.edit(key, rest)
        self.batch.rows = rows
        return self._edited()

    @_locked
    def remove(self, keys: Keys) -> dict[str, Any]:
        rows = self.batch.rows
        for key in keys.keys:
            rows = rows.remove(key)
        self.batch.rows = rows
        n = len(keys.keys)
        return self._edited(f'Removed {n} sample{"s" if n > 1 else ""}.')

    @_locked
    def restore(self) -> dict[str, Any]:
        """Bring back every row that was removed."""
        rows = self.batch.rows
        for key in rows.removed:
            rows = rows.reset(key)
        self.batch.rows = rows
        return self._edited()

    @_locked
    def add(self, new: NewRow) -> dict[str, Any]:
        key = new.key.strip()
        if not key:
            raise Problem('Give the new sample a name.')
        try:
            self.batch.rows = self.batch.rows.add(Row(key, {}, {}))
        except SubmitError as error:
            raise Problem(f'{key}: {error.errors[0].msg}') from None
        return self._edited(f'Added {key}: fill in its cells.')

    # Reducing

    @_locked
    def reduce(self, request: ReduceRequest) -> dict[str, Any]:
        """Submit the rows asked for, all or none."""
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
            self._refused = list(error.errors)
            self._changed()
            raise
        self._started = tuple(records)
        self._relist()
        n = len(records)
        return self._edited(f'Reducing {n} sample{"s" if n != 1 else ""}.' if n else '')

    @_locked
    def cancel(self) -> dict[str, Any]:
        self.batch.cancel()
        return self._edited('Cancelled the samples still running.')

    # Settings

    @_locked
    def edit_settings(self, edit: forms.Edit) -> dict[str, Any]:
        try:
            forms.apply(self.settings, edit)
        except (KeyError, ValueError) as error:
            raise Problem(str(error)) from None
        return self._edited()

    @_locked
    def use_settings(self, key: str) -> dict[str, Any]:
        """Take the settings that the result of row ``key`` was reduced with."""
        record = self._states()[key].record
        if record is None:
            raise Problem(f'{key} has no result.')
        self.settings.set(record.request.params, self._listing)
        return self._edited('The settings are those of this result now.')

    # One row

    def _load_tweak(self, key: str, state: RowState) -> None:
        """Load the row's cells and what its result ran with, once per result."""
        cells = self.batch.rows[key].values
        record = state.record
        source = (key, cells, None if record is None else record.id)
        if self._tweak_of == source:
            return
        self._tweak_of = source
        base = (self._settings() or {}) if record is None else record.request.params
        rest = {k: v for k, v in base.items() if k not in self.batch.columns}
        self.tweak.set({**rest, **cells}, self._listing)

    @_locked
    def row(self, key: str) -> dict[str, Any]:
        """What one row was reduced with, why it failed, and its tweak form."""
        state = self._states()[key]
        self._load_tweak(key, state)
        record = state.record
        detail: dict[str, Any] = {'key': key, 'reduced_with': None, 'differs': []}
        if record is not None:
            given = Fields(
                self.batch.spec.params,
                self._listing,
                fields=self.batch.settings_fields,
            )
            given.set(record.request.params, self._listing)
            detail['reduced_with'] = forms.tree(given)
            now = self._settings() or {}
            detail['differs'] = [
                self.settings.titles[n]
                for n in self.batch.settings_fields
                if forms.jsonable(record.request.params.get(n))
                != forms.jsonable(now.get(n))
            ]
            detail['created'] = record.created.astimezone().strftime('%H:%M:%S')
        detail['tweak'] = forms.tree(self.tweak)
        return detail

    @_locked
    def edit_tweak(self, key: str, edit: forms.Edit) -> dict[str, Any]:
        self._load_tweak(key, self._states()[key])
        try:
            forms.apply(self.tweak, edit)
        except (KeyError, ValueError) as error:
            raise Problem(str(error)) from None
        return self.row(key)

    @_locked
    def submit_tweak(self, key: str) -> dict[str, Any]:
        """Reduce row ``key`` alone, with the parameters of the tweak form."""
        self._load_tweak(key, self._states()[key])
        try:
            self.batch.tweak(key, self.tweak.params)
        except SubmitError as error:
            raise Problem('; '.join(map(str, error.errors))) from None
        self._started = (key,)
        self._relist()
        return self._edited(f'Reducing {key} alone.')

    # The result

    @_locked
    def plot(self, keys: Sequence[str]) -> dict[str, Any]:
        """
        The figure of the done rows among ``keys``, or of the first done rows
        if none is given, and a note of what it shows.
        """
        states = self._states()
        done = {
            k: s.record
            for k, s in states.items()
            if s.record is not None and s.status is Status.COMPLETED
        }
        if keys:
            chosen = {k: done[k] for k in keys if k in done}
            note = '' if chosen else 'None of the samples selected has a result.'
        else:
            chosen = dict(list(done.items())[:_DRAWN])
            note = (
                f'The first {_DRAWN} of {len(done)} results. Select samples to choose.'
                if len(done) > _DRAWN
                else ''
            )
            if not done:
                note = 'No results yet. Reduce samples to see them here.'
        if not chosen or self._draw is None:
            return {'figure': None, 'note': note}
        try:
            return {'figure': self._draw(chosen), 'note': note}
        except Exception as error:  # a drawing is code of the application
            return {'figure': None, 'note': f'Cannot draw: {error}'}

    # Runs

    def _runs(self) -> dict[str, Any] | None:
        if self._marks is None or self._files is None:
            return None
        files = self.batch.client.files
        refs = files.list(Selector())
        rows = []
        for ref, metadata in zip(refs, files.metadata(refs), strict=True):
            row: dict[str, Any] = {
                'key': ref.file,
                'file': self._listing.file_label(ref),
                'edited': list(self._corrected.get(ref, {})),
            }
            for name, field in self._marks.fields.items():
                value = metadata.get(name, UNSET)
                row[name] = forms.cell(field, value)
                row[f'{name}:text'] = self._text(value)
            rows.append(row)
        return {
            'columns': [
                {'name': n, 'title': self._marks.titles[n], 'editor': forms.editor(f)}
                for n, f in self._marks.fields.items()
            ],
            'rows': rows,
        }

    def _corrections_changed(self, notice: str) -> dict[str, Any]:
        self._relist()
        self.batch.scan()
        return self._edited(notice)

    @_locked
    def correct(self, edit: CellEdit) -> dict[str, Any]:
        """Correct a field of runs; the rows follow."""
        if self._marks is None or self._files is None:
            raise Problem('This application corrects no runs.')
        field = self._marks.fields[edit.column]
        try:
            value = forms.read(field, edit.value)
        except (KeyError, ValueError) as error:
            raise Problem(str(error)) from None
        value = None if value is UNSET else value
        for key in edit.keys:
            ref = FileRef(file=key)
            self._files.correct(ref, **{edit.column: value})
            self._corrected.setdefault(ref, {})[edit.column] = value
        title = self._marks.titles[edit.column]
        n = len(edit.keys)
        return self._corrections_changed(
            f'{title} corrected on {n} run{"s" * (n > 1)}.'
        )

    @_locked
    def revert_run(self, cells: Cells) -> dict[str, Any]:
        """Take a field of runs back to what the files say."""
        if self._files is None:
            raise Problem('This application corrects no runs.')
        for key in cells.keys:
            ref = FileRef(file=key)
            rest = {
                k: v
                for k, v in self._corrected.pop(ref, {}).items()
                if k != cells.column
            }
            self._files.reset(ref)
            if rest:
                self._files.correct(ref, **rest)
                self._corrected[ref] = rest
        return self._corrections_changed('')

    # Files

    def _table(self, keys: Iterable[str] | None = None) -> Any:
        rows = [r for r in self.batch.rows if keys is None or r.key in keys]
        table = frames.table_of(rows, self.batch.columns)
        return frames.with_results(self.batch.client, self._states(), table)

    @_locked
    def table_file(self, suffix: str) -> bytes:
        """The rows as a table file, ``.csv`` or ``.xlsx``."""
        with _file(f'samples{suffix}') as path:
            frames.write_table(self._table(), path)
            return path.read_bytes()

    @_locked
    def load_table(self, name: str, content: bytes) -> dict[str, Any]:
        """Take the rows of a table file in place of a scan, under the edits."""
        batch = self.batch
        with _file(name, content) as path:
            try:
                table = frames.read_table(path)
                rows = frames.rows_of(table, batch.spec.params, batch.columns)
                batch.rows = batch.rows.rescan(rows)
            except SubmitError as error:
                raise Problem('; '.join(map(str, error.errors))) from None
            except (OSError, ValueError) as error:
                raise Problem(f'Cannot load {name}: {error}') from None
        self._relist()
        return self._edited(f'Loaded {len(rows)} samples from {name}.')

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

    @_locked
    def load_settings(self, name: str, content: bytes) -> dict[str, Any]:
        """Show the settings of a YAML template."""
        batch = self.batch
        with _file('settings.yaml', content) as path:
            try:
                template = load_template(path)
                if template.spec != SpecId.of(batch.spec):
                    raise ValueError(f'it is a template of {template.spec}')
                if set(template.blanks) != set(batch.columns):
                    raise ValueError(f'its blanks are not {", ".join(batch.columns)}')
                settings = with_named_files(batch.spec.params, template.params)
                self.settings.set(with_latest(batch.client, settings), self._listing)
            except (OSError, ValueError) as error:
                raise Problem(f'Cannot load {name}: {error}') from None
        return self._edited(f'Loaded the settings of {name}.')

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
            raise Problem('None of these samples has a result.')
        buffer = io.BytesIO()
        with _file('results') as folder:
            exported = export_table(self.batch.client, self._table(done), folder)
            frames.write_table(exported, folder / 'results.csv')
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(folder.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(folder.parent))
        return buffer.getvalue()

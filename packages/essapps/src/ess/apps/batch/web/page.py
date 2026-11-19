# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the page of one batch does, without HTTP: :class:`Page`. :mod:`.api`
serves it. This module imports neither ``fastapi`` nor ``starlette``, so
another view of a batch can call the same actions and render the same
:class:`~.models.State`.

A :class:`Page` holds what a person changes on it: the form of the
settings and the refusals. It asks :class:`~.pages.Pages` for the rest: the
:class:`~ess.apps.batch.Batch` of its label with its rows and settings, kept
by the manager, the corrections of runs, and Follow. The batch and the
corrections keep their part in their definitions, if given them
(:mod:`..definition`); a change that the definitions cannot keep is not made,
and answers a :class:`Problem`.

The form of the settings is a draft of the batch's settings. An edit changes
the form only; :meth:`Page.apply_settings` writes it to the batch, and is
refused while something is missing; :meth:`Page.discard_settings` shows the
batch's settings again. "Load settings" and "Put these settings in the form"
fill the form, all or none. While the form holds a change, a reduction by a person
and a download of the settings are refused, so that nothing is reduced or
saved with settings other than those shown. Settings that the form cannot
show, such as those kept for another version of the spec, are named in the
state's ``settings_problem`` and kept until Apply. A ``Page`` writes the
batch's settings only at Apply: the form does not show an assignment by code
while the server runs.

The page in the browser holds only what it shows: the selection, the
filter, the tab. Each action returns the :class:`~.models.State` after it, and
the browser polls the state about once a second with the ``version`` it has,
to see results arrive. The version counts the changes made here and those of
the client's records, which a ``Page`` reads from ``client.changes`` at each
poll. ``start`` tells a page that the server started again, so that the same
version may be another state.

A reduction refused raises :class:`Problem`. It says the reasons that no cell
or setting shows. The others stay in the state until the value of each is
changed, a setting's by Apply, or a reduction succeeds, so that the page marks
each at its cell, or at its setting while the form shows the batch's settings.
Any other request that cannot be done raises :class:`Problem` too, with what
to tell the person. A ``Page`` serves one batch to one person, and takes one
request at a time, under the lock of all pages.

Statuses are named in a scientist's words: a row that is ``not reduced`` is
``ready``, a ``pending`` one ``running``, and a ``completed`` one ``done``;
``incomplete`` and ``outdated`` keep their names. A change of settings applies
to rows not reduced yet: a done row whose result differs from its request only
at settings (:func:`~ess.apps.batch.earlier_settings`) stays ``done``, marked
as reduced with earlier settings, and "Reduce" leaves it
(:func:`~ess.apps.batch.left_to_reduce`) until a person reduces it with the
settings now. A failed or cancelled row stays so when the settings change,
since it needs reducing either way. The rows read as with the batch's
settings, whatever the form holds. While the workflow refuses the
batch's settings, no result can be said to be current, so a done row is
outdated.

The tab "Batch" shows the header of the batch, and changes its title,
description and scope with the manager (:meth:`Page.change`), which decides
what can be changed. :meth:`Page.start_next` has the manager end the batch at
a row and make the next one from there. Removing a batch is :meth:`Pages.remove
<.pages.Pages.remove>`; a request to a page of a batch removed answers
:class:`Missing`.

A batch with a source offers to follow the files. The switch "Follow" makes
the batch the one that follows (:meth:`~ess.apps.batch.BatchManager.follow`),
so no other batch follows. While it
follows, :meth:`Pages.tick <.pages.Pages.tick>` steps it every ``interval``
seconds, which scans as "Rescan runs" does and reduces the rows that
:func:`~ess.apps.batch.arrived` gives, at the columns of the source, under the
lock of all pages, with the batch's settings, whatever the form holds; then
:meth:`Page.stepped` shows what the step changed. The page of the batch that
follows says what the last step raised, and why rows were not reduced; the
next step runs all the same. Follow reduces a row for what the files changed,
never for what a person changed
(:attr:`~ess.apps.batch.RowState.changed_by_hand`): a row whose cells a person
set waits for "Reduce". So an action, but turning Follow on, submits only what
a person asks to reduce. Apply forgets why Follow could not reduce rows, since
its next step tries them with the settings applied. Which batch follows is
kept in the definitions, so a restart resumes.
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
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ess.spec import FileRef

from ...errors import FieldError, SubmitError
from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ...records import Record, Status
from .. import frames
from ..definition import NotKept
from ..export import export_table
from ..reduction import Batch, Words
from ..state import (
    EARLIER,
    RowState,
    earlier_settings,
    left_to_reduce,
    ready,
    status_texts,
)
from ..table import EMPTY, Scope
from ..table import Row as TableRow
from ..text import checked_settings, load_template, write_template
from . import forms, plots
from .models import (
    Action,
    Cell,
    CellEdit,
    Cells,
    Column,
    Edit,
    Following,
    HeaderEdit,
    HeaderShown,
    Made,
    Next,
    Plot,
    Reduce,
    Refused,
    Row,
    RowDetail,
    State,
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

_STATUS = {
    'not reduced': 'ready',
    'pending': 'running',
    'completed': 'done',
    f'completed, {EARLIER}': 'done',
}
_ORDER = ('ready', 'running', 'done', 'outdated', 'failed', 'cancelled', 'incomplete')
_ENDED = (Status.FAILED, Status.CANCELLED)
_BY_HAND = ('ready', 'outdated')
"""The statuses at which a row changed by hand says that Follow leaves it."""
_LEFT = 'Set by hand, so Follow leaves it to you: reduce it.'
"""What a row changed by hand says while following."""
_DRAWN = 12
"""How many results the plot shows when no row is selected."""


def page_statuses(
    states: Mapping[str, RowState],
    columns: Collection[str],
    waiting: Collection[str] = (),
) -> dict[str, str]:
    """
    The status of each row in the words of the page (see the module), by key:
    that of :func:`~ess.apps.batch.status_texts`, but ``incomplete`` for a row
    of ``waiting``, which Follow could not submit, and ``done`` for a row of
    earlier settings. The page and the counts of the batches both use it.
    """
    return {
        key: 'incomplete' if key in waiting else _STATUS.get(text, text)
        for key, text in status_texts(states, columns).items()
    }


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
        The label of the batch. The source of the batch says what a row is
        called, and which entries of :attr:`Row.info` name and describe it.
    """

    def __init__(self, pages: Pages, label: str) -> None:
        self.pages = pages
        self.label = label
        batch = self.batch
        source = batch.source
        self.words = Words() if source is None else source.words
        self._name = None if source is None else source.row_name
        self._info = () if source is None else source.row_info
        self._listing = Listing(batch.client)
        model, listing = batch.spec.params, self._listing
        self.settings = self._form()
        self._show_settings()
        self._cells = Fields(model, listing, fields=batch.columns)
        self.refused: list[FieldError] = []
        self.version = 1
        pages.runs_listed(self._runs_seen())
        # The corrections of runs that the batch was scanned after; the batch
        # may have been made before any of them.
        self._corrected = 0
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
        The lock of all pages, taken for a request; raises :class:`Missing`
        if the batch was removed since the page was handed out, such as by a
        request that ran between this one's check of the label and its
        action.
        """
        with self.lock:
            if self.label not in self.pages.manager.headers():
                raise Missing(f'There is no batch {self.label}.')
            yield

    def _follows(self) -> bool:
        """Whether this batch is the one that follows."""
        return self.pages.manager.following == self.label

    # Reading

    def _live(self) -> tuple[object, ...]:
        """
        What the page shows that changes without an action: the rows, the
        records, the runs, the list of batches, which batch follows, and how
        the last step of Follow went.
        """
        pages = self.pages
        return (
            self.batch.rows,
            self._listing.cursor,
            pages.runs_version,
            pages.batches_version(),
            pages.manager.following,
            pages.refused,
            pages.failure,
        )

    def _moved(self) -> bool:
        """Whether what :meth:`_live` gives changed since the version moved."""
        return self._live() != self._seen

    def _new_version(self) -> None:
        self.version += 1
        self._seen = self._live()

    def _relist(self) -> None:
        self._listing = Listing(self.batch.client, previous=self._listing)
        self._refresh()

    def _refresh(self) -> None:
        for fields in (self.settings, self._cells):
            fields.refresh(self._listing)

    def _runs_seen(self) -> dict[FileRef, dict[str, Any]]:
        """The runs that the Runs view lists, with their metadata."""
        return self._listing.listed(Selector()) if self.pages.corrects_runs else {}

    def _form(self) -> Fields:
        """A form of the settings, at the fields' defaults."""
        batch = self.batch
        return Fields(batch.spec.params, self._listing, fields=batch.settings_fields)

    def _show_settings(self) -> None:
        """Show the batch's settings in the form, which then holds no change."""
        # Settings kept for another version of the spec may not fit the form;
        # the batch keeps them, and the page names them, until Apply.
        self._unfit = self.settings.set_fitting(self.batch.settings, self._listing)
        # What the form holds while it shows the batch's settings.
        self._shown = self.settings.held()

    def _unapplied(self) -> str | None:
        """
        What a person does first while the form holds a change, since a
        reduction would use settings other than those shown; else ``None``.
        """
        held = self.settings.held()
        if held == self._shown:
            return None
        return f'{"Fill in" if held[1] else "Apply"} or discard the settings'

    def _states(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The state of the rows of ``keys``, or of all, with the batch's settings;
        and, if those are refused, the reasons, and the rows with their results
        only (:meth:`~ess.apps.batch.Batch.results`).
        """
        try:
            return self.batch.state(keys), ()
        except SubmitError as error:
            return self.batch.results(keys), error.errors

    @_locked
    def poll(self, after: int, start: str = '') -> State | None:
        """
        The state, or ``None`` if it is still that of version ``after``.

        After a correction of runs (:meth:`Pages.correct
        <.pages.Pages.correct>`), the batch is scanned first. If that scan
        fails, the rows stay as they are, the state says why in its notice,
        and no later poll scans again for that correction; "Rescan runs" tries
        again.
        """
        notice = ''
        if self._corrected != self.pages.corrected:
            try:
                self._scan('Runs were corrected, but the scan failed')
            except Problem as error:
                notice = str(error)
        changes = self.batch.client.changes(after=self._listing.cursor)
        # A scan makes a new table each time, equal if nothing changed; one
        # that changed may name files that the listing lacks.
        if changes.records or self._moved():
            self._relist()
            self._new_version()
        if not notice and after == self.version and start == self.start:
            return None
        return self.state(notice=notice)

    def _row_of(self, key: str) -> TableRow:
        if key not in self.batch.rows:
            raise Problem(f'There is no {self.words.one} {key}.')
        return self.batch.rows[key]

    def _known(self, keys: Iterable[str]) -> None:
        """Raise ``Problem`` unless the table has a row for each of ``keys``."""
        for key in keys:
            self._row_of(key)

    def _named(self, row: TableRow) -> str:
        return (row.info.get(self._name) if self._name else None) or row.key

    def _setting_title(self, error: FieldError) -> str:
        return self.settings.titles.get(str(error.loc[0]), str(error.loc[0]))

    @_locked
    def state(self, notice: str = '') -> State:
        """Everything the page shows but a row's details, the plot, and the runs."""
        states, unusable = self._states()
        # With the settings refused, the rows are not compared with them.
        earlier = (
            set() if unusable else set(earlier_settings(states, self.batch.columns))
        )
        refused = self.pages.waiting(self.label, states)
        statuses = page_statuses(states, self.batch.columns, refused)
        failures = self._failures(states)
        table = self.batch.rows
        scan = {row.key: row for row in table.scan}
        added = {row.key for row in table.added}
        rows = [
            self._row(
                row,
                states[row.key],
                statuses[row.key],
                row.key in earlier,
                refused.get(row.key, ''),
                failures,
                scan,
                added,
            )
            for row in table
        ]
        counts = dict.fromkeys(_ORDER, 0)
        for status in statuses.values():
            counts[status] += 1
        given, missing = self.settings.held()

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
        fields = self.batch.spec.params.model_fields
        header, source = self.pages.manager.headers()[self.label], self.batch.source
        return State(
            version=self.version,
            start=self.start,
            runs=self.pages.runs_version if self.pages.corrects_runs else None,
            batches=self.pages.batches_version(),
            label=self.label,
            title=header.title,
            header=HeaderShown(
                workflow=self.batch.spec.title,
                source=header.source,
                index_title='' if source is None else source.index_title,
                description=header.description,
                start=header.scope.start,
                stop=header.scope.stop,
            ),
            application=self.pages.title,
            words=self.words,
            folder=self.pages.folder,
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
            removed=len(table.removed_in_scope),
            action=self._primary(
                states, statuses, unapplied or problem, len(earlier), unapplied
            ),
            settings=forms.tree(self.settings, marks),
            settings_problem=problem,
            settings_changed=unapplied is not None,
            refused=[self._refused(e) for e in self.refused],
            follow=self._follow(),
            notice=notice,
        )

    def _said(self, errors: Sequence[FieldError]) -> str:
        """Errors of the settings in words, each with its setting's title."""
        return '; '.join(
            _say(self._setting_title(e), e.msg) if e.loc else e.msg for e in errors
        )

    def _follow(self) -> Following | None:
        if (source := self.batch.source) is None:
            return None
        manager = self.pages.manager
        label, on = manager.following, self._follows()
        return Following(
            on=on,
            other=None if on or label is None else manager.headers()[label].title,
            interval=self.pages.interval,
            at=sorted(self._cells.titles[c] for c in source.fills),
            failure=self.pages.failure if on else None,
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
        row: TableRow,
        state: RowState,
        status: str,
        earlier: bool,
        refused: str,
        failures: Mapping[str, str],
        scan: Mapping[str, TableRow],
        added: set[str],
    ) -> Row:
        edits = self.batch.rows.edits.get(row.key, {})
        titles = {**self.settings.titles, **self._cells.titles}
        problems = {
            str(e.loc[0]): _say(titles.get(str(e.loc[0]), str(e.loc[0])), e.msg)
            for e in (*state.problems, *self.refused)
            if e.entry == row.key and e.loc
        }
        record = state.record
        reason = ''
        if refused:
            reason = refused
        elif status in ('failed', 'cancelled') and record is not None:
            reason = failures.get(record.id, '')
        elif problems:
            reason = '; '.join(problems.values())
        elif earlier:
            changed = self._changed(state, self.batch.settings_fields)
            reason = f'Reduced with earlier settings: {changed} changed since.'
        elif status == 'outdated' and state.outdated:
            reason = self._outdated(state)
        elif status == 'outdated':
            reason = 'The settings cannot be used, so this result may not match them.'
        if self._follows() and state.changed_by_hand and status in _BY_HAND:
            reason = f'{reason} {_LEFT}'.lstrip()
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
            earlier=earlier,
            record=None if record is None else record.id,
            result=result,
            cells=cells,
            strays=[n for n in row.values if n not in self.batch.columns],
        )

    def _primary(
        self,
        states: Mapping[str, RowState],
        statuses: Mapping[str, str],
        blocked: str | None,
        earlier: int,
        label: str | None = None,
    ) -> Action:
        """What the button does now, or ``label`` if blocked; how far rows run."""
        left = [] if blocked else left_to_reduce(states, self.batch.columns)
        kinds = {statuses[k] for k in left}
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
            earlier=earlier,
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
        )

    # The rows

    @_action
    def scan(self) -> str:
        """Make the rows from the files again; what was set by hand stays."""
        changes = self._scanned()
        return f'Found {self.words.count(len(self.batch.rows))}. {changes}'.strip()

    def _scan(self, failed: str = 'The scan failed') -> None:
        """The scan of "Rescan runs" and after a correction of runs."""
        self._corrected = self.pages.corrected
        try:
            self.batch.scan()
        except SubmitError as error:
            raise Problem('; '.join(map(str, error.errors))) from None
        except Exception as error:  # a source is code of the application
            raise Problem(f'{failed}: {error}') from None

    def _incomplete(self) -> set[str]:
        return {k for k, s in self._states()[0].items() if s.problems}

    def _renew_listing(self) -> None:
        """
        List again: new rows may name files that the listing lacks, and the
        Runs view shows the runs found, even if the scan then fails.
        """
        self._relist()
        self.pages.runs_listed(self._runs_seen())

    def _scanned(self) -> str:
        """
        Scan, and say how many rows changed and which the scan left
        incomplete.
        """
        before = {r.key: dict(r.values) for r in self.batch.rows}
        incomplete = self._incomplete()
        try:
            self._scan()
        except Problem:
            # The listing may fail for the reason that the scan failed; the
            # failure of the scan is what to say.
            with suppress(Exception):
                self._renew_listing()
            raise
        self._renew_listing()
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

    @_action
    def edit(self, edit: CellEdit) -> str:
        """Set one column of rows, as its field reads the value sent."""
        value = read(self._cells, edit)
        self._known(edit.keys)
        self.batch.rows = self.batch.rows.edit_each(
            edit.keys, {edit.column: EMPTY if value is UNSET else value}
        )
        self._forget(edit.keys, edit.column)
        if len(edit.keys) < 2:
            return ''
        title = self._cells.titles[edit.column]
        return f'{title} set on {self.words.count(len(edit.keys))}.'

    @_action
    def revert(self, cells: Cells) -> None:
        """
        Take the cells of ``column`` back to what the scan gave; for a name
        that is not a column, forget its edit.
        """
        self._known(cells.keys)
        self.batch.rows = self.batch.rows.reset_each(cells.keys, cells.column)
        self._forget(cells.keys, cells.column)

    def _forget(self, keys: Sequence[str], column: str) -> None:
        """Forget the reasons of a refusal at ``column`` of rows ``keys``."""
        self.refused = [
            e
            for e in self.refused
            if not (e.entry in keys and e.loc and str(e.loc[0]) == column)
        ]

    @_action
    def remove(self, keys: Sequence[str]) -> str:
        """Remove rows; one added by hand is gone for good."""
        self._known(keys)
        added = {row.key for row in self.batch.rows.added}
        self.batch.rows = self.batch.rows.remove_each(keys)
        notice = f'Removed {self.words.count(len(keys))}.'
        if gone := len(added.intersection(keys)):
            notice += f' {gone} added by hand cannot be brought back.'
        return notice

    @_action
    def restore(self) -> None:
        """Bring back every row removed that the scope holds."""
        rows = self.batch.rows
        self.batch.rows = rows.reset_each(rows.removed_in_scope)

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
        self._known(request.keys)
        if unapplied := self._unapplied():
            raise Problem(f'Nothing was reduced: {unapplied.lower()}.')
        try:
            columns = self.batch.columns
            rules: dict[str, Callable[[Mapping[str, RowState]], list[str]]] = {
                'remaining': lambda s: left_to_reduce(s, columns),
                'earlier': lambda s: earlier_settings(s, columns),
                'all': ready,
            }
            keys = (
                request.keys
                if request.which == 'selected'
                else rules[request.which](self.batch.state())
            )
            records = self.batch.reduce(keys)
        except SubmitError as error:
            self.refused = [e for e in error.errors if self._placed(e)]
            unplaced = [e for e in error.errors if e not in self.refused]
            raise _Refusal(
                [self._refused(e).text for e in unplaced], len(self.refused)
            ) from None
        self.refused = []
        self._relist()
        n = len(records)
        return f'Reducing {self.words.count(n)}.' if n else ''

    @_action
    def cancel(self) -> str:
        self.batch.cancel()
        return f'Cancelled the {self.words.many} still running.'

    # Follow

    @_action
    def follow(self, on: bool) -> str:
        """
        Make this batch the one that follows, with a first step at once; or,
        if it follows, let none follow.
        """
        if not on:
            if not self._follows():
                return ''  # another batch follows, or none
            self.pages.manager.follow(None)
            return 'Stopped following.'
        try:
            self.pages.manager.follow(self.label)
        except ValueError as error:
            raise Problem(f'{why(error)}.') from None
        self.pages.refused = {}
        self.pages.tick()
        return 'Following.'

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
                self._listing = Listing(self.batch.client, previous=self._listing)
                self.pages.runs_listed(self._runs_seen())
                if self._moved():
                    self._refresh()
            finally:
                if self._moved():
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
        given, missing = self.settings.held()
        if missing:
            raise Problem(f'Not applied: {self._said(missing)}.')
        self.batch.settings = given
        self._show_settings()
        # A refusal of a setting was a refusal of the settings replaced, and
        # Follow tries every row again with these at its next step.
        self.refused = [e for e in self.refused if e.entry]
        if self._follows():
            self.pages.refused = {}
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
        """Show the settings that the result of row ``key`` was reduced with."""
        name = self._named(self._row_of(key))
        row = self._states([key])[0][key]
        if row.record is None:
            raise Problem(f'{name} has no result.')
        if row.spec_changed:
            raise Problem(
                f'{name} was reduced by another version of the workflow, '
                'whose settings this one does not take.'
            )
        self._fill(row.record.request.params)
        return (
            f'The form shows the settings {name} was reduced with. Apply to use them.'
        )

    # The header

    @_action
    def change(self, edit: HeaderEdit) -> str:
        """
        Give the batch the title, description and scope of ``edit``
        (:meth:`~ess.apps.batch.BatchManager.change`). What the manager
        refuses is a :class:`Problem`, and changes nothing.
        """
        try:
            self.pages.manager.change(
                self.label,
                title=edit.title,
                description=edit.description,
                scope=Scope(start=edit.start, stop=edit.stop),
            )
        except ValueError as error:
            raise Problem(f'The batch was not changed: {why(error)}.') from None
        return 'Saved.'

    @_locked
    def start_next(self, request: Next) -> Made:
        """
        End the batch before row ``request.key``, and start the next batch
        there, called ``request.title``
        (:meth:`~ess.apps.batch.BatchManager.start_next`). Follow stays where
        it is: the rows that move have no result under the next batch, so a
        step would reduce them all again before a person can change its
        settings. What the manager refuses is a :class:`Problem`.
        """
        row = self._row_of(request.key)
        if row.index is None:
            source = self.batch.source
            what = 'index' if source is None else source.index_title.lower()
            raise Problem(
                f'{self._named(row)} has no {what}, so no batch can start at it.'
            )
        try:
            label = self.pages.manager.start_next(
                self.label, row.index, title=request.title
            )
        except NotKept as error:
            raise Problem(f'The change was not kept: {error}') from None
        except Exception as error:  # the scan runs code of the application
            raise Problem(f'The next batch was not made: {why(error)}.') from None
        return Made(label=label)

    # One row

    @_locked
    def row(self, key: str) -> RowDetail:
        """What the result of row ``key`` was reduced with."""
        self._row_of(key)
        state = self._states([key])[0][key]
        record = state.record
        if record is None or state.spec_changed:
            # another spec's params are not this one's
            return RowDetail(key=key, reduced_with=None, created=None, differs=[])
        given = self._form()
        given.set(record.request.params, self._listing)
        return RowDetail(
            key=key,
            reduced_with=forms.tree(given),
            created=record.created,
            differs=[
                n for n in self.batch.settings_fields if n in (state.changed or ())
            ],
        )

    # The result

    @_locked
    def plot(self, keys: Sequence[str]) -> Plot:
        """
        The figure of the results among the rows ``keys``, or of the first
        results if none is given, and a note of what it shows.
        """
        order = {row.key: i for i, row in enumerate(self.batch.rows)}
        states, _ = self._states(
            sorted({k for k in keys if k in order}, key=order.__getitem__)
            if keys
            else None
        )
        statuses = page_statuses(states, self.batch.columns)
        done = [
            s
            for s in states.values()
            if s.record is not None and s.status is Status.COMPLETED
        ]
        many = self.words.many
        if keys:
            chosen = done
            note = '' if chosen else f'None of the {many} selected has a result.'
        else:
            chosen = done[:_DRAWN]
            note = (
                f'No results yet. Reduce {many} to see them here.' if not done else ''
            )
            if len(done) > _DRAWN:
                note = f'The first {_DRAWN} of {len(done)}. Select {many} to choose.'
        draw = self.pages.draw.get(self.batch.spec.name)
        if not chosen or draw is None:
            return Plot(figure=None, note=note)
        colors = plots.colors([order[s.key] for s in chosen])
        shown = [
            Shown(
                s.key,
                self._named(self.batch.rows[s.key]),
                s.record,
                statuses[s.key] == 'outdated',
                c,
            )
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
        """The rows as a table file, ``.csv`` or ``.xlsx``."""
        with _file(f'{self.words.many}{suffix}') as path:
            frames.write_table(frames.batch_table(self.batch, self._states()[0]), path)
            return path.read_bytes()

    @_action
    def load_table(self, name: str, content: bytes) -> str:
        """
        Set the rows of a table file by hand (:meth:`RowTable.put`), so that
        they show what the file says, and stay so after a scan.
        """
        batch = self.batch
        try:
            if batch.source is not None:
                try:
                    batch.scan()  # the table is set on top of the rows of the files
                except Exception as error:  # a source is code of the application
                    raise Problem(
                        f'Not loaded, since the scan failed: {error}'
                    ) from None
            with _file(name, content) as path:
                try:
                    table = frames.read_table(path)
                    rows = frames.rows_of(table, batch.spec.params, batch.columns)
                    batch.put(rows)
                except SubmitError as error:
                    raise Problem('; '.join(map(str, error.errors))) from None
                except NotKept:
                    raise  # a fault of the definitions, not of the file
                except (OSError, ValueError, KeyError) as error:
                    raise Problem(f'Cannot load {name}: {why(error)}') from None
        finally:
            # The scan and the rows set may name files that the listing lacks,
            # even if the table is then refused.
            self._relist()
        return f'Loaded {self.words.count(len(rows))} from {name}.'

    @_locked
    def settings_file(self) -> bytes:
        """The batch's settings as a YAML template."""
        if unapplied := self._unapplied():
            raise Problem(f'{unapplied} first.')
        if missing := self.settings.held()[1]:
            raise Problem(f'Fill in the settings first: {SubmitError(missing)}')
        with _file('settings.yaml') as path:
            write_template(self.batch.template(), path)
            return path.read_bytes()

    @_action
    def load_settings(self, name: str, content: bytes) -> str:
        """Show the settings of a YAML template in the form, all or none."""
        cannot = f'Cannot load {name}:'
        with _file('settings.yaml', content) as path:
            try:
                template = load_template(path)
            except (OSError, ValueError):
                raise Problem(
                    f'{cannot} it is not a settings file saved from this page.'
                ) from None
        try:
            self._fill(checked_settings(self.batch, template))
        except (ValueError, KeyError) as error:
            raise Problem(f'{cannot} {forms.message(why(error))}.') from None
        return f'The form shows the settings of {name}. Apply to use them.'

    @_locked
    def results_file(self, keys: Sequence[str]) -> bytes:
        """A zip of the outputs of the done rows among ``keys``, or of all."""
        known = [k for k in keys if k in self.batch.rows]
        states, _ = self._states(known if keys else None)
        done = [k for k, s in states.items() if s.status is Status.COMPLETED]
        if not done:
            raise Problem(f'None of these {self.words.many} has a result.')
        buffer = io.BytesIO()
        with _file('results') as folder:
            table = frames.batch_table(self.batch, states, done)
            exported = export_table(self.batch.client, table, folder)
            frames.write_table(exported, folder / 'results.csv')
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

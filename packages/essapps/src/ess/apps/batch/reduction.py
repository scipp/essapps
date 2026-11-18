# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: scanning for rows, reducing them, and reading their results.

A :class:`Batch` is two parts. Its :class:`Table` holds the rows: it scans the
files with the source, and keeps the edits. Its :class:`Step` reduces them: it
holds the spec, the settings and the label, reads the records under the label,
and submits. The step is given the rows it works on, so it knows nothing of
files or edits; the table knows nothing of specs or records.

This module is the only part of the toolbox's logic that calls the client.
It needs no interface of its own over the client: tests and UI work run it on
``local()`` with fake workflows, which start in milliseconds and refuse and fail
as real ones do.

A row's state is read from the records, not from what this object submitted,
so every ``Batch`` on the label, and this one after a restart, sees the same,
but for how it resolved a file name, which it keeps until its next scan.
What a ``Step`` keeps of them can be rebuilt from the records at any time:
the newest record of each member under the label, the IDs of the records under
the label it has read, and which of them are not of the bound spec
(:meth:`~ess.apps.Client.bound`), all read from ``client.changes``, so that the
state of chosen rows does not read every record under the label.

The settings are the step's own (:attr:`Step.settings`), so each state is
read with them, and each reduction submits them. A state that resolves
nothing (:meth:`Step.results`) is for showing the rows while the settings are
refused, and for the checks before a submission.

Where a row's request differs from its result's (``RowState.changed``) is
worked out on both as a record holds them, resolving as little as it can::

    settings = client.resolve(Template(spec, params=settings, blanks=columns))
    # once per call; a refusal is raised: no row can run with these settings
    cells = {c: v for c, v in cells.items() if v != record.request.params[c]}
    # a row with a result: only the cells that differ as given, such as a
    # file named by run; a row without one: all its cells
    client.resolve({key: Template(spec, params=cells, blanks=the_others)})
    # one call for all rows; a refusal is a problem of its row
    changed = differs({**settings, **cells}, record.request.params, names)

A parameter resolves the same whatever the other parameters are, so a cell is
resolved on its own, and a value resolved once is kept by its column and value
until the next scan (:meth:`Step.forget_resolved`).

A stage is opened lazily and kept with the template it was opened from, as
given, since the stage's own holds resolved values. A refusal with no position
is the stage's, one that stopped or was released, so the stage is replaced and
the rows submitted once more. A refusal at a position is the row's or the
settings', and leaves the stage, which may have done its expensive part,
alone.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from ess.spec import FileRef, WorkflowSpec

from ..client import Client, Stage
from ..errors import FieldError, SubmitError
from ..files import Selector
from ..records import Record, SpecId, Status, Template
from . import definition, state
from .definition import DefinitionStore
from .state import RowState, earlier_settings, left_to_reduce
from .table import Row, RowTable


@dataclass(frozen=True)
class FileInfo:
    """A file and its metadata, as a :data:`RowSource` is given it."""

    ref: FileRef
    metadata: Mapping[str, Any]


def file_infos(client: Client, selector: Selector | None = None) -> list[FileInfo]:
    """The files ``selector`` matches, raw files if not given, with their metadata."""
    refs = client.files.list(Selector() if selector is None else selector)
    return [
        FileInfo(*pair) for pair in zip(refs, client.files.metadata(refs), strict=True)
    ]


_NONE = object()


def _kept(name: str, value: Any) -> tuple[str, Any] | None:
    """
    The key a resolved value is kept by, ``None`` for one that cannot be a key.
    The column is in it, since a value resolves per parameter: ``True`` is
    ``1`` at an ``int`` and refused at a ``StrictInt``. The type of the value,
    and of each element of a list or tuple, is in it too, since ``1``,
    ``1.0`` and ``True`` are equal keys in Python but not equal values to a
    parameter.
    """
    try:
        return (name, _typed(value))
    except TypeError:
        return None


def _typed(value: Any) -> tuple[type, Any]:
    """``value`` with its type at every level; raises ``TypeError`` if unhashable."""
    if isinstance(value, list | tuple):
        return (type(value), tuple(_typed(v) for v in value))
    hash(value)
    return (type(value), value)


def _running(row: RowState) -> list[FieldError]:
    if row.status is Status.PENDING:
        return [FieldError(row.key, (), 'still running')]
    return []


type RowSource = Callable[[Sequence[FileInfo]], Sequence[Row]]
"""
What an application knows of its technique: which files make which rows. It
takes plain data and returns plain data, so it is tested on its own.
"""


class Table:
    """
    The rows of a batch: those of the last scan, with the edits on top.

    Parameters
    ----------
    client:
        The client whose files are scanned.
    columns:
        The columns of the rows: those that :meth:`put` sets, and those that a
        row added by hand sets by hand.
    label:
        Names the edits in ``definitions``.
    source:
        Makes rows from files, for :meth:`scan`.
    selector:
        The files given to ``source``; raw files if not given.
    definitions:
        Keep the edits of the label (:mod:`~.definition`): read here, and
        written at each change. Without them, the edits are kept in memory only.

    Raises
    ------
    ValueError
        If ``definitions`` hold edits that cannot be read, naming the file and
        the entry; or if the label is too long to name their file.
    """

    def __init__(
        self,
        client: Client,
        *,
        columns: Sequence[str],
        label: str,
        source: RowSource | None = None,
        selector: Selector | None = None,
        definitions: DefinitionStore | None = None,
    ) -> None:
        self.client = client
        self.columns = tuple(columns)
        self.label = label
        self.source = source
        self._selector = Selector() if selector is None else selector
        self._definitions = definitions
        self._rows = RowTable()
        self._scanned = False
        if definitions is not None:
            edits = definitions.load(definition.edits_key(label), definition.edits_of)
            if edits is not None:
                self._rows = edits

    @property
    def rows(self) -> RowTable:
        """
        The rows: those of the last scan, with what was set by hand on top. An
        assignment is written to the definitions, if the table has them, and
        is not made if they cannot keep it.
        """
        return self._rows

    @rows.setter
    def rows(self, rows: RowTable) -> None:
        if self._definitions is not None:
            key = definition.edits_key(self.label)
            self._definitions.save(key, definition.edits_data(rows))
        self._rows = rows

    def select(self, keys: Sequence[str] | None = None) -> list[Row]:
        """The rows of ``keys``, or all; raises ``KeyError`` for a key not a row."""
        return list(self._rows) if keys is None else [self._rows[k] for k in keys]

    def put(self, rows: Iterable[Row]) -> None:
        """
        Set ``rows`` by hand, as a table of them gives them
        (:meth:`RowTable.put`). Raises ``ValueError`` if the table has a source
        and has not scanned, since every row would then be added, and none
        would follow the files.
        """
        if self.source is not None and not self._scanned:
            raise ValueError('scan the files first: the rows are set on top of them')
        self.rows = self._rows.put(rows, self.columns)

    def scan(self) -> list[FileInfo]:
        """
        Make rows from the files now; what was set by hand stays. Returns the
        files, as the source got them.
        """
        if self.source is None:
            raise ValueError('this batch has no source of rows')
        infos = file_infos(self.client, self._selector)
        # What was set by hand stays, so there is nothing to write.
        self._rows = self._rows.rescan(self.source(infos))
        self._scanned = True
        return infos

    def set_by_hand(self) -> dict[str, frozenset[str]]:
        """The columns set by hand of each row: its edits, or all if added."""
        rows = self._rows
        added = dict.fromkeys((r.key for r in rows.added), frozenset(self.columns))
        return {k: frozenset(cells) for k, cells in rows.edits.items()} | added


class Step:
    """
    One spec reduced for rows that it is given: its settings, the label of its
    records, and where each row stands against them.

    Parameters
    ----------
    client:
        The client that submits.
    spec:
        The spec of every row.
    columns:
        The parameters that each row gives, the blanks of the template. The
        other parameters are the settings, shared by all rows.
    label:
        The label of the records. A row's key is its member.
    takes:
        The column of the table that a parameter of ``columns`` takes, if not
        the column of its name. The rows given and the states returned are in
        the names of the columns; only the requests are in those of the
        parameters.
    name:
        Names the step in a batch, and its output columns
        ``'<name>.<output>'``; the spec's name if not given.
    staged:
        Run the rows through a stage, so that what they share is computed
        once. The spec's binding must make stages.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    settings:
        The settings that the step starts with if ``definitions`` hold none,
        such as the technique's defaults; none if not given, so each parameter
        takes its default.
    definitions:
        Keep the settings of the label (:mod:`~.definition`): read here, and
        written at each change. Without them, the settings are kept in memory
        only.

    Raises
    ------
    KeyError
        If a column is not a parameter of the spec, or ``takes`` names a
        parameter that is not a column.
    ValueError
        If ``settings`` name a column.
    ValueError
        If ``definitions`` hold settings that cannot be read, or settings of a
        spec of another name, naming the file and the entry; or if the label
        is too long to name their file.
    """

    def __init__(
        self,
        client: Client,
        spec: WorkflowSpec[Any, Any],
        *,
        columns: Sequence[str],
        label: str,
        takes: Mapping[str, str] | None = None,
        name: str | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
        settings: Mapping[str, Any] | None = None,
        definitions: DefinitionStore | None = None,
    ) -> None:
        fields = spec.params.model_fields
        if unknown := sorted(set(columns) - fields.keys()):
            raise KeyError(f'{spec.title} has no parameters {unknown}')
        if unknown := sorted(set(takes or {}) - set(columns)):
            raise KeyError(f'{unknown} are not columns of {spec.title}')
        self.client = client
        self.spec = spec
        self.columns = tuple(columns)
        self.settings_fields = tuple(n for n in fields if n not in columns)
        self.label = label
        self.name = spec.name if name is None else name
        self.takes = {c: c for c in self.columns} | dict(takes or {})
        self._renamed = any(p != c for p, c in self.takes.items())
        self._definitions = definitions
        self._settings = self._own(settings or {})
        if definitions is not None:
            kept = definitions.load(
                definition.settings_key(label),
                lambda data: definition.settings_of(data, spec),
            )
            if kept is not None:
                self._settings = kept
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        # The records under the label, read from the proposal's changes.
        self._cursor: int | None = None
        self._seen: set[str] = set()
        self._found: dict[str, Record] = {}
        self._unbound: set[str] = set()
        # A cell's value as a record holds it, by its column and value as given.
        self._resolved: dict[tuple[str, Any], Any] = {}

    @property
    def settings(self) -> Mapping[str, Any]:
        """
        The settings in force: a value for each parameter that is not a
        column, where it is not left to its default. A copy, read-only at the
        top, so that only an assignment of all of them changes the step::

            step.settings = {**step.settings, 'q': {'num_bins': 50}}

        An assignment reduces no row: the rows reduced before read
        ``outdated``, with the setting in :attr:`RowState.changed`. The
        workspace checks the settings when :meth:`state` resolves them, and
        refuses a name that is not a parameter. Raises ``ValueError`` for a
        name that is a column: a column's value is a cell of each row.
        """
        return MappingProxyType(copy.deepcopy(self._settings))

    @settings.setter
    def settings(self, settings: Mapping[str, Any]) -> None:
        settings = self._own(settings)
        if self._definitions is not None:
            template = Template(self.spec, params=settings, blanks=self.columns)
            key = definition.settings_key(self.label)
            self._definitions.save(key, definition.settings_data(template))
        self._settings = settings

    def _own(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """A copy of ``settings``; raises ``ValueError`` for a column in them."""
        if at := sorted(settings.keys() & set(self.columns)):
            raise ValueError(f'{at}: columns of the batch, not settings')
        return copy.deepcopy(dict(settings))

    def template(self) -> Template:
        """The template of the settings, with the columns as its blanks."""
        return Template(self.spec, params=self._settings, blanks=self.columns)

    @property
    def outputs(self) -> dict[str, str]:
        """The name of each output by its column, ``'<name>.<output>'``."""
        return {f'{self.name}.{o}': o for o in self.spec.outputs.model_fields}

    def _in_params(self, row: Row) -> Row:
        """
        ``row`` in the names of the parameters: the cell of each column taken
        under its parameter, and any other value under its own name, so that
        it is a problem of the row (``not a column``).
        """
        if not self._renamed:
            return row
        columns = set(self.takes.values())
        values = {p: row.values[c] for p, c in self.takes.items() if c in row.values}
        values |= {n: v for n, v in row.values.items() if n not in columns}
        return replace(row, values=values)

    def _column(self, name: str) -> str:
        """The column a parameter takes; a setting keeps its name."""
        return self.takes.get(name, name)

    def _placed(self, errors: Iterable[FieldError]) -> tuple[FieldError, ...]:
        """``errors`` at the columns that their parameters take."""
        return tuple(
            replace(e, loc=(self._column(str(e.loc[0])), *e.loc[1:])) if e.loc else e
            for e in errors
        )

    def _in_columns(self, row: RowState, waits: tuple[str, ...]) -> RowState:
        """
        ``row`` in the names of the columns, waiting for ``waits``: a problem
        at a column it waits for is the cell that is empty, so it is dropped.
        """
        if not self._renamed and not waits:
            return row
        problems = [
            e for e in self._placed(row.problems) if not (e.loc and e.loc[0] in waits)
        ]
        changed = row.changed
        if changed is not None:
            changed = frozenset(map(self._column, changed))
        return replace(row, problems=tuple(problems), changed=changed, waits=waits)

    def forget_resolved(self) -> None:
        """
        Resolve each cell again from now on. Called after a scan: a file that
        gained a catalogue identifier is resolved to it from then on, by any
        of its names, its old identity among them.
        """
        self._resolved.clear()

    def _cells(self, row: Row) -> dict[str, Any]:
        """The value of each column of a row, the default of an empty one."""
        fields = self.spec.params.model_fields
        return {
            c: row.values[c]
            if c in row.values
            else fields[c].get_default(call_default_factory=True)
            for c in self.columns
        }

    def state(
        self,
        rows: Sequence[Row],
        set_by_hand: Mapping[str, frozenset[str]] = MappingProxyType({}),
        waits: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
    ) -> dict[str, RowState]:
        """
        Where each of ``rows`` stands now, read from the client: its result,
        and where its request with the settings differs from its result's
        (:attr:`RowState.changed`), with the reasons a cell is refused, as
        problems of its row. ``set_by_hand`` names the columns set by hand of
        each row, and ``waits`` the empty output columns it waits for, by key.

        Raises ``SubmitError`` if the settings are refused, with reasons that
        name no row: no row can run with them.
        """
        given = [self._in_params(r) for r in rows]
        found = self._results_of(given)
        changed, refused = self._compare(given, found)
        states = self._states(given, found, changed, refused, set_by_hand)
        return {k: self._in_columns(s, waits.get(k, ())) for k, s in states.items()}

    def results(
        self,
        rows: Sequence[Row],
        set_by_hand: Mapping[str, frozenset[str]] = MappingProxyType({}),
        waits: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
    ) -> dict[str, RowState]:
        """
        Each of ``rows`` with its result, without its request: the problems of
        its cells as given, its status, and whether its result is of another
        spec. Nothing is resolved, so :attr:`RowState.changed` is ``None``, and
        so is :attr:`RowState.outdated` unless the result is of another spec:
        :func:`~.state.left_to_reduce` and :func:`~.state.arrived` refuse these
        states.
        """
        given = [self._in_params(r) for r in rows]
        found = self._results_of(given)
        states = self._states(given, found, None, {}, set_by_hand)
        return {k: self._in_columns(s, waits.get(k, ())) for k, s in states.items()}

    def _results_of(self, rows: Sequence[Row]) -> dict[str, Record]:
        """The result of each of ``rows`` that has one, by key."""
        self._poll()
        return {r.key: self._found[r.key] for r in rows if r.key in self._found}

    def _states(
        self,
        rows: Sequence[Row],
        found: Mapping[str, Record],
        changed: Mapping[str, frozenset[str]] | None,
        refused: Mapping[str, tuple[FieldError, ...]],
        set_by_hand: Mapping[str, frozenset[str]],
    ) -> dict[str, RowState]:
        upstream = self._known(sorted(state.references(rows)))
        status = self._status([*found.values(), *upstream])
        return state.row_states(
            rows,
            self.spec.params,
            self.columns,
            found,
            status,
            changed,
            refused,
            self._spec_changed(found),
            set_by_hand,
        )

    def _poll(self) -> None:
        """
        Bring the result of each member under the label up to date, from the
        proposal's changes; the first call reads every record under the label.
        """
        if self._cursor is None:
            self._cursor = self.client.changes().cursor
            records = self.client.records(label=self.label)
        else:
            changes = self.client.changes(after=self._cursor)
            self._cursor = changes.cursor
            # A record is in the changes when submitted and when it finishes,
            # so one seen before is not newer than the result held.
            new = [i for i in changes.records if i not in self._seen]
            records = [r for r in self._known(new) if r.label == self.label]
        self._seen.update(r.id for r in records)
        self._found |= state.results(records)
        bound = self.client.bound(records) if records else []
        self._unbound.update(r.id for r, b in zip(records, bound, strict=True) if not b)

    def _known(self, ids: list[str]) -> list[Record]:
        """
        The records of ``ids`` that the client knows. If one is unknown, the
        others are read one by one, so that it touches only the rows that
        reference it, whose submission it refuses.
        """
        try:
            return self.client.records(ids=ids) if ids else []
        except KeyError:
            if len(ids) == 1:
                return []
            return [r for i in ids for r in self._known([i])]

    def _status(self, records: list[Record]) -> dict[str, Status]:
        statuses = self.client.status(records)
        return {r.id: s for r, s in zip(records, statuses, strict=True)}

    def _spec_changed(self, found: Mapping[str, Record]) -> set[str]:
        """The keys whose result is of another spec than the bound one."""
        spec = SpecId.of(self.spec)
        return {
            key for key, r in found.items() if r.spec != spec or r.id in self._unbound
        }

    def _compare(
        self, rows: Sequence[Row], found: Mapping[str, Record]
    ) -> tuple[dict[str, frozenset[str]], dict[str, tuple[FieldError, ...]]]:
        """
        For each row with a result, the names at which its request with the
        settings differs from its result's; and the reasons of the rows whose
        cells are refused. Raises ``SubmitError`` if the settings are.
        """
        resolved = self.client.resolve(self.template())
        if isinstance(resolved, SubmitError):
            raise resolved
        fields = self.spec.params.model_fields
        required = {c for c in self.columns if fields[c].is_required()}
        asked = {}
        for row in rows:
            if not required <= row.values.keys():
                continue  # an empty cell is a problem, and nothing to resolve
            if (record := found.get(row.key)) is None:
                asked[row.key] = {
                    c: row.values[c] for c in self.columns if c in row.values
                }
            else:
                cells = self._cells(row)
                was = record.request.params
                asked[row.key] = {
                    c: cells[c] for c in state.differs(cells, was, self.columns)
                }
        cells_now, refused = self._resolve_cells(asked)
        changed = {}
        for key, record in found.items():
            if key in asked:
                now = {**resolved.params, **cells_now[key]}
                names = [*self.settings_fields, *cells_now[key]]
                changed[key] = frozenset(
                    state.differs(now, record.request.params, names)
                )
        return changed, refused

    def _resolve_cells(
        self, asked: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[FieldError, ...]]]:
        """
        The cells of each row, by key, as a record holds them, and the reasons
        of each row whose cells are refused. A row asks for its cells not
        resolved before in a template of their own, every other parameter
        blank, all rows in one call of :meth:`~ess.apps.Client.resolve`.

        A value resolved is kept by its column and value until
        :meth:`forget_resolved`, since a parameter resolves the same whatever
        the others are. A kept value differs from one resolved now only for a
        file that gained a catalogue identifier since, which keeps its old
        identity until then, as ``resolve`` allows. A list is kept by its
        elements; a value that cannot be a key, such as a dict, is resolved
        each time. A refusal is not kept, so that a file that arrives later is
        found.
        """
        fields = self.spec.params.model_fields
        cells: dict[str, dict[str, Any]] = {}
        new: dict[str, dict[str, Any]] = {}
        for key, given in asked.items():
            cells[key] = {}
            for name, value in given.items():
                kept = self._resolved.get(_kept(name, value), _NONE)
                if kept is _NONE:
                    new.setdefault(key, {})[name] = value
                else:
                    cells[key][name] = kept
        templates = {
            key: Template(
                self.spec, params=given, blanks=[n for n in fields if n not in given]
            )
            for key, given in new.items()
        }
        resolved = self.client.resolve(templates) if templates else {}
        refused = {}
        for key, result in resolved.items():
            if isinstance(result, SubmitError):
                refused[key] = result.errors
                continue
            for name, value in new[key].items():
                cells[key][name] = result.params[name]
                if (at := _kept(name, value)) is not None:
                    self._resolved[at] = result.params[name]
        return cells, refused

    def reduce(self, rows: Sequence[Row]) -> dict[str, Record]:
        """
        Submit ``rows`` with the settings, all or none; their records by key.

        Raises ``SubmitError`` if a row cannot run or is still running, naming
        it by key and the cell by column. A reason about the settings is named
        once, without a key.
        """
        states = self.results(rows)
        refused = [
            e
            for row in rows
            for e in (*states[row.key].problems, *_running(states[row.key]))
        ]
        if refused:
            raise SubmitError(refused)
        if not rows:
            return {}
        template = self.template()
        each = {row.key: self._cells(self._in_params(row)) for row in rows}
        try:
            records = self._submit(template, each)
        except SubmitError as error:
            raise SubmitError(self._placed(self._once(error.errors))) from None
        return records

    def _submit(
        self, template: Template, each: dict[str, dict[str, Any]]
    ) -> dict[str, Record]:
        options: dict[str, Any] = {'label': self.label, 'persist': self._persist}
        if not self._staged:
            requests = {k: template.fill(params=c) for k, c in each.items()}
            return self.client.submit(requests, **options)
        try:
            return self.client.submit(self._open(template), each=each, **options)
        except SubmitError as error:
            if any(e.loc for e in error.errors):
                raise
        self.close()
        return self.client.submit(self._open(template), each=each, **options)

    def _open(self, template: Template) -> Stage:
        """The stage of ``template``: the open one if it is of the same."""
        if self._stage is None or self._stage[0] != template:
            stage = self.client.stage(template)
            self.close()
            self._stage = (template, stage)
        return self._stage[1]

    def _once(self, errors: tuple[FieldError, ...]) -> list[FieldError]:
        """Each reason that names no column once, without a row's key."""
        placed = [
            e if e.loc and e.loc[0] in self.columns else replace(e, entry=None)
            for e in errors
        ]
        return list(dict.fromkeys(placed))

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        """
        Cancel the records still running that are the newest under the label
        for their member, of the members ``keys`` if given, or of every member,
        whether or not it is a row now.
        """
        self._poll()
        chosen = [r for k, r in self._found.items() if keys is None or k in keys]
        status = self._status(chosen)
        self.client.cancel([r for r in chosen if status[r.id] is Status.PENDING])

    def close(self) -> None:
        """Release the open stage, if any; what was submitted through it still runs."""
        if self._stage is not None:
            self.client.release(self._stage[1])
            self._stage = None


@dataclass
class StepRows:
    """
    The rows of a batch as one of its steps takes them, at that step of the
    walk along the chain (:meth:`Batch.walk`): the cells of the columns it
    takes, with the output cells of the steps before it, and the output
    columns each row waits for.
    """

    step: Step
    rows: list[Row]
    set_by_hand: Mapping[str, frozenset[str]]
    waits: Mapping[str, tuple[str, ...]]
    _state: dict[str, RowState] | None = field(default=None, init=False, repr=False)

    def state(self) -> dict[str, RowState]:
        """:meth:`Step.state` of the rows; read once until :meth:`reduce`."""
        if self._state is None:
            self._state = self.step.state(self.rows, self.set_by_hand, self.waits)
        return self._state

    def results(self) -> dict[str, RowState]:
        """:meth:`Step.results` of the rows."""
        return self.step.results(self.rows, self.set_by_hand, self.waits)

    def reduce(self, keys: Sequence[str]) -> dict[str, Record]:
        """:meth:`Step.reduce` of the rows of ``keys``."""
        rows = {r.key: r for r in self.rows}
        self._state = None
        return self.step.reduce([rows[k] for k in keys])


def _current(row: RowState, columns: Collection[str]) -> bool:
    """
    Whether the result of ``row`` fills its output cells: it runs or
    completed, of the bound spec, for the cells the row has now at
    ``columns``. Settings do not count, so a change of settings leaves the
    results in use until a person reduces them again.
    """
    return (
        row.record is not None
        and row.status not in (Status.FAILED, Status.CANCELLED)
        and not row.spec_changed
        and not row.problems
        and not row.waits
        and row.changed is not None
        and row.changed.isdisjoint(columns)
    )


class Batch:
    """
    A table of rows and a chain of steps that reduce them; one step unless
    more are added (:meth:`add`). See ``packages/essapps/docs/batch.md``.

    The constructor makes the table and its first step, whose records are
    under ``label``. An added step's records are under ``'<label>/<name>'``,
    and each of its outputs is a column ``'<name>.<output>'``. An output cell
    holds a reference to the step's result for the row if that result is
    current, and is empty otherwise; a step that takes an empty output cell
    waits (:attr:`RowState.waits`).

    Parameters
    ----------
    client:
        The client that submits.
    spec:
        The spec of the first step.
    columns:
        The parameters of the first step that each row gives, the blanks of the
        template. The other parameters are the settings, shared by all rows.
    label:
        The label of the first step's records, and of the definitions. A row's
        key is its member.
    takes:
        The column that a parameter of ``columns`` takes, if not the column of
        its name, such as ``{'run': 'sample'}``.
    source:
        Makes rows from files, for :meth:`scan`. A value at a name that no
        step takes makes its row ``incomplete`` (``not a column``), so add the
        steps that take its columns before the rows are reduced.
    selector:
        The files given to ``source``; raw files if not given.
    staged:
        Run the rows through a stage, so that what they share is computed
        once. The spec's binding must make stages.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    settings:
        The settings that the batch starts with if ``definitions`` hold none,
        such as the technique's defaults; none if not given, so each parameter
        takes its default.
    definitions:
        Keep the edits and the settings of the label (:mod:`~.definition`):
        read here, and written at each change. Without them, the edits and
        settings are kept in memory only.

    Raises
    ------
    KeyError
        If a column is not a parameter of the spec.
    ValueError
        If ``settings`` name a column.
    ValueError
        If ``definitions`` hold edits or settings that cannot be read, or
        settings of a spec of another name, naming the file and the entry; or
        if the label is too long to name their file.
    """

    def __init__(
        self,
        client: Client,
        spec: WorkflowSpec[Any, Any],
        *,
        columns: Sequence[str],
        label: str,
        takes: Mapping[str, str] | None = None,
        source: RowSource | None = None,
        selector: Selector | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
        settings: Mapping[str, Any] | None = None,
        definitions: DefinitionStore | None = None,
    ) -> None:
        self.client = client
        self.label = label
        self._definitions = definitions
        first = Step(
            client,
            spec,
            columns=columns,
            label=label,
            takes=takes,
            staged=staged,
            persist=persist,
            settings=settings,
            definitions=definitions,
        )
        self._steps = [first]
        self._table = Table(
            client,
            columns=tuple(dict.fromkeys(first.takes.values())),
            label=label,
            source=source,
            selector=selector,
            definitions=definitions,
        )
        # The columns of the first step stay when steps are removed.
        self._first_columns = self._table.columns

    @property
    def steps(self) -> tuple[Step, ...]:
        """The steps, in the order of the chain."""
        return tuple(self._steps)

    def step(self, name: str) -> Step:
        """The step of ``name``; raises ``KeyError`` if there is none."""
        for step in self._steps:
            if step.name == name:
                return step
        raise KeyError(f'{self.label} has no step {name!r}')

    @property
    def columns(self) -> tuple[str, ...]:
        """The input columns, those the steps take that are not outputs."""
        return self._table.columns

    @property
    def outputs(self) -> dict[str, Step]:
        """The output columns, each with the step that fills it."""
        return {c: step for step in self._steps for c in step.outputs}

    def _only(self) -> Step:
        """The step of a batch of one; raises ``ValueError`` for several."""
        if len(self._steps) > 1:
            raise ValueError(f'{self.label} has several steps: see Batch.steps')
        return self._steps[0]

    @property
    def spec(self) -> WorkflowSpec[Any, Any]:
        """The spec of every row, of a batch of one step."""
        return self._only().spec

    @property
    def settings_fields(self) -> tuple[str, ...]:
        """The parameters that are not columns, of a batch of one step."""
        return self._only().settings_fields

    @property
    def source(self) -> RowSource | None:
        """Makes rows from files, for :meth:`scan`."""
        return self._table.source

    def add(
        self,
        spec: WorkflowSpec[Any, Any],
        *,
        name: str | None = None,
        takes: Mapping[str, str] | None = None,
        settings: Mapping[str, Any] | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
    ) -> Step:
        """
        Add a step at the end of the chain, its records under
        ``'<label>/<name>'``, its name the spec's if not given.

        A parameter takes the column that ``takes`` names, a new input column
        if the batch has none of that name; else the column of its name, if
        the batch has one and ``settings`` do not name it; else it is a
        setting. The settings kept in the definitions for the step's label
        replace ``settings``, as for the first step.

        Raises ``ValueError`` if a step has the name, and ``KeyError`` if
        ``takes`` names a parameter the spec lacks, or a column with a ``.``
        that is not an output of an earlier step.
        """
        name = spec.name if name is None else name
        if name in {s.name for s in self._steps}:
            raise ValueError(f'{self.label} has a step {name!r}')
        takes = dict(takes or {})
        fields = spec.params.model_fields
        if unknown := sorted(takes.keys() - fields.keys()):
            raise KeyError(f'{spec.title} has no parameters {unknown}')
        outputs = self.outputs
        if bad := sorted(c for c in takes.values() if '.' in c and c not in outputs):
            raise KeyError(f'{bad} are not outputs of earlier steps')
        known = {*self.columns, *outputs}
        for n in fields:
            if n not in takes and n in known and n not in (settings or {}):
                takes[n] = n
        step = Step(
            self.client,
            spec,
            columns=[n for n in fields if n in takes],
            label=f'{self.label}/{name}',
            takes=takes,
            name=name,
            staged=staged,
            persist=persist,
            settings=settings,
            definitions=self._definitions,
        )
        new = [c for c in step.takes.values() if c not in known]
        self._table.columns = tuple(dict.fromkeys([*self.columns, *new]))
        self._steps.append(step)
        return step

    def remove(self, name: str) -> list[str]:
        """
        Remove the step of ``name``, and the columns that only it took but for
        those of the first step; returns those columns. The edits at them go
        too. Its records and kept settings stay.

        Raises ``ValueError`` if another step takes one of its outputs, or if
        it is the only step.
        """
        step = self.step(name)
        if len(self._steps) == 1:
            raise ValueError(f'{name!r} is the only step of {self.label}')
        if users := [
            s.name for s in self._steps if set(s.takes.values()) & set(step.outputs)
        ]:
            raise ValueError(f'{users} take the output of {name!r}')
        self._steps.remove(step)
        step.close()
        taken = {c for s in self._steps for c in s.takes.values()}
        gone = [
            c for c in self.columns if c not in taken and c not in self._first_columns
        ]
        gone += list(step.outputs)
        rows = self._table.rows
        if edited := [k for k, cells in rows.edits.items() if set(gone) & cells.keys()]:
            self._table.rows = rows.reset_each(edited, *gone)
        self._table.columns = tuple(c for c in self.columns if c not in gone)
        return gone

    @property
    def rows(self) -> RowTable:
        """
        The rows: those of the last scan, with what was set by hand on top. An
        assignment is written to the definitions, if the batch has them, and
        is not made if they cannot keep it.
        """
        return self._table.rows

    @rows.setter
    def rows(self, rows: RowTable) -> None:
        self._table.rows = rows

    def put(self, rows: Iterable[Row]) -> None:
        """
        Set ``rows`` by hand, as a table of them gives them
        (:meth:`RowTable.put`). Raises ``ValueError`` if the batch has a source
        and has not scanned, since every row would then be added, and none
        would follow the files.
        """
        self._table.put(rows)

    def scan(self) -> list[FileInfo]:
        """
        Make rows from the files now; what was set by hand stays. Returns the
        files, as the source got them.
        """
        infos = self._table.scan()
        for step in self._steps:
            step.forget_resolved()
        return infos

    @property
    def settings(self) -> Mapping[str, Any]:
        """
        The settings in force, of a batch of one step: a value for each
        parameter that is not a column, where it is not left to its default.
        A copy, read-only at the top, so that only an assignment of all of
        them changes the batch::

            batch.settings = {**batch.settings, 'q': {'num_bins': 50}}

        An assignment reduces no row: the rows reduced before read
        ``outdated``, with the setting in :attr:`RowState.changed`. The
        workspace checks the settings when :meth:`state` resolves them, and
        refuses a name that is not a parameter. Raises ``ValueError`` for a
        name that is a column: a column's value is a cell of each row. The
        settings of a step of several are :attr:`Step.settings`.
        """
        return self._only().settings

    @settings.setter
    def settings(self, settings: Mapping[str, Any]) -> None:
        self._only().settings = settings

    def template(self) -> Template:
        """The template of the settings, with the columns as its blanks."""
        return self._only().template()

    def walk(self, keys: Sequence[str] | None = None) -> Iterator[StepRows]:
        """
        The rows of ``keys``, or all, as each step takes them, in the order of
        the chain. Between two steps the caller may reduce rows at the step
        given; the walk then reads its state again, so that the output cells
        that the next step takes hold the records just submitted. A reference
        to a record that still runs fills a cell, so one walk can submit the
        whole chain for a row.
        """
        rows = self._table.select(keys)
        hand = self._table.set_by_hand()
        outputs = self.outputs
        known = {*self.columns, *outputs}
        filled: dict[str, dict[str, Any]] = {r.key: {} for r in rows}
        for i, step in enumerate(self._steps):
            taken = set(step.takes.values())
            given, waits = [], {}
            for row in rows:
                values = (
                    {**filled[row.key], **row.values} if filled[row.key] else row.values
                )
                if at := tuple(
                    c for c in step.takes.values() if c in outputs and c not in values
                ):
                    waits[row.key] = at
                if other := [n for n in values if n in known and n not in taken]:
                    values = {n: v for n, v in values.items() if n not in other}
                changed = filled[row.key] or other
                given.append(replace(row, values=values) if changed else row)
            mine = {
                k: frozenset(c for c in cells if c in taken or c not in known)
                for k, cells in hand.items()
            }
            here = StepRows(step, given, mine, waits)
            yield here
            if i + 1 == len(self._steps):
                return
            try:
                states = here.state()
            except SubmitError:
                continue  # the settings are refused: no output is current
            for key, s in states.items():
                if s.record is not None and _current(s, taken):
                    refs = {c: s.record.ref(o) for c, o in step.outputs.items()}
                    filled[key] |= refs

    def state(self, keys: Sequence[str] | None = None) -> dict[str, RowState]:
        """
        Where each row stands now, of a batch of one step, read from the
        client, or the rows of ``keys`` if given: its result, and where its
        request with the settings differs from its result's
        (:attr:`RowState.changed`), with the reasons a cell is refused, as
        problems of its row.

        Raises ``SubmitError`` if the settings are refused, with reasons that
        name no row: no row can run with them.
        """
        self._only()
        return next(self.walk(keys)).state()

    def states(
        self, keys: Sequence[str] | None = None
    ) -> dict[str, dict[str, RowState]]:
        """:meth:`state` of each step, by its name, along the chain."""
        return {here.step.name: here.state() for here in self.walk(keys)}

    def results(self, keys: Sequence[str] | None = None) -> dict[str, RowState]:
        """
        Each row, or each of ``keys``, with its result, without its request, of
        a batch of one step: the problems of its cells as given, its status,
        and whether its result is of another spec. Nothing is resolved, so
        :attr:`RowState.changed` is ``None``, and so is
        :attr:`RowState.outdated` unless the result is of another spec:
        :func:`~.state.left_to_reduce` and :func:`~.state.arrived` refuse these
        states.

        It is what a page shows while the settings are refused, and what
        :meth:`reduce` checks before it submits. Which rows to reduce is
        chosen from :meth:`state`.
        """
        self._only()
        return next(self.walk(keys)).results()

    def reduce(self, keys: Sequence[str]) -> dict[str, Record]:
        """
        Submit the rows of ``keys`` with the settings, all or none, of a batch
        of one step; their records by key.

        Raises ``SubmitError`` if a row cannot run or is still running, naming
        it by key and the cell by column. A reason about the settings is named
        once, without a key.
        """
        self._only()
        return next(self.walk(keys)).reduce(keys)

    def reduce_left(
        self, keys: Sequence[str] | None = None, *, again: Collection[str] = ()
    ) -> dict[str, Record]:
        """
        "Reduce" of a person, on the rows of ``keys`` or all: at each step in
        order, the rows of :func:`~.state.left_to_reduce`, and at the steps
        named in ``again`` those of :func:`~.state.earlier_settings` too. A
        row's later steps take what its earlier ones submit, so one call
        reduces a row to the end of the chain. Returns the last record
        submitted for each row, which reads the others.

        Raises ``SubmitError`` as :meth:`Step.reduce`, with the steps before
        the one refused submitted.
        """
        records: dict[str, Record] = {}
        for here in self.walk(keys):
            state = here.state()
            columns = set(here.step.takes.values())
            chosen = left_to_reduce(state, columns)
            if here.step.name in again:
                chosen += earlier_settings(state, columns)
            records |= here.reduce(chosen)
        return records

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        """
        Cancel the records still running that are the newest under the label
        of each step for their member, of the members ``keys`` if given, or of
        every member, whether or not it is a row now.
        """
        for step in self._steps:
            step.cancel(keys)

    def close(self) -> None:
        """Release the open stages; what was submitted through them still runs."""
        for step in self._steps:
            step.close()

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: scanning for rows, reducing them, and reading their results.

:class:`Batch` is the only part of the toolbox's logic that calls the client.
It needs no interface of its own over the client: tests and UI work run it on
``local()`` with fake workflows, which start in milliseconds and refuse and fail
as real ones do.

A row's state is read from the records, not from what this object submitted,
so every ``Batch`` on the label, and this one after a restart, sees the same,
but for how it resolved a file name, which it keeps until its next scan.
What a ``Batch`` keeps of them can be rebuilt from the records at any time:
the newest record of each member under the label, the IDs of the records under
the label it has read, and which of them are not of the bound spec
(:meth:`~ess.apps.Client.bound`), all read from ``client.changes``, so that the
state of chosen rows does not read every record under the label. Only a tweak
is kept that the records cannot explain (:meth:`Batch.tweak`).

The settings are the batch's own (:attr:`Batch.settings`), so each state is
read with them, and each reduction submits them. A state that resolves
nothing (:meth:`Batch.results`) is for showing the rows while the settings are
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
until the next scan (:meth:`Batch._resolve_cells`).

A stage is opened lazily and kept with the template it was opened from, as
given, since the stage's own holds resolved values. A refusal with no position
is the stage's, one that stopped or was released, so the stage is replaced and
the rows submitted once more. A refusal at a position is the row's or the
settings', and leaves the stage, which may have done its expensive part,
alone.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from ess.spec import FileRef, WorkflowSpec

from ..client import Client, Stage
from ..errors import FieldError, SubmitError
from ..files import Selector
from ..records import Record, Request, SpecId, Status, Template
from . import definition, state
from .definition import DefinitionStore
from .state import RowState
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


class Batch:
    """
    One spec reduced for many rows. See ``packages/essapps/docs/batch.md``.

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
    source:
        Makes rows from files, for :meth:`scan`.
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
        source: RowSource | None = None,
        selector: Selector | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
        settings: Mapping[str, Any] | None = None,
        definitions: DefinitionStore | None = None,
    ) -> None:
        fields = spec.params.model_fields
        if unknown := sorted(set(columns) - fields.keys()):
            raise KeyError(f'{spec.title} has no parameters {unknown}')
        self.client = client
        self.spec = spec
        self.columns = tuple(columns)
        self.settings_fields = tuple(n for n in fields if n not in columns)
        self.label = label
        self.source = source
        self._definitions = definitions
        self._rows = RowTable()
        self._scanned = False
        self._settings = self._own(settings or {})
        if definitions is not None:
            self._load(definitions)
        self._selector = Selector() if selector is None else selector
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        # The ID of the record of each tweak, by key.
        self._tweaked: dict[str, str] = {}
        # The records under the label, read from the proposal's changes.
        self._cursor: int | None = None
        self._seen: set[str] = set()
        self._found: dict[str, Record] = {}
        self._unbound: set[str] = set()
        # A cell's value as a record holds it, by its column and value as given.
        self._resolved: dict[tuple[str, Any], Any] = {}

    def _load(self, definitions: DefinitionStore) -> None:
        """Take the edits and the settings that ``definitions`` hold, if any."""
        edits = definitions.load(definition.edits_key(self.label), definition.edits_of)
        if edits is not None:
            self._rows = edits
        settings = definitions.load(
            definition.settings_key(self.label),
            lambda data: definition.settings_of(data, self.spec),
        )
        if settings is not None:
            self._settings = settings

    @property
    def rows(self) -> RowTable:
        """
        The rows: those of the last scan, with what was set by hand on top. An
        assignment is written to the definitions, if the batch has them, and
        is not made if they cannot keep it.
        """
        return self._rows

    @rows.setter
    def rows(self, rows: RowTable) -> None:
        if self._definitions is not None:
            key = definition.edits_key(self.label)
            self._definitions.save(key, definition.edits_data(rows))
        self._rows = rows

    def put(self, rows: Iterable[Row]) -> None:
        """
        Set ``rows`` by hand, as a table of them gives them
        (:meth:`RowTable.put`). Raises ``ValueError`` if the batch has a source
        and has not scanned, since every row would then be added, and none
        would follow the files.
        """
        if self.source is not None and not self._scanned:
            raise ValueError('scan the files first: the rows are set on top of them')
        self.rows = self.rows.put(rows, self.columns)

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
        # A file that gained a catalogue identifier is resolved to it from now
        # on, by any of its names, its old identity among them.
        self._resolved.clear()
        return infos

    @property
    def settings(self) -> Mapping[str, Any]:
        """
        The settings in force: a value for each parameter that is not a
        column, where it is not left to its default. A copy, read-only at the
        top, so that only an assignment of all of them changes the batch::

            batch.settings = {**batch.settings, 'q': {'num_bins': 50}}

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

    def _cells(self, row: Row) -> dict[str, Any]:
        """The value of each column of a row, the default of an empty one."""
        fields = self.spec.params.model_fields
        return {
            c: row.values[c]
            if c in row.values
            else fields[c].get_default(call_default_factory=True)
            for c in self.columns
        }

    def state(self, keys: Sequence[str] | None = None) -> dict[str, RowState]:
        """
        Where each row stands now, read from the client, or the rows of
        ``keys`` if given: its result, and where its request with the settings
        differs from its result's (:attr:`RowState.changed`), with the reasons
        a cell is refused, as problems of its row.

        Raises ``SubmitError`` if the settings are refused, with reasons that
        name no row: no row can run with them.
        """
        rows, found = self._rows_and_results(keys)
        changed, refused = self._compare(rows, found)
        return self._states(rows, found, changed, refused)

    def results(self, keys: Sequence[str] | None = None) -> dict[str, RowState]:
        """
        Each row, or each of ``keys``, with its result, without its request:
        the problems of its cells as given, its status, and whether its result
        is of another spec. Nothing is resolved, so :attr:`RowState.changed`
        is ``None``, and so is :attr:`RowState.outdated` unless the result is
        of another spec: :func:`~.state.remaining` and
        :func:`~.state.arrived` refuse these states.

        It is what a page shows while the settings are refused, and what
        :meth:`reduce` checks before it submits. Which rows to reduce is
        chosen from :meth:`state`.
        """
        rows, found = self._rows_and_results(keys)
        return self._states(rows, found, None, {})

    def _rows_and_results(
        self, keys: Sequence[str] | None
    ) -> tuple[list[Row], dict[str, Record]]:
        """The rows of ``keys``, or all, and the result of each that has one."""
        self._poll()
        rows = list(self.rows) if keys is None else [self.rows[k] for k in keys]
        return rows, {r.key: self._found[r.key] for r in rows if r.key in self._found}

    def _states(
        self,
        rows: list[Row],
        found: Mapping[str, Record],
        changed: Mapping[str, frozenset[str]] | None,
        refused: Mapping[str, tuple[FieldError, ...]],
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
            {k for k, r in found.items() if self._tweaked.get(k) == r.id},
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
        self, rows: list[Row], found: Mapping[str, Record]
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

        A value resolved is kept by its column and value until the next scan,
        since a parameter resolves the same whatever the others are. A kept
        value differs from one resolved now only for a file that gained a
        catalogue identifier since, which keeps its old identity until the
        scan, as ``resolve`` allows. A list is kept by its elements; a value
        that cannot be a key, such as a dict, is resolved each time. A refusal
        is not kept, so that a file that arrives later is found.
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

    def reduce(self, keys: Sequence[str]) -> dict[str, Record]:
        """
        Submit the rows of ``keys`` with the settings, all or none; their
        records by key.

        Raises ``SubmitError`` if a row cannot run or is still running, naming
        it by key and the cell by column. A reason about the settings is named
        once, without a key.
        """
        states = self.results(keys)
        rows = [self.rows[key] for key in keys]
        refused = [e for k in keys for e in (*states[k].problems, *_running(states[k]))]
        if refused:
            raise SubmitError(refused)
        if not rows:
            return {}
        template = self.template()
        each = {row.key: self._cells(row) for row in rows}
        try:
            records = self._submit(template, each)
        except SubmitError as error:
            raise SubmitError(self._once(error.errors)) from None
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

    def tweak(self, key: str, params: Mapping[str, Any]) -> Record:
        """
        Submit one row with ``params``, all of its request's, which become its
        result. While it is the row's result, the row is outdated only where
        its cells differ from ``params``, not where the settings do;
        :attr:`RowState.changed` names both. This batch keeps that in memory:
        another ``Batch`` on the label, or this one after a restart, reads the
        tweak's result as outdated where the settings differ, as any other.
        Raises ``SubmitError`` if the row is still running.
        """
        if running := _running(self.results([key])[key]):
            raise SubmitError(running)
        request = Request(self.spec, params=params)
        record = self.client.submit(
            request, label=self.label, member=key, persist=self._persist
        )
        self._tweaked[key] = record.id
        return record

    def tweak_params(self, row: RowState) -> dict[str, Any]:
        """
        The params that a :meth:`tweak` of ``row`` starts from: its cells, and
        the other params of its result, or the settings if it has none or one
        of another spec, whose params are not this spec's.
        """
        record = None if row.spec_changed else row.record
        base = self.settings if record is None else record.request.params
        rest = {k: v for k, v in base.items() if k not in self.columns}
        return {**rest, **self.rows[row.key].values}

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

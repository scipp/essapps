# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: its template and its batch table, the request of each
row, reducing rows, and reading their results.

The state of a row is read from the records under the label, not from what
this object submitted. So every ``Batch`` of the label, and this one after a
restart, computes the same state, except for a file that gained a catalogue
identifier after a ``Batch`` cached it (below). :class:`LabelRecords` holds
the records read and follows ``client.changes``, so that the state of some
rows does not read every record under the label.

:meth:`Batch.state` compares a row with its result as a record holds both. It
resolves the settings once per call, and for a row with a result only the
cells that differ as given. A resolved cell is cached by column and value until
the next :meth:`Batch.set_table`: a parameter resolves the same whatever the
other parameters are. :meth:`Batch.files` uses the same cache.

How the value of a parameter for a row is found: guide
``packages/essbatch/docs/batch.md``, "The request of a row".
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pandas as pd
from pydantic_core import to_json

from ess.apps.client import Client, Stage
from ess.apps.errors import FieldError, NotCleared, SubmitError
from ess.apps.records import Record, SpecId, Status, Template
from ess.spec import FileRef, WorkflowSpec, field_types
from ess.spec.refs import iter_refs

from . import definition, state
from .definition import DefinitionStore
from .state import RowState
from .table import as_request, cell_of, data_of, row_names, typed

_NONE = object()


def _cache_key(name: str, value: Any) -> tuple[str, Any] | None:
    """
    The key of a resolved value in the cache, ``None`` if ``value`` is not
    hashable.

    The key holds the column, since a value resolves per parameter: ``True``
    is ``1`` at an ``int`` and rejected at a ``StrictInt``. It holds the type
    of the value and of each element, since ``1``, ``1.0`` and ``True`` are
    equal in Python.
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


def _files_in(params: Mapping[str, Any]) -> list[FileRef]:
    """The references to files in ``params``, at any depth."""
    return [ref for _, ref in iter_refs(params) if isinstance(ref, FileRef)]


def _running(row: RowState) -> list[FieldError]:
    if row.record_status is Status.PENDING:
        return [FieldError(row.key, (), 'still running')]
    return []


class LabelRecords:
    """
    The records under ``label``: the result of each member (:attr:`found`),
    and the IDs of the records of an unbound spec (:attr:`unbound`).

    Up to date after :meth:`poll`.
    """

    def __init__(self, client: Client, label: str) -> None:
        self.client = client
        self.label = label
        self.found: dict[str, Record] = {}
        self.unbound: set[str] = set()
        self._cursor: int | None = None
        self._seen: set[str] = set()

    def poll(self) -> None:
        """
        Read the changes of the proposal since the last call; the first call
        reads every record under the label.
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
            records = [r for r in self.known(new) if r.label == self.label]
        self._seen.update(r.id for r in records)
        self.found |= state.results(records)
        bound = self.client.bound(records) if records else []
        self.unbound.update(r.id for r, b in zip(records, bound, strict=True) if not b)

    def known(self, ids: list[str]) -> list[Record]:
        """
        The records of ``ids`` that the client knows, of any label.

        If one is unknown, the others are read one by one, so that the unknown
        one affects only the rows that reference it.
        """
        try:
            return self.client.records(ids=ids) if ids else []
        except KeyError:
            if len(ids) == 1:
                return []
            return [r for i in ids for r in self.known([i])]

    def status(self, records: list[Record]) -> dict[str, Status]:
        """The status of each of ``records``, by record ID."""
        statuses = self.client.status(records)
        return {r.id: s for r, s in zip(records, statuses, strict=True)}

    def cleared(self, records: list[Record]) -> dict[str, state.Cleared]:
        """
        Each of ``records`` whose outputs are cleared, by ID. Reads the records
        of the label of each, of any label, to tell whether it holds a newer
        result of the member.
        """
        flags = self.client.cleared(records)
        gone = [r for r, c in zip(records, flags, strict=True) if c]
        newest: dict[tuple[str | None, str | None], Record] = {}
        for label in {r.label for r in gone if r.label is not None}:
            found = state.results(self.client.records(label=label))
            newest |= {(label, member): r for member, r in found.items()}
        newer = {
            r.id: n
            for r in gone
            if (n := newest.get((r.label, r.member))) is not None and n.id != r.id
        }
        readable = self._readable(list(newer.values()))
        return {
            r.id: state.Cleared(r.label, r.id in newer and newer[r.id].id in readable)
            for r in gone
        }

    def _readable(self, records: list[Record]) -> set[str]:
        """The IDs of the completed ``records`` whose outputs are not cleared."""
        if not records:
            return set()
        status = self.client.status(records)
        cleared = self.client.cleared(records)
        return {
            r.id
            for r, s, c in zip(records, status, cleared, strict=True)
            if s is Status.COMPLETED and not c
        }


class Batch:
    """
    A template and a batch table: one spec reduced for many rows.

    Guide: ``packages/essbatch/docs/batch.md``.

    Parameters
    ----------
    client:
        The client that submits.
    spec:
        The spec of every row.
    label:
        The label of the records. The key of a row is the member of its
        records.
    settings:
        The value of each parameter for the batch, unless left to its default.
        Values at ``source_columns`` are dropped.
    table:
        The batch table, as :meth:`set_table` accepts it. If not given, a table
        without rows, with a column for each of ``source_columns``.
    staged:
        Run the rows through a stage, which computes what they share once. The
        binding of the spec must make stages.
    persist:
        As :meth:`~ess.apps.Client.submit` accepts it.
    definitions:
        Where each change of the settings and the batch table is written.
        Nothing is read from them: :meth:`.manager.BatchManager.batch` reads
        them. Without definitions, the batch stores nothing.
    source_columns:
        The parameters that the row source of the batch fills
        (:attr:`~.source.RowSource.source_columns`). An empty cell there makes
        the row incomplete, whatever the settings and the default.

    Attributes
    ----------
    parameter_columns:
        The parameter columns of the batch table, in its order.
    settings_fields:
        The parameters of the spec without a column.
    revision:
        Changes whenever :meth:`table` or :meth:`template` may return something
        else. It counts no record and no file: a record that changes is in
        ``client.changes``, and a file that arrives can change the state of a
        row without a change of revision.

    Raises
    ------
    ValueError
        If :meth:`set_table` would reject ``table``.
    """

    def __init__(
        self,
        client: Client,
        spec: WorkflowSpec[Any, Any],
        *,
        label: str,
        settings: Mapping[str, Any] | None = None,
        table: pd.DataFrame | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
        definitions: DefinitionStore | None = None,
        source_columns: Collection[str] = (),
    ) -> None:
        self.client = client
        self.spec = spec
        self.label = label
        self.source_columns = frozenset(source_columns)
        self.definitions = definitions
        self._revision = 0
        # A setting at a source column, as old definitions may hold, is never read.
        self._settings = copy.deepcopy(self.settable(settings or {}))
        self._template = Template(spec, params=self._settings)
        if table is None:
            table = pd.DataFrame(columns=sorted(self.source_columns))
        table = typed(table, spec.params)
        self._keeps_source_columns(table.columns)
        self._set(table)
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        self._records = LabelRecords(client, label)
        # A cell's value as a record holds it, by _cache_key; also a setting's,
        # for files(). Without it, each state resolves every cell of every row
        # without a result.
        self._resolved: dict[tuple[str, Any], Any] = {}

    def _set(self, table: pd.DataFrame) -> None:
        """Hold ``table``, typed, and make its parameter columns the blanks."""
        fields = self.spec.params.model_fields
        self._table = table
        blanks = [c for c in table.columns if c in fields]
        self._template = Template(self.spec, params=self._settings, blanks=blanks)
        self._revision += 1

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def parameter_columns(self) -> tuple[str, ...]:
        return self._template.blanks

    @property
    def settings_fields(self) -> tuple[str, ...]:
        fields = self.spec.params.model_fields
        return tuple(n for n in fields if n not in self.parameter_columns)

    def settable(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """
        ``settings`` without the values at :attr:`source_columns`.

        For settings from elsewhere, such as another batch, a record or a
        template file, which an assignment of :attr:`settings` would reject.
        """
        return {k: v for k, v in settings.items() if k not in self.source_columns}

    def _keeps_source_columns(self, columns: Collection[str]) -> None:
        """Raise ``ValueError`` if ``columns`` lack one of :attr:`source_columns`."""
        if lacking := [c for c in sorted(self.source_columns) if c not in columns]:
            raise ValueError(
                f'the source of the batch fills {", ".join(lacking)}: a row is '
                'incomplete until its cell there is filled, so the table must '
                'have the column'
            )

    def table(self) -> pd.DataFrame:
        """A deep copy of the batch table. Change the batch with :meth:`set_table`."""
        table = self._table.copy()
        for c in table.select_dtypes(object).columns:
            cells = copy.deepcopy(table[c].tolist())
            table[c] = pd.Series(cells, index=table.index, dtype=object)
        return table

    def set_table(self, table: pd.DataFrame) -> None:
        """
        Replace the batch table with ``table`` and write it to the definitions.

        The settings do not change. A table equal to the current one, dtypes
        and order included, writes nothing. The cache of resolved cells is
        cleared, so that a file that gained a catalogue identifier resolves to
        it.

        Raises ``ValueError`` if :func:`~.table.typed` rejects ``table`` or it
        lacks a column of :attr:`source_columns`. Raises
        :class:`~.definition.DefinitionWriteError` if the definitions reject the
        write. Nothing changes in either case.
        """
        table = typed(table, self.spec.params)
        self._keeps_source_columns(table.columns)
        self._resolved.clear()
        data = data_of(table)
        if to_json(data) == to_json(data_of(self._table)):
            return
        if self.definitions is not None:
            self.definitions.save(definition.TABLE.key(self.label), data)
        self._set(table)

    @property
    def settings(self) -> Mapping[str, Any]:
        """
        A read-only copy of the settings. Assign all of them to change one::

            batch.settings = {**batch.settings, 'q': {'num_bins': 50}}

        An assignment reduces no row. The workspace checks the settings when
        :meth:`state` resolves them. A setting at a column is checked as a
        cell of each row that uses it. A reference to a record that failed,
        was cancelled or is cleared, in a setting outside the columns, is a
        problem of the settings, once and without a key, that :meth:`shown`
        reports and that refuses :meth:`reduce`.

        An assignment raises ``ValueError``, and changes nothing, for a value
        at :attr:`source_columns`: use :meth:`settable` for settings from
        elsewhere.
        """
        return MappingProxyType(copy.deepcopy(self._settings))

    @settings.setter
    def settings(self, settings: Mapping[str, Any]) -> None:
        if at := sorted(settings.keys() & self.source_columns):
            raise ValueError(
                f'the source of the batch fills {", ".join(at)}: an empty cell '
                'there makes the row incomplete, so it takes no setting'
            )
        settings = copy.deepcopy(dict(settings))
        self._save(settings)
        self._settings = settings
        self._template = Template(
            self.spec, params=settings, blanks=self.parameter_columns
        )
        self._revision += 1

    def make_column(self, name: str) -> None:
        """
        Add a column ``name`` with an empty cell in every row: one
        :meth:`set_table`.

        The setting stays, so every row uses it until its cell is set. Raises
        ``ValueError`` if ``name`` is a column or not a parameter, and as
        :meth:`set_table` raises.
        """
        if name not in self.settings_fields:
            raise ValueError(f'{name} is not a setting of {self.spec.name}')
        self.set_table(self.table().assign(**{name: None}))

    def make_setting(self, name: str, key: str | None = None) -> None:
        """
        Drop the column ``name``, and make the value that the row ``key`` uses
        there the setting.

        Without ``key``, every row must use the same value (:meth:`values`).
        If the cell of the row is empty, the settings do not change. The
        settings are written before the batch table. If the definitions reject
        the batch table, the batch still has the column and the changed setting, as
        a batch read again would.

        Raises ``ValueError`` if ``name`` is not a column or is one of
        :attr:`source_columns`, if the table has no row ``key``, or if ``key``
        is not given and the rows use different values. Raises
        :class:`~.definition.DefinitionWriteError` if the definitions reject a
        write.
        """
        self._check_column(name)
        self._keeps_source_columns([c for c in self.parameter_columns if c != name])
        if key is None:
            values = self.values(name)
            if len(values) > 1:
                raise ValueError(f'the rows differ at {name}: choose the value to keep')
            key = next(iter(values), None)
        elif key not in self._table.index:
            raise ValueError(f'the batch has no row {key}')
        if key is not None:
            cell = self._table[[name]].loc[[key]].to_dict('index')[key][name]
            if cell is not None:
                value = as_request(self.spec.params, {name: cell})[name]
                self.settings = {**self._settings, name: value}
        self.set_table(self.table().drop(columns=name))

    def _check_column(self, name: str) -> None:
        """Raise ``ValueError`` unless ``name`` is a parameter column."""
        if name not in self.parameter_columns:
            raise ValueError(f'{name} is not a column of the batch')

    def values(self, name: str) -> dict[str, Any]:
        """
        The distinct values that the rows use at the column ``name``, each by
        the key of the first row that uses it.

        A value is a cell. For an empty cell it is the cell of what
        :meth:`empty` returns, or ``None`` if that has no value. Raises
        ``ValueError`` if ``name`` is not a column.
        """
        self._check_column(name)
        cells = {
            k: row[name] for k, row in self._table[[name]].to_dict('index').items()
        }
        if name in (empty := self.empty()):
            given = cell_of(empty[name])
            cells = {k: given if c is None else c for k, c in cells.items()}
        first: dict[bytes, str] = {}
        for key, cell in cells.items():
            first.setdefault(to_json(cell), key)
        return {key: cells[key] for key in first.values()}

    def _save(self, settings: Mapping[str, Any]) -> None:
        """Write ``settings``, with :attr:`source_columns` as the blanks."""
        if self.definitions is not None:
            kept = Template(
                self.spec, params=settings, blanks=sorted(self.source_columns)
            )
            definition.SETTINGS.save(self.definitions, self.label, kept)

    def template(self) -> Template:
        """
        A copy of the template that each row fills: the settings, with the
        parameter columns as blanks.
        """
        return copy.deepcopy(self._template)

    def empty(self) -> dict[str, Any]:
        """
        The value of an empty cell, by column, as a request accepts it: the
        setting, else the default, else ``None`` if the parameter accepts
        ``None``.

        A column without such a value is left out, and so is each of
        :attr:`source_columns`.
        """
        fields = self.spec.params.model_fields
        empty = {}
        for c in self.parameter_columns:
            if c in self.source_columns:
                continue
            if c in self._settings:
                empty[c] = copy.deepcopy(self._settings[c])
            elif not fields[c].is_required():
                empty[c] = fields[c].get_default(call_default_factory=True)
            elif field_types.optional(fields[c].annotation)[1]:
                empty[c] = None
        return empty

    def cells(self, keys: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        """
        The values of the parameter columns of the rows of ``keys``, or of all
        rows, by key, as a request accepts them.

        An empty cell has the value of :meth:`empty`, and is left out where
        that has none. Raises ``KeyError`` for a key that the table lacks.
        """
        rows = self._table[list(self.parameter_columns)]
        if keys is not None:
            rows = rows.loc[list(keys)]
        empty = self.empty()
        return {
            key: {
                **empty,
                **as_request(
                    self.spec.params, {c: v for c, v in row.items() if v is not None}
                ),
            }
            for key, row in rows.to_dict('index').items()
        }

    def files(self) -> set[FileRef]:
        """
        The files that the cells and the settings refer to, at any depth, each
        by its identity in the file source.

        Each value is resolved alone, so a rejected value, such as a file not
        listed, hides no other file. Each element of a rejected list is
        resolved alone too. In a group, a rejected file hides the other files
        of the group.
        """
        given = [
            *self._template.params.items(),
            *(item for cells in self.cells().values() for item in cells.items()),
        ]
        unique = {
            _cache_key(n, v) or object(): {n: v} for n, v in given if _files_in({n: v})
        }
        asked = {str(i): a for i, a in enumerate(unique.values())}
        resolved, refused = self._resolve_cells(asked)
        alone = [
            {n: [x]}
            for key in refused
            for n, v in asked[key].items()
            if isinstance(v, list)
            for x in v
        ]
        more, _ = self._resolve_cells({str(i): a for i, a in enumerate(alone)})
        named = [*resolved.values(), *more.values()]
        return {ref for cells in named for ref in _files_in(cells)}

    def state(
        self, keys: Sequence[str] | None = None, *, compare: bool = True
    ) -> dict[str, RowState]:
        """
        The state of each row, or of the rows of ``keys``, read from the client.

        A cell that the workspace rejects is a problem of its row. With
        ``compare=False``, nothing is resolved: :attr:`RowState.changed` is
        ``None``, and so is :attr:`RowState.outdated` unless the result is of
        another spec.

        Raises ``SubmitError``, with reasons that name no row, if the workspace
        rejects the settings. Never with ``compare=False``.
        """
        cells, found = self._cells_and_results(keys)
        if not compare:
            return self._states(cells, found, None, {})[0]
        changed, refused = self._compare(cells, found)
        return self._states(cells, found, changed, refused)[0]

    def shown(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The states of the rows of ``keys``, or of all rows, and the problems
        of the settings: why the workspace rejects them, and their references
        to records that failed, were cancelled or are cleared.

        While the workspace rejects the settings, the states are not compared.
        """
        cells, found = self._cells_and_results(keys)
        try:
            changed, refused = self._compare(cells, found)
        except SubmitError as error:
            states, problems = self._states(cells, found, None, {})
            return states, (*error.errors, *problems)
        return self._states(cells, found, changed, refused)

    def settings_of(self, key: str) -> dict[str, Any]:
        """
        The settings that the result of row ``key`` was reduced with.

        At a column where the cell of the row holds a value, the current
        setting of the batch. Elsewhere, the value in the request of the
        result.

        Raises ``ValueError`` naming the row if it has no result, or a result
        of another spec. Raises ``KeyError`` if the table has no row ``key``.
        """
        row = self.shown([key])[0][key]
        name = row_names(self._table, self.parameter_columns)[key]
        if row.record is None:
            raise ValueError(f'{name} has no result')
        if row.spec_changed:
            raise ValueError(
                f'{name} was reduced by another version of the workflow, '
                'whose settings this one does not take'
            )
        kept = {n: v for n, v in self.settings.items() if n in row.overrides}
        took = {
            n: v for n, v in row.record.request.params.items() if n not in row.overrides
        }
        return {**took, **kept}

    def _cells_and_results(
        self, keys: Sequence[str] | None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Record]]:
        """The cells of ``keys``, or of all, and the result of each that has one."""
        self._records.poll()
        cells = self.cells(keys)
        found = self._records.found
        return cells, {k: found[k] for k in cells if k in found}

    def _states(
        self,
        cells: Mapping[str, Mapping[str, Any]],
        found: Mapping[str, Record],
        changed: Mapping[str, frozenset[str]] | None,
        refused: Mapping[str, tuple[FieldError, ...]],
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The state of each row of ``cells``, and the problems of the settings
        outside the columns, which name no row (:func:`.state.problems`).
        """
        settings = {
            n: v for n, v in self._settings.items() if n not in self.parameter_columns
        }
        referenced = state.references([settings, *cells.values()])
        upstream = self._records.known(sorted(referenced))
        status = self._records.status([*found.values(), *upstream])
        cleared = self._records.cleared([*found.values(), *upstream])
        given = self._table.loc[list(cells), list(self.parameter_columns)].notna()
        overrides = {
            key: frozenset(c for c, filled in row.items() if filled)
            for key, row in given.to_dict('index').items()
        }
        states = state.row_states(
            cells,
            self.parameter_columns,
            found,
            status,
            changed,
            refused,
            self._spec_changed(found),
            cleared,
            overrides=overrides,
        )
        return states, state.problems(None, settings, (), status, cleared)

    def _spec_changed(self, found: Mapping[str, Record]) -> set[str]:
        """The keys whose result is of an unbound spec."""
        spec = SpecId.of(self.spec)
        return {
            key
            for key, r in found.items()
            if r.spec != spec or r.id in self._records.unbound
        }

    def _compare(
        self, cells: Mapping[str, Mapping[str, Any]], found: Mapping[str, Record]
    ) -> tuple[dict[str, frozenset[str]], dict[str, tuple[FieldError, ...]]]:
        """
        :attr:`RowState.changed` of each row with a result, and the problems
        of the rows whose cells are rejected.
        """
        resolved = self.client.resolve(self._template)
        if isinstance(resolved, SubmitError):
            raise resolved
        asked = {}
        for key, given in cells.items():
            if len(given) < len(self.parameter_columns):
                continue  # an empty cell is a problem, and nothing to resolve
            if (record := found.get(key)) is None:
                asked[key] = dict(given)
            else:
                was = record.request.params
                asked[key] = {
                    c: given[c]
                    for c in state.changed_names(given, was, self.parameter_columns)
                }
        cells_now, refused = self._resolve_cells(asked)
        changed = {}
        for key, record in found.items():
            if key in asked:
                now = {**resolved.params, **cells_now[key]}
                names = [*self.settings_fields, *cells_now[key]]
                changed[key] = frozenset(
                    state.changed_names(now, record.request.params, names)
                )
        return changed, refused

    def _resolve_cells(
        self, asked: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[FieldError, ...]]]:
        """
        The cells of each row of ``asked`` as a record holds them, and the
        problems of each row whose cells are rejected.

        The cells not cached are resolved in one call, each row in a template
        with every other parameter blank. A cached value differs from a fresh
        one only for a file that gained a catalogue identifier since, which
        ``resolve`` allows. A rejection is not cached, so that a file that
        arrives later is found.
        """
        fields = self.spec.params.model_fields
        cells: dict[str, dict[str, Any]] = {}
        new: dict[str, dict[str, Any]] = {}
        for key, given in asked.items():
            cells[key] = {}
            for name, value in given.items():
                kept = self._resolved.get(_cache_key(name, value), _NONE)
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
                if (at := _cache_key(name, value)) is not None:
                    self._resolved[at] = result.params[name]
        return cells, refused

    def reduce(self, keys: Sequence[str]) -> dict[str, Record]:
        """
        Submit the rows of ``keys``, all or none, and return their records by
        key.

        Raises ``SubmitError`` if a row cannot run or is still running, naming
        its key and column. A reason about the settings outside the columns,
        such as a reference to a cleared record, appears once, without a key.
        """
        cells, found = self._cells_and_results(keys)
        states, settings = self._states(cells, found, None, {})
        rows = [e for k in keys for e in (*states[k].problems, *_running(states[k]))]
        refused = [*settings, *rows]
        if refused:
            raise SubmitError(refused)
        if not cells:
            return {}
        try:
            records = self._submit(self._template, cells)
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
        opened = self._stage is None or self._stage[0] != template
        try:
            return self.client.submit(self._open(template), each=each, **options)
        except SubmitError as error:
            # A rejection at a position is of a row or of the settings. A stage
            # opened before, which may have done its expensive part, stays. One
            # opened for this submit has staged nothing and is released, so that
            # it holds no output its template references. A rejection without
            # a position is of the stage, which stopped or was released: it is
            # replaced and the rows are submitted once more.
            if any(e.loc for e in error.errors):
                if opened:
                    self.close()
                raise
        self.close()
        return self.client.submit(self._open(template), each=each, **options)

    def _open(self, template: Template) -> Stage:
        """The stage of ``template``: the open one if opened from an equal one."""
        if self._stage is None or self._stage[0] != template:
            stage = self.client.stage(template)
            self.close()
            self._stage = (template, stage)
        return self._stage[1]

    def _once(self, errors: tuple[FieldError, ...]) -> list[FieldError]:
        """``errors``, each that names no parameter column once and without a key."""
        placed = [
            e
            if e.loc and e.loc[0] in self.parameter_columns
            else replace(e, request_key=None)
            for e in errors
        ]
        return list(dict.fromkeys(placed))

    @property
    def stage(self) -> str | None:
        """The ID of the open stage, if any."""
        return None if self._stage is None else self._stage[1].id

    def clear(self, keys: Sequence[str] | None = None) -> None:
        """
        Clear the outputs of every record of the rows ``keys``, or of every
        member, also of members no longer rows, under the label
        (:meth:`~ess.apps.Client.clear`).

        Raises ``KeyError`` naming a key that is no row. Raises ``ValueError``,
        and clears nothing, if a record is running or another reads it, worded
        by :func:`clear_refusal`.
        """
        if keys is not None and (
            unknown := [k for k in keys if k not in self._table.index]
        ):
            raise KeyError(f'the batch has no row {", ".join(unknown)}')
        records = [
            r
            for r in self.client.records(label=self.label)
            if keys is None or r.member in keys
        ]
        stages = {} if self.stage is None else {self.stage: self.label}
        clear_records(self.client, records, self.definitions, stages)

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        """
        Cancel the running results of the members ``keys``, or of every member.

        A result is the newest record of a member under the label, whether or
        not the member is a row now.
        """
        self._records.poll()
        found = self._records.found
        chosen = [r for k, r in found.items() if keys is None or k in keys]
        status = self._records.status(chosen)
        self.client.cancel([r for r in chosen if status[r.id] is Status.PENDING])

    def close(self) -> None:
        """Release the open stage, if any. What was submitted through it still runs."""
        if self._stage is not None:
            self.client.release(self._stage[1])
            self._stage = None


def clear_records(
    client: Client,
    records: Sequence[Record],
    definitions: DefinitionStore | None,
    stages: Mapping[str, str],
) -> None:
    """
    Clear the outputs of ``records`` (:meth:`~ess.apps.Client.clear`).

    Raises ``ValueError``, and clears nothing, if the client refuses, worded
    by :func:`clear_refusal` with the titles in ``definitions`` and the label
    of the batch of each stage in ``stages``, by stage ID.
    """
    try:
        client.clear(records)
    except NotCleared as error:
        raise ValueError(clear_refusal(client, error, definitions, stages)) from None


def clear_refusal(
    client: Client,
    error: NotCleared,
    definitions: DefinitionStore | None,
    stages: Mapping[str, str],
) -> str:
    """
    The text of a refused clear, which names a record by its row and the title
    of its batch, from ``definitions``, or its label without them::

        row 1 of Find offset is still running
        row 1 of Find offset is read by row 2 of Reduce, which is still running
        row 1 of Find offset is read by the open stage of Reduce
        row 1 of Find offset is read by an accumulator

    ``stages`` holds the label of the batch of each open stage, by ID. A stage
    of another batch is ``an open stage``. A record without a member is ``a
    record of Reduce``, and one without a label ``a record of no batch``.
    """
    ids = [error.record] if error.held_by != 'record' else [error.record, error.reader]
    named = {r.id: r for r in client.records(ids=ids)}

    def title(label: str) -> str:
        header = (
            None if definitions is None else definition.HEADER.load(definitions, label)
        )
        return label if header is None else header.title

    def name(record: Record) -> str:
        if record.label is None:
            return 'a record of no batch'
        if record.member is None:
            return f'a record of {title(record.label)}'
        return f'row {record.member} of {title(record.label)}'

    held = name(named[error.record])
    if error.held_by in ('pending', 'running'):
        return f'{held} is still running'
    if error.held_by == 'record':
        return f'{held} is read by {name(named[error.reader])}, which is still running'
    if error.held_by == 'stage':
        label = stages.get(error.reader)
        stage = (
            'an open stage' if label is None else f'the open stage of {title(label)}'
        )
        return f'{held} is read by {stage}'
    return f'{held} is read by an accumulator'

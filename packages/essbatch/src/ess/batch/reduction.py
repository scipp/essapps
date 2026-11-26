# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: its template and its batch table, the request of each row,
reducing rows, and reading their results.

:class:`Batch` is the only part of the toolbox's logic that submits to the client.
It needs no interface of its own over the client: tests and UI work run it on
``local()`` with fake workflows, which start in milliseconds and refuse and fail
as real ones do.

The batch table is one frame (:mod:`~.table`), and only :meth:`Batch.put` changes
it. The settings are a value per parameter for the batch, also for one that is
a column. The value of a parameter for a row is its cell; if the cell is empty
or the parameter is no column, the setting; if the batch has none, the default
of the parameter. But in a batch made from a source, a column that the source
fills takes its value from the cell only: an empty cell there means that the
row waits for its file, and the row is incomplete, whatever the settings hold
(:attr:`Batch.fills`, :meth:`Batch.empty`). A template cannot fix a value and
leave it blank, so the template of the requests and of the stage holds the
settings without the columns, its blanks are the columns, and each row fills
every column::

    template = Template(spec, params=settings, blanks=columns)  # none at columns
    row = rows[columns].loc[[key]].to_dict('index')[key]      # each cell of its type
    cells = as_request(model, {c: v for c, v in row.items() if v is not None})
    for c in columns - cells.keys():                          # the empty cells
        if c in fills: continue                               # waits for its file
        elif c in settings: cells[c] = settings[c]
        elif has_default(c): cells[c] = default(c)
        elif takes_none(c): cells[c] = None
    template.fill(params=cells)                               # a blank left: incomplete

A row's state is read from the records, not from what this object submitted,
so every ``Batch`` on the label, and this one after a restart, sees the same,
but for how it resolved a file name, which it keeps until its next ``put``.
What a ``Batch`` keeps of them (:class:`LabelRecords`) can be rebuilt from the
records at any time: the newest record of each member under the label, the IDs
of the records under the label it has read, and which of them are not of the
bound spec (:meth:`~ess.apps.Client.bound`), all read from ``client.changes``,
so that the state of chosen rows does not read every record under the label.

The settings are the batch's own (:attr:`Batch.settings`), so each state is
read with them, and each reduction submits them. A state that resolves
nothing (``Batch.state(compare=False)``) is for showing the rows while the
settings are refused (:meth:`Batch.shown`), and for the checks before a
submission.

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
until the next ``put`` (:meth:`Batch._resolve_cells`). The files that a batch
names (:meth:`Batch.files`) are read from the same: each cell and each setting
that names a file is resolved alone, as a template of that one parameter.

A stage is opened lazily and kept with the template it was opened from, as
given, since the stage's own holds resolved values. A refusal with no position
is the stage's, one that stopped or was released, so the stage is replaced and
the rows submitted once more. A refusal at a position is the row's or the
settings', and leaves the stage, which may have done its expensive part,
alone.
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
from ess.apps.errors import FieldError, SubmitError
from ess.apps.records import Record, SpecId, Status, Template
from ess.spec import FileRef, WorkflowSpec, field_types
from ess.spec.refs import iter_refs

from . import definition, state
from .definition import DefinitionStore
from .state import RowState
from .table import as_request, cell_of, data_of, names, typed

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


def _files_in(params: Mapping[str, Any]) -> list[FileRef]:
    """The references to files in ``params``, at any depth."""
    return [ref for _, ref in iter_refs(params) if isinstance(ref, FileRef)]


def _running(row: RowState) -> list[FieldError]:
    if row.status is Status.PENDING:
        return [FieldError(row.key, (), 'still running')]
    return []


class LabelRecords:
    """
    The records under ``label``, read from the proposal's changes: the result
    of each member (:attr:`found`), and the IDs of those that are not of the
    bound spec (:attr:`unbound`). Up to date after :meth:`poll`.
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
            records = [r for r in self.known(new) if r.label == self.label]
        self._seen.update(r.id for r in records)
        self.found |= state.results(records)
        bound = self.client.bound(records) if records else []
        self.unbound.update(r.id for r, b in zip(records, bound, strict=True) if not b)

    def known(self, ids: list[str]) -> list[Record]:
        """
        The records of ``ids`` that the client knows, of any label. If one is
        unknown, the others are read one by one, so that it touches only the
        rows that reference it, whose submission it refuses.
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


class Batch:
    """
    A template and a batch table: one spec reduced for many rows. See
    ``packages/essbatch/docs/batch.md``.

    Parameters
    ----------
    client:
        The client that submits.
    spec:
        The spec of every row.
    label:
        The label of the records. A row's key is its member.
    settings:
        The value of each parameter for the batch, where it is not left to
        its default; none if not given. A value at a column is the value of
        each row whose cell is empty; one at :attr:`fills` is left out.
    rows:
        The batch table, as :meth:`put` takes it; if not given, one without
        rows, with a column for each of :attr:`fills`.
    staged:
        Run the rows through a stage, so that what they share is computed
        once. The spec's binding must make stages.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    definitions:
        Where the settings and the rows of the label are written at each
        change (:mod:`~.definition`). Nothing is read from them: the manager
        reads them and gives them as ``settings``, ``rows`` and ``fills``
        (:meth:`.manager.BatchManager.batch`). A ``Batch`` made in code
        without them keeps nothing.
    fills:
        The parameters that the source of the batch fills, if it was made
        from a row source (:attr:`~.source.RowSource.fills`); they are kept
        as the blanks of its settings. Each is a column, and an empty cell
        there means that the row waits for its file: the row is incomplete,
        whatever the settings hold and whatever the default of the parameter
        is.

    Attributes
    ----------
    parameter_columns:
        The parameter columns of the batch table, in its order: the blanks of the
        template.
    settings_fields:
        The other parameters of the spec, which no row gives itself.
    definitions:
        As given: where the entries of the label are kept, if anywhere.
    version:
        A number that moves whenever :meth:`frame` or :meth:`template` may
        give something else: while it is the same, they give the same. A
        :meth:`put` that writes, an assignment of :attr:`settings`,
        :meth:`make_column` and :meth:`make_setting` move it; a :meth:`put`
        of an equal table does not. It counts no record and no file: a
        record that changes is in ``client.changes``, and a file that
        arrives, or that gains a catalogue identifier, can change the state
        of a row without a signal.

    Raises
    ------
    ValueError
        If ``rows`` are refused, as :meth:`put` refuses them, also for a
        table without a column of :attr:`fills`, so that each of them is a
        column of every batch.
    """

    def __init__(
        self,
        client: Client,
        spec: WorkflowSpec[Any, Any],
        *,
        label: str,
        settings: Mapping[str, Any] | None = None,
        rows: pd.DataFrame | None = None,
        staged: bool = False,
        persist: bool | Sequence[str] = False,
        definitions: DefinitionStore | None = None,
        fills: Collection[str] = (),
    ) -> None:
        self.client = client
        self.spec = spec
        self.label = label
        self.fills = frozenset(fills)
        self.definitions = definitions
        self._version = 0
        # A setting at fills, as definitions may hold, is never read: dropped.
        self._settings = copy.deepcopy(self.settable(settings or {}))
        self._template = Template(spec, params=self._settings)
        if rows is None:
            rows = pd.DataFrame(columns=sorted(self.fills))
        rows = typed(rows, spec.params)
        self._keeps_fills(rows.columns)
        self._set(rows)
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        self._records = LabelRecords(client, label)
        # A cell's value as a record holds it, by its column and value as given;
        # also a setting's, for :meth:`files`.
        # A state of 1000 toy rows without results takes 21 ms with it, and
        # 198 ms without; with every row reduced, 23 ms either way, since a
        # row with a result resolves only the cells that differ.
        self._resolved: dict[tuple[str, Any], Any] = {}

    def _set(self, rows: pd.DataFrame) -> None:
        """
        Hold ``rows``, a typed table, with its parameter columns as the blanks
        of the template.
        """
        fields = self.spec.params.model_fields
        self._rows = rows
        blanks = [c for c in rows.columns if c in fields]
        self._template = Template(self.spec, params=self._settings, blanks=blanks)
        self._version += 1

    @property
    def version(self) -> int:
        return self._version

    @property
    def parameter_columns(self) -> tuple[str, ...]:
        return self._template.blanks

    @property
    def settings_fields(self) -> tuple[str, ...]:
        fields = self.spec.params.model_fields
        return tuple(n for n in fields if n not in self.parameter_columns)

    def settable(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """
        ``settings`` without the values at :attr:`fills`, which an assignment
        of :attr:`settings` refuses: to tidy settings taken from elsewhere,
        such as those of another batch, of a record or of a template file.
        """
        return {k: v for k, v in settings.items() if k not in self.fills}

    def _keeps_fills(self, columns: Collection[str]) -> None:
        """Raise ``ValueError`` if ``columns`` lack one of :attr:`fills`."""
        if lacking := [c for c in sorted(self.fills) if c not in columns]:
            raise ValueError(
                f'the source of the batch fills {", ".join(lacking)}: a row waits '
                'until its cell there is filled, so the table keeps the column'
            )

    def frame(self) -> pd.DataFrame:
        """
        A copy of the batch table: the key as index, a column per blank, and notes.
        A list or a group in a cell is copied too, so that changing it in place
        changes the batch only by :meth:`put`.
        """
        frame = self._rows.copy()
        for c in frame.select_dtypes(object).columns:
            cells = copy.deepcopy(frame[c].tolist())
            frame[c] = pd.Series(cells, index=frame.index, dtype=object)
        return frame

    def put(self, frame: pd.DataFrame) -> None:
        """
        Make ``frame`` the batch table. Its parameter columns become the blanks of
        the template. The settings stay: a parameter that becomes a column
        keeps its setting for the rows whose cell is empty, and a column
        dropped takes its setting, or its default if the batch has none. A
        table equal to the one held, dtypes and order included, writes
        nothing.

        A value that a cell resolved to is forgotten, so that a file that
        gained a catalogue identifier is resolved to it from now on.

        Raises ``ValueError``, and changes nothing, for a table that
        :func:`~.table.typed` refuses: a key that is not text, empty, or
        given twice, or a cell that is not of the kind of its column; and for
        a table that lacks a column of :attr:`fills`.

        Raises :class:`~.definition.NotKept` if the definitions refuse the
        table, and then nothing changes.
        """
        rows = typed(frame, self.spec.params)
        self._keeps_fills(rows.columns)
        self._resolved.clear()
        data = data_of(rows)
        if to_json(data) == to_json(data_of(self._rows)):
            return
        if self.definitions is not None:
            self.definitions.save(definition.ROWS.key(self.label), data)
        self._set(rows)

    @property
    def settings(self) -> Mapping[str, Any]:
        """
        The settings in force: a value for each parameter, where it is not
        left to its default, for the rows that give none themselves: at a
        parameter that is no column every row, at a column the rows whose
        cell is empty. A copy, read-only at the top, so that only an
        assignment of all of them changes the batch::

            batch.settings = {**batch.settings, 'q': {'num_bins': 50}}

        An assignment reduces no row: the rows reduced before that take a
        setting changed read ``earlier``, with the setting in
        :attr:`RowState.changed`. The workspace checks the settings when
        :meth:`state` resolves them, and refuses a name that is not a
        parameter; a setting at a column is checked as a cell of each row
        that takes it. Raises ``ValueError``, and changes nothing, for a
        value at :attr:`fills`, which no row takes: use :meth:`settable` for
        settings taken from elsewhere. Such a value that ``settings`` of the
        constructor holds is left out.
        """
        return MappingProxyType(copy.deepcopy(self._settings))

    @settings.setter
    def settings(self, settings: Mapping[str, Any]) -> None:
        if at := sorted(settings.keys() & self.fills):
            raise ValueError(
                f'the source of the batch fills {", ".join(at)}: an empty cell '
                'there waits for its file, so it takes no setting'
            )
        settings = copy.deepcopy(dict(settings))
        self._save(settings)
        self._settings = settings
        self._template = Template(
            self.spec, params=settings, blanks=self.parameter_columns
        )
        self._version += 1

    def make_column(self, name: str) -> None:
        """
        Make the parameter ``name`` a column with an empty cell in every row:
        one :meth:`put`. The setting stays, so every row takes it until its
        cell is set, also a row added later. Raises ``ValueError`` if ``name``
        is a column or no parameter, and as :meth:`put`.
        """
        if name not in self.settings_fields:
            raise ValueError(f'{name} is not a setting of {self.spec.name}')
        self.put(self.frame().assign(**{name: None}))

    def make_setting(self, name: str, key: str | None = None) -> None:
        """
        Make the column ``name`` a setting: drop the column, so that every row
        takes the value that the row of ``key`` takes, or, if not given, the
        value that every row takes (:meth:`values`). The setting becomes the
        cell of that row; if the cell is empty, the row takes the setting, or
        the default if the batch has none, and the settings stay. So a batch
        whose cells at ``name`` are all empty, or that has no rows, keeps its
        setting.

        The settings, if changed, are written before the batch table without
        the column, which is one :meth:`put`. If the definitions refuse the
        batch table, the batch keeps the column with the setting changed, as a
        batch read again does: its rows with an empty cell take the value
        chosen.

        Raises ``ValueError`` if ``name`` is not a column, or one of
        :attr:`fills`, as :meth:`put` refuses a table without it; if the
        table has no row of ``key``, or if ``key`` is not given and the rows
        take different values; :class:`~.definition.NotKept` if the
        definitions refuse a write.
        """
        self._check_column(name)
        self._keeps_fills([c for c in self.parameter_columns if c != name])
        if key is None:
            values = self.values(name)
            if len(values) > 1:
                raise ValueError(f'the rows differ at {name}: choose the value to keep')
            key = next(iter(values), None)
        elif key not in self._rows.index:
            raise ValueError(f'the batch has no row {key}')
        if key is not None:
            cell = self._rows[[name]].loc[[key]].to_dict('index')[key][name]
            if cell is not None:
                value = as_request(self.spec.params, {name: cell})[name]
                self.settings = {**self._settings, name: value}
        self.put(self.frame().drop(columns=name))

    def _check_column(self, name: str) -> None:
        """Raise ``ValueError`` unless ``name`` is a parameter column."""
        if name not in self.parameter_columns:
            raise ValueError(f'{name} is not a column of the batch')

    def values(self, name: str) -> dict[str, Any]:
        """
        The different values that the rows take at the column ``name``, each
        by the key of the first row that takes it, in the order of the table,
        as a cell: a row's cell, or, for an empty cell, what it gives
        (:meth:`empty`, :func:`~.table.cell_of`), or ``None`` if it gives
        nothing. Raises ``ValueError`` if ``name`` is not a column.
        """
        self._check_column(name)
        cells = {k: row[name] for k, row in self._rows[[name]].to_dict('index').items()}
        if name in (empty := self.empty()):
            given = cell_of(empty[name])
            cells = {k: given if c is None else c for k, c in cells.items()}
        first: dict[bytes, str] = {}
        for key, cell in cells.items():
            first.setdefault(to_json(cell), key)
        return {key: cells[key] for key in first.values()}

    def _save(self, settings: Mapping[str, Any]) -> None:
        """
        Write ``settings`` as the settings of the label, if kept, with
        :attr:`fills` as the blanks of the template.
        """
        if self.definitions is not None:
            kept = Template(self.spec, params=settings, blanks=sorted(self.fills))
            definition.SETTINGS.save(self.definitions, self.label, kept)

    def template(self) -> Template:
        """
        A copy of the template that each row fills: the settings without
        those at the columns, and the columns as its blanks. Changing it
        changes the batch only by :attr:`settings`.
        """
        return copy.deepcopy(self._template)

    def empty(self) -> dict[str, Any]:
        """
        The value that an empty cell gives, by column, as a request takes it:
        the setting, else its parameter's default, or ``None`` if it takes
        ``None``. Nothing at a column of :attr:`fills`, where the row waits
        for its file, whatever its default, nor where the parameter gives
        nothing.
        """
        fields = self.spec.params.model_fields
        empty = {}
        for c in self.parameter_columns:
            if c in self.fills:
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
        The cells of the rows of ``keys``, or of all, by key, as a request
        takes them (:func:`~.table.as_request`): each empty cell as
        :meth:`empty` gives it, and left out where it gives nothing, a problem
        of the row. Raises ``KeyError`` for a key that the table lacks.
        """
        rows = self._rows[list(self.parameter_columns)]
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
        The files that the batch names, each by its identity: in a cell of a
        row or in a setting, at any depth, such as an element of a list.

        A name is resolved by the workspace, so ``run:60339`` gives the
        identity under which the file source lists the file. Each value
        that names a file is resolved on its own, so that a value refused,
        such as one that names a file not listed, names no file and hides
        no other; the elements of a list refused are resolved each on its
        own, so that one not listed hides no other. In a group, such as a
        setting of several fields, a file refused hides the group's other
        files. A value resolved is kept as :meth:`state` keeps it, until
        the next :meth:`put`.
        """
        given = [
            *self._template.params.items(),
            *(item for cells in self.cells().values() for item in cells.items()),
        ]
        unique = {
            _kept(n, v) or object(): {n: v} for n, v in given if _files_in({n: v})
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
        Where each row stands now, read from the client, or the rows of
        ``keys`` if given: its result, and where its request with the settings
        differs from its result's (:attr:`RowState.changed`), with the reasons
        a cell is refused, as problems of its row.

        With ``compare=False``, nothing is resolved: each row has the problems
        of its cells as given, its status, and whether its result is of
        another spec. :attr:`RowState.changed` is then ``None``, and so is
        :attr:`RowState.outdated` unless the result is of another spec. It is
        what :meth:`reduce` checks before it submits.

        Raises ``SubmitError`` if the settings are refused, with reasons that
        name no row: no row can run with them. Only if compared.
        """
        cells, found = self._cells_and_results(keys)
        if not compare:
            return self._states(cells, found, None, {})
        changed, refused = self._compare(cells, found)
        return self._states(cells, found, changed, refused)

    def shown(
        self, keys: Sequence[str] | None = None
    ) -> tuple[dict[str, RowState], tuple[FieldError, ...]]:
        """
        The state of the rows of ``keys``, or of all, as a table shows it, and
        why the settings are refused: compared, or not compared while the
        settings are refused, with the reasons.
        """
        try:
            return self.state(keys), ()
        except SubmitError as error:
            return self.state(keys, compare=False), error.errors

    def settings_of(self, key: str) -> dict[str, Any]:
        """
        The settings that the result of row ``key`` was reduced with: at a
        column where the row gives its own value, the batch's setting, and
        where its cell is empty, the value that the row took. Raises
        ``ValueError`` naming the row (:func:`~.table.names`) if it has no
        result, or one of another spec, whose settings this one does not take;
        ``KeyError`` if the table has no row ``key``.
        """
        row = self.shown([key])[0][key]
        name = names(self._rows, self.parameter_columns)[key]
        if row.record is None:
            raise ValueError(f'{name} has no result')
        if row.spec_changed:
            raise ValueError(
                f'{name} was reduced by another version of the workflow, '
                'whose settings this one does not take'
            )
        kept = {n: v for n, v in self.settings.items() if n in row.own}
        took = {n: v for n, v in row.record.request.params.items() if n not in row.own}
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
    ) -> dict[str, RowState]:
        upstream = self._records.known(sorted(state.references(cells.values())))
        status = self._records.status([*found.values(), *upstream])
        given = self._rows.loc[list(cells), list(self.parameter_columns)].notna()
        own = {
            key: frozenset(c for c, filled in row.items() if filled)
            for key, row in given.to_dict('index').items()
        }
        return state.row_states(
            cells,
            self.parameter_columns,
            found,
            status,
            changed,
            refused,
            self._spec_changed(found),
            own=own,
        )

    def _spec_changed(self, found: Mapping[str, Record]) -> set[str]:
        """The keys whose result is of another spec than the bound one."""
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
        For each row with a result, the names at which its request with the
        settings differs from its result's; and the reasons of the rows whose
        cells are refused. Raises ``SubmitError`` if the settings are.
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
                    for c in state.differs(given, was, self.parameter_columns)
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

        A value resolved is kept by its column and value until the next
        :meth:`put`, since a parameter resolves the same whatever the others
        are. A kept value differs from one resolved now only for a file that
        gained a catalogue identifier since, which keeps its old identity until
        the ``put``, as ``resolve`` allows. A list is kept by its elements; a value
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
        cells, found = self._cells_and_results(keys)
        states = self._states(cells, found, None, {})
        refused = [e for k in keys for e in (*states[k].problems, *_running(states[k]))]
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
            e
            if e.loc and e.loc[0] in self.parameter_columns
            else replace(e, request_key=None)
            for e in errors
        ]
        return list(dict.fromkeys(placed))

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        """
        Cancel the records still running that are the newest under the label
        for their member, of the members ``keys`` if given, or of every member,
        whether or not it is a row now.
        """
        self._records.poll()
        found = self._records.found
        chosen = [r for k, r in found.items() if keys is None or k in keys]
        status = self._records.status(chosen)
        self.client.cancel([r for r in chosen if status[r.id] is Status.PENDING])

    def close(self) -> None:
        """Release the open stage, if any; what was submitted through it still runs."""
        if self._stage is not None:
            self.client.release(self._stage[1])
            self._stage = None

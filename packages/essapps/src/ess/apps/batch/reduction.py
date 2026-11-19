# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: its template and its table, the request of each row,
reducing rows, and reading their results.

:class:`Batch` is the only part of the toolbox's logic that calls the client.
It needs no interface of its own over the client: tests and UI work run it on
``local()`` with fake workflows, which start in milliseconds and refuse and fail
as real ones do.

The table is one frame (:mod:`~.table`), and only :meth:`Batch.put` changes
it. Its parameter columns are the blanks of the template, and every other
parameter is a setting. The request of a row is the template filled with the
row's cells::

    row = rows[columns].loc[[key]].to_dict('index')[key]   # each cell of its type
    cells = as_request(model, {c: v for c, v in row.items() if v is not None})
    for c in columns - cells.keys():                       # the empty cells
        if has_default(c): cells[c] = default(c)
        elif takes_none(c): cells[c] = None
    template.fill(params=cells)                            # a blank left: incomplete

A row's state is read from the records, not from what this object submitted,
so every ``Batch`` on the label, and this one after a restart, sees the same,
but for how it resolved a file name, which it keeps until its next ``put``.
What a ``Batch`` keeps of them can be rebuilt from the records at any time:
the newest record of each member under the label, the IDs of the records under
the label it has read, and which of them are not of the bound spec
(:meth:`~ess.apps.Client.bound`), all read from ``client.changes``, so that the
state of chosen rows does not read every record under the label.

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
until the next ``put`` (:meth:`Batch._resolve_cells`).

A stage is opened lazily and kept with the template it was opened from, as
given, since the stage's own holds resolved values. A refusal with no position
is the stage's, one that stopped or was released, so the stage is replaced and
the rows submitted once more. A refusal at a position is the row's or the
settings', and leaves the stage, which may have done its expensive part,
alone.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

import pandas as pd
from pydantic_core import to_json

from ess.spec import WorkflowSpec, field_types

from ..client import Client, Stage
from ..errors import FieldError, SubmitError
from ..records import Record, SpecId, Status, Template
from . import definition, state
from .definition import DefinitionStore
from .state import RowState
from .table import as_request, data_of, typed

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


class Batch:
    """
    A template and a table: one spec reduced for many rows. See
    ``packages/essapps/docs/batch.md``.

    Parameters
    ----------
    client:
        The client that submits.
    spec:
        The spec of every row.
    label:
        The label of the records. A row's key is its member.
    settings:
        The value of each parameter that is not a column, where it is not
        left to its default; none if not given. A value at a column is left
        out, as a template leaves out a value at a blank.
    rows:
        The table, as :meth:`put` takes it; a table without rows or columns
        if not given.
    staged:
        Run the rows through a stage, so that what they share is computed
        once. The spec's binding must make stages.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    definitions:
        Where the settings and the rows of the label are written at each
        change (:mod:`~.definition`). Nothing is read from them: the manager
        reads them and gives them as ``settings`` and ``rows``
        (:meth:`.manager.BatchManager.batch`). A ``Batch`` made in code
        without them keeps nothing.

    Attributes
    ----------
    columns:
        The parameter columns of the table, in its order: the blanks of the
        template.
    settings_fields:
        The other parameters of the spec: the settings.

    Raises
    ------
    ValueError
        If ``rows`` are refused, as :meth:`put` refuses them.
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
    ) -> None:
        self.client = client
        self.spec = spec
        self.label = label
        self._definitions = definitions
        self._template = Template(spec, params=copy.deepcopy(dict(settings or {})))
        self._set(typed(pd.DataFrame() if rows is None else rows, spec.params))
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

    def _set(self, rows: pd.DataFrame) -> None:
        """
        Hold ``rows``, a typed table, with its parameter columns as the blanks
        of the template, which drops the settings at these.
        """
        fields = self.spec.params.model_fields
        self._rows = rows
        blanks = [c for c in rows.columns if c in fields]
        self._template = Template(
            self.spec, params=self._template.params, blanks=blanks
        )

    @property
    def columns(self) -> tuple[str, ...]:
        return self._template.blanks

    @property
    def settings_fields(self) -> tuple[str, ...]:
        fields = self.spec.params.model_fields
        return tuple(n for n in fields if n not in self.columns)

    def frame(self) -> pd.DataFrame:
        """
        A copy of the table: the key as index, a column per blank, and notes.
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
        Make ``frame`` the table. Its parameter columns become the blanks of
        the template: a setting that becomes a column loses its value, and a
        column dropped is a setting with no value. A table equal to the one
        held, dtypes and order included, writes nothing.

        A value that a cell resolved to is forgotten, so that a file that
        gained a catalogue identifier is resolved to it from now on.

        Raises ``ValueError``, and changes nothing, for a table that
        :func:`~.table.typed` refuses: a key that is not text, empty, or
        given twice, or a cell that is not of the kind of its column.

        Raises :class:`~.definition.NotKept` if the definitions refuse the
        table, and then nothing changes. The table is written before the
        settings: if the definitions refuse the settings after the table, the
        table is changed and kept, and the settings kept are stale only in
        their blanks, which are not read, and at the values that the columns
        drop, which a batch made again from them drops too.
        """
        rows = typed(frame, self.spec.params)
        self._resolved.clear()
        data = data_of(rows)
        if to_json(data) == to_json(data_of(self._rows)):
            return
        before = self._template
        if self._definitions is not None:
            self._definitions.save(definition.rows_key(self.label), data)
        self._set(rows)
        if self._template != before:
            self._save(self._template)

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
        return MappingProxyType(copy.deepcopy(self._template.params))

    @settings.setter
    def settings(self, settings: Mapping[str, Any]) -> None:
        if at := sorted(settings.keys() & set(self.columns)):
            raise ValueError(f'{at}: columns of the batch, not settings')
        template = Template(
            self.spec, params=copy.deepcopy(dict(settings)), blanks=self.columns
        )
        self._save(template)
        self._template = template

    def _save(self, template: Template) -> None:
        """Write ``template`` as the settings of the label, if kept."""
        if self._definitions is not None:
            key = definition.settings_key(self.label)
            self._definitions.save(key, definition.template_data(template))

    def template(self) -> Template:
        """The template of the settings, with the columns as its blanks."""
        return self._template

    def _cells(self, keys: Sequence[str] | None) -> dict[str, dict[str, Any]]:
        """
        The cells of the rows of ``keys``, or of all, by key, as a request
        takes them: each empty cell its parameter's default, or ``None`` if it
        takes ``None``, and left out otherwise. Raises ``KeyError`` for a key
        that the table lacks.
        """
        rows = self._rows[list(self.columns)]
        if keys is not None:
            rows = rows.loc[list(keys)]
        fields = self.spec.params.model_fields
        empty = {}
        for c in self.columns:
            if not fields[c].is_required():
                empty[c] = fields[c].get_default(call_default_factory=True)
            elif field_types.optional(fields[c].annotation)[1]:
                empty[c] = None
        return {
            key: {
                **empty,
                **as_request(
                    self.spec.params, {c: v for c, v in row.items() if v is not None}
                ),
            }
            for key, row in rows.to_dict('index').items()
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
        cells, found = self._cells_and_results(keys)
        changed, refused = self._compare(cells, found)
        return self._states(cells, found, changed, refused)

    def results(self, keys: Sequence[str] | None = None) -> dict[str, RowState]:
        """
        Each row, or each of ``keys``, with its result, without its request:
        the problems of its cells as given, its status, and whether its result
        is of another spec. Nothing is resolved, so :attr:`RowState.changed`
        is ``None``, and so is :attr:`RowState.outdated` unless the result is
        of another spec: :func:`~.state.left_to_reduce` refuses these states.

        It is what a page shows while the settings are refused, and what
        :meth:`reduce` checks before it submits. Which rows to reduce is
        chosen from :meth:`state`.
        """
        return self._states(*self._cells_and_results(keys), None, {})

    def _cells_and_results(
        self, keys: Sequence[str] | None
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Record]]:
        """The cells of ``keys``, or of all, and the result of each that has one."""
        self._poll()
        cells = self._cells(keys)
        return cells, {k: self._found[k] for k in cells if k in self._found}

    def _states(
        self,
        cells: Mapping[str, Mapping[str, Any]],
        found: Mapping[str, Record],
        changed: Mapping[str, frozenset[str]] | None,
        refused: Mapping[str, tuple[FieldError, ...]],
    ) -> dict[str, RowState]:
        upstream = self._known(sorted(state.references(cells.values())))
        status = self._status([*found.values(), *upstream])
        return state.row_states(
            cells,
            self.columns,
            found,
            status,
            changed,
            refused,
            self._spec_changed(found),
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
        self, cells: Mapping[str, Mapping[str, Any]], found: Mapping[str, Record]
    ) -> tuple[dict[str, frozenset[str]], dict[str, tuple[FieldError, ...]]]:
        """
        For each row with a result, the names at which its request with the
        settings differs from its result's; and the reasons of the rows whose
        cells are refused. Raises ``SubmitError`` if the settings are.
        """
        resolved = self.client.resolve(self.template())
        if isinstance(resolved, SubmitError):
            raise resolved
        asked = {}
        for key, given in cells.items():
            if len(given) < len(self.columns):
                continue  # an empty cell is a problem, and nothing to resolve
            if (record := found.get(key)) is None:
                asked[key] = dict(given)
            else:
                was = record.request.params
                asked[key] = {
                    c: given[c] for c in state.differs(given, was, self.columns)
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
            records = self._submit(self.template(), cells)
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

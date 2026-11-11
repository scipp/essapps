# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch on a client: scanning for rows, reducing them, and reading their results.

:class:`Batch` is the only part of the toolbox's logic that calls the client.
It needs no interface of its own over the client: tests and UI work run it on
``local()`` with fake workflows, which start in milliseconds and refuse and fail
as real ones do.

A stage is opened lazily and kept with the template it was opened from, as
given, since the stage's own holds resolved values. A refusal with no position
is the stage's, one that stopped or was released, so the stage is replaced and
the rows submitted once more. A refusal at a position is the row's or the
settings', and leaves the stage, which may have done its expensive part,
alone.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ess.spec import FileRef, WorkflowSpec

from ..client import Client, Stage
from ..errors import FieldError, SubmitError
from ..files import Selector
from ..records import Record, Request, SpecId, Status, Template
from . import state
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

    Raises
    ------
    KeyError
        If a column is not a parameter of the spec.
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
        self.rows = RowTable()
        self._selector = Selector() if selector is None else selector
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        self._submitted: dict[str, Request] = {}
        self._tweaked: dict[str, Mapping[str, Any]] = {}

    def scan(self) -> None:
        """Make rows from the files now; what was set by hand stays."""
        if self.source is None:
            raise ValueError('this batch has no source of rows')
        infos = file_infos(self.client, self._selector)
        self.rows = self.rows.rescan(self.source(infos))

    def template(self, settings: Mapping[str, Any]) -> Template:
        """The template of ``settings``, with the columns as its blanks."""
        return Template(self.spec, params=settings, blanks=self.columns)

    def _cells(self, row: Row) -> dict[str, Any]:
        """The value of each column of a row, the default of an empty one."""
        fields = self.spec.params.model_fields
        return {
            c: row.values[c]
            if c in row.values
            else fields[c].get_default(call_default_factory=True)
            for c in self.columns
        }

    def state(self, settings: Mapping[str, Any] | None = None) -> dict[str, RowState]:
        """
        Where each row stands now, read from the client. With ``settings``,
        also which rows are stale. A result that came from :meth:`tweak` is
        stale only if the row's cells have changed since.
        """
        found = self._results()
        upstream = self._records_read()
        status = self._status([*found.values(), *upstream])
        stale = set() if settings is None else self._stale(settings)
        return state.row_states(
            self.rows,
            self.spec.params,
            self.columns,
            found,
            status,
            stale,
            self._tweaked.keys(),
        )

    def _results(self) -> dict[str, Record]:
        records = self.client.records(label=self.label)
        return state.results(records, SpecId.of(self.spec), self.client.submitter)

    def _records_read(self) -> list[Record]:
        """The records that cells reference, if the client knows them all."""
        ids = sorted(state.references(self.rows))
        try:
            return self.client.records(ids=ids) if ids else []
        except KeyError:  # the workspace refuses such a cell at submission
            return []

    def _status(self, records: list[Record]) -> dict[str, Status]:
        statuses = self.client.status(records)
        return {r.id: s for r, s in zip(records, statuses, strict=True)}

    def _stale(self, settings: Mapping[str, Any]) -> set[str]:
        template = self.template(settings)
        return {row.key for row in self.rows if self._differs(row, template)}

    def _differs(self, row: Row, template: Template) -> bool:
        if row.key in self._tweaked:
            return row.values != self._tweaked[row.key]
        return (
            row.key in self._submitted
            and not state.problems(row, self.spec.params, self.columns, {})
            and template.fill(params=self._cells(row)) != self._submitted[row.key]
        )

    def reduce(
        self, settings: Mapping[str, Any], keys: Sequence[str]
    ) -> dict[str, Record]:
        """
        Submit the rows of ``keys``, all or none; their records by key.

        Raises ``SubmitError`` if a row cannot run or is still running, naming
        it by key and the cell by column. A reason about the settings is named
        once, without a key.
        """
        states = self.state()
        rows = [self.rows[key] for key in keys]
        refused = [e for k in keys for e in (*states[k].problems, *_running(states[k]))]
        if refused:
            raise SubmitError(refused)
        if not rows:
            return {}
        template = self.template(settings)
        each = {row.key: self._cells(row) for row in rows}
        try:
            records = self._submit(template, each)
        except SubmitError as error:
            raise SubmitError(self._once(error.errors)) from None
        self._submitted |= {k: template.fill(params=c) for k, c in each.items()}
        for key in keys:
            self._tweaked.pop(key, None)
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
        result. It is not stale for differing from the settings or the cells,
        only if the cells change after this. Raises ``SubmitError`` if the row
        is still running.
        """
        if running := _running(self.state()[key]):
            raise SubmitError(running)
        request = Request(self.spec, params=params)
        record = self.client.submit(
            request, label=self.label, member=key, persist=self._persist
        )
        self._tweaked[key] = self.rows[key].values
        return record

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        """Cancel the results still running, of ``keys`` if given, or of any row."""
        chosen = [r for k, r in self._results().items() if keys is None or k in keys]
        status = self._status(chosen)
        self.client.cancel([r for r in chosen if status[r.id] is Status.PENDING])

    def close(self) -> None:
        """Release the open stage, if any; what was submitted through it still runs."""
        if self._stage is not None:
            self.client.release(self._stage[1])
            self._stage = None

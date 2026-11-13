"""
Spike of candidate B of ``.scratch/next-steps/one-interface.md``: one row state
for a person's batch and for the automatic reduction.

It is ``ess.apps.batch.reduction``, ``state``, and ``auto`` of the branch
``184-automatic-as-batch`` with these changes, each marked CHANGE below:

1. A row's result is the newest record under the label with the row's key, of
   any version of the spec. ``RowState.outdated`` says it is of another spec.
2. ``RowState.changed`` names the parameters at which the row's request now
   differs from its result's. It is read from the record, so ``Batch`` keeps no
   copy of what it submitted (``_submitted`` and ``_tweaked`` are gone).
3. A value of a row at a parameter that is not a column overrides the setting
   for that row. This replaces ``tweak``.
4. ``Batch`` reads its records from the proposal's changes, so the state of
   chosen rows does not list every record under the label. ``Automatic`` no
   longer collects which files the records hold.
5. Which rows run is a function from states to keys: ``remaining`` for a
   person, ``arrived(at)`` for the automatic reduction.

MISSING in ``ess.apps``: a call that resolves values without submitting them
(``Resolve`` below). The spike is given one made from ``ess.apps.checks.Checks``
and the file source, which a client does not hand out.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ess.apps import Client, Record, Request, Status, SubmitError, Template
from ess.apps.batch import Row, RowSource, RowTable, file_infos
from ess.apps.batch.state import references
from ess.apps.client import Stage
from ess.apps.errors import FieldError
from ess.apps.files import Selector
from ess.apps.records import SpecId
from ess.spec import OutputRef, WorkflowSpec
from ess.spec.refs import iter_refs

_RERUN = (Status.FAILED, Status.CANCELLED)
_INVALID: Any = object()

type Resolve = Callable[[Template], Mapping[str, Any]]
"""
MISSING: the fixed values of a template by name as a record would hold them
(file names resolved, defaults filled, JSON forms); ``SubmitError`` if a value
is invalid. The workspace has this as ``Checks.template``; no client call
reaches it.
"""

type Runs = Callable[[Mapping[str, 'RowState']], list[str]]
"""Which rows run: from the states of rows to the keys to submit."""


@dataclass(frozen=True)
class RowState:
    """
    Where one row stands: its result, the newest record under the label with
    the row's key, and how the row differs from it now.

    ``changed`` names the parameters whose value in the row's request differs
    from the result's request. ``outdated`` is true if the result is of another
    spec than the batch's (version or digest).
    """

    key: str
    record: Record | None = None
    status: Status | None = None
    problems: tuple[FieldError, ...] = ()
    changed: frozenset[str] = frozenset()
    outdated: bool = False

    @property
    def stale(self) -> bool:
        """As the views of the branch read it (``ess.apps.batch.web``)."""
        return bool(self.changed)

    tweaked = False  # the views of the branch read it; an override is an edit

    @property
    def text(self) -> str:
        if self.status is Status.PENDING:
            return 'pending'
        if self.problems:
            return 'incomplete'
        if self.status is None:
            return 'not reduced'
        if self.outdated:
            return 'outdated'
        return 'stale' if self.changed else str(self.status)


def ready(states: Mapping[str, RowState]) -> list[str]:
    """The keys of the rows that can be reduced now: complete and not running."""
    return [
        k
        for k, s in states.items()
        if not s.problems and s.status is not Status.PENDING
    ]


def remaining(states: Mapping[str, RowState]) -> list[str]:
    """A person's: no result, a failed or cancelled one, or one that differs."""
    return [
        k
        for k in ready(states)
        if states[k].status is None
        or states[k].status in _RERUN
        or states[k].changed
        or states[k].outdated
    ]


def arrived(at: str) -> Runs:
    """The automatic reduction's: no result, or one with another cell at ``at``."""

    def runs(states: Mapping[str, RowState]) -> list[str]:
        return [
            k for k in ready(states) if states[k].record is None or at in states[k].changed
        ]

    return runs


def _running(row: RowState) -> list[FieldError]:
    if row.status is Status.PENDING:
        return [FieldError(row.key, (), 'still running')]
    return []


class Batch:
    """One spec reduced for many rows; see ``ess.apps.batch.Batch``."""

    def __init__(
        self,
        client: Client,
        spec: WorkflowSpec[Any, Any],
        *,
        columns: Sequence[str],
        label: str,
        resolve: Resolve,
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
        self._resolve = resolve
        self._selector = Selector() if selector is None else selector
        self._staged = staged
        self._persist = persist
        self._stage: tuple[Template, Stage] | None = None
        # CHANGE 4: the join's right side, kept up to date from the changes.
        self._cursor = 0
        self._seen: set[str] = set()
        self._found: dict[str, Record] = {}
        # What only spares work: the resolved values of the rows that differ
        # from their results as given, and the resolved settings.
        self._cells_of: dict[str, tuple[Mapping[str, Any], list[str], Any]] = {}
        self._fixed_of: tuple[Any, Mapping[str, Any]] | None = None

    def scan(self) -> None:
        """Make rows from the files now; what was set by hand stays."""
        if self.source is None:
            raise ValueError('this batch has no source of rows')
        infos = file_infos(self.client, self._selector)
        self.rows = self.rows.rescan(self.source(infos))

    def _poll(self) -> None:
        """Read the records that changed since the last call."""
        changes = self.client.changes(after=self._cursor)
        self._cursor = changes.cursor
        for record in self.client.records(ids=changes.records):
            # A record comes first when submitted, so one seen before only
            # changed its status and is not newer than the one held.
            if (
                record.label == self.label
                and record.member is not None
                and record.id not in self._seen
            ):
                self._seen.add(record.id)
                self._found[record.member] = record

    def template(self, settings: Mapping[str, Any]) -> Template:
        return Template(self.spec, params=settings, blanks=self.columns)

    def _cells(self, row: Row) -> dict[str, Any]:
        fields = self.spec.params.model_fields
        return {
            c: row.values[c]
            if c in row.values
            else fields[c].get_default(call_default_factory=True)
            for c in self.columns
        }

    def _overrides(self, row: Row) -> dict[str, Any]:
        """CHANGE 3: the row's values at parameters that are not columns."""
        return {n: v for n, v in row.values.items() if n not in self.columns}

    def _problems(self, row: Row, status: Mapping[str, Status]) -> list[FieldError]:
        fields = self.spec.params.model_fields
        found = [
            FieldError(row.key, (name,), 'not a parameter')
            for name in row.values
            if name not in fields
        ]
        found += [
            FieldError(row.key, (c,), 'empty')
            for c in self.columns
            if c not in row.values and fields[c].is_required()
        ]
        found += [
            FieldError(row.key, loc, f'the record it reads is {status[ref.record]}')
            for loc, ref in iter_refs(row.values)
            if isinstance(ref, OutputRef) and status.get(ref.record) in _RERUN
        ]
        return found

    def _own(self, row: Row) -> dict[str, Any]:
        """The values a row gives its request: its cells and its overrides."""
        return {**self._cells(row), **self._overrides(row)}

    def _changed(self, row: Row, was: Mapping[str, Any]) -> frozenset[str]:
        """
        CHANGE 2: the names at which the row's own values differ from ``was``,
        the params of its result. Values are compared as given first, since a
        source gives files by identity, as a record holds them. Only a value
        that differs as given is resolved, and the result kept for the next
        call. A value that cannot be resolved counts as changed, and the
        submission says why it is refused.
        """
        given = self._own(row)
        differs = [n for n, v in given.items() if n not in was or was[n] != v]
        if not differs:
            return frozenset()
        cached = self._cells_of.get(row.key)
        if cached is None or cached[:2] != (row.values, differs):
            blanks = [n for n in self.spec.params.model_fields if n not in differs]
            try:
                now: Any = self._resolve(
                    Template(
                        self.spec, params={n: given[n] for n in differs}, blanks=blanks
                    )
                )
            except SubmitError:
                now = None
            cached = self._cells_of[row.key] = (row.values, differs, now)
        now = cached[2]
        if now is None:
            return frozenset(differs)
        return frozenset(n for n in differs if n not in was or was[n] != now[n])

    def _fixed(self, settings: Mapping[str, Any]) -> Mapping[str, Any]:
        """The settings as a record would hold them; raises ``SubmitError``."""
        if self._fixed_of is None or self._fixed_of[0] != settings:
            fixed = self._resolve(self.template(settings))
            self._fixed_of = (copy.deepcopy(settings), fixed)
        return self._fixed_of[1]

    def state(
        self,
        settings: Mapping[str, Any] | None = None,
        keys: Sequence[str] | None = None,
    ) -> dict[str, RowState]:
        """
        Where each row stands, or the rows of ``keys``. ``changed`` covers the
        row's own values, and with ``settings`` also the settings; every
        setting counts as changed if the settings are invalid.
        """
        self._poll()
        rows = list(self.rows) if keys is None else [self.rows[k] for k in keys]
        found = {r.key: self._found[r.key] for r in rows if r.key in self._found}
        upstream = self._records_read(rows)
        records = [*found.values(), *upstream]
        status = dict(
            zip((r.id for r in records), self.client.status(records), strict=True)
        )
        bound = dict(
            zip(found, self.client.bound(list(found.values())), strict=True)
        )
        try:
            fixed = {} if settings is None else self._fixed(settings)
        except SubmitError:
            # No result is of settings that cannot be submitted.
            fixed = dict.fromkeys(self.settings_fields, _INVALID)
        spec = SpecId.of(self.spec)
        states = {}
        for row in rows:
            record = found.get(row.key)
            problems = self._problems(row, status)
            changed: frozenset[str] = frozenset()
            if record is not None and not problems:
                was = record.request.params
                own = self._changed(row, was)
                rest = (
                    n
                    for n, v in fixed.items()
                    if n not in row.values and (n not in was or was[n] != v)
                )
                changed = own.union(rest)
            states[row.key] = RowState(
                key=row.key,
                record=record,
                status=None if record is None else status[record.id],
                problems=tuple(problems),
                changed=changed,
                # CHANGE 1: a result of another spec is still the row's result.
                outdated=record is not None
                and (record.spec != spec or not bound[row.key]),
            )
        return states

    def _records_read(self, rows: list[Row]) -> list[Record]:
        ids = sorted(references(rows))
        try:
            return self.client.records(ids=ids) if ids else []
        except KeyError:
            return []

    def reduce(
        self, settings: Mapping[str, Any], keys: Sequence[str]
    ) -> dict[str, Record]:
        """Submit the rows of ``keys``, all or none; their records by key."""
        states = self.state(keys=keys)
        refused = [e for k in keys for e in (*states[k].problems, *_running(states[k]))]
        if refused:
            raise SubmitError(refused)
        if not keys:
            return {}
        rows = [self.rows[key] for key in keys]
        template = self.template(settings)
        each = {r.key: self._cells(r) for r in rows if not self._overrides(r)}
        own = {
            r.key: Request(self.spec, params={**settings, **self._own(r)})
            for r in rows
            if self._overrides(r)
        }
        try:
            records = self._submit(template, each, own)
        except SubmitError as error:
            raise SubmitError(self._once(error.errors)) from None
        return {key: records[key] for key in keys}

    def _submit(
        self,
        template: Template,
        each: dict[str, dict[str, Any]],
        own: dict[str, Request],
    ) -> dict[str, Record]:
        options: dict[str, Any] = {'label': self.label, 'persist': self._persist}
        if not self._staged:
            requests = {k: template.fill(params=c) for k, c in each.items()}
            return self.client.submit(requests | own, **options)
        # Two calls, so not all or none across them: a limit of the spike.
        records = self.client.submit(own, **options) if own else {}
        if not each:
            return records
        try:
            return records | self.client.submit(
                self._open(template), each=each, **options
            )
        except SubmitError as error:
            if any(e.loc for e in error.errors):
                raise
        self.close()
        return records | self.client.submit(self._open(template), each=each, **options)

    def _open(self, template: Template) -> Stage:
        if self._stage is None or self._stage[0] != template:
            stage = self.client.stage(template)
            self.close()
            self._stage = (template, stage)
        return self._stage[1]

    def _once(self, errors: tuple[FieldError, ...]) -> list[FieldError]:
        placed = [
            e if e.loc and e.loc[0] in self.columns else replace(e, entry=None)
            for e in errors
        ]
        return list(dict.fromkeys(placed))

    def cancel(self, keys: Sequence[str] | None = None) -> None:
        self._poll()
        chosen = [
            r for k, r in self._found.items() if keys is None or k in set(keys)
        ]
        status = self.client.status(chosen)
        self.client.cancel(
            [r for r, s in zip(chosen, status, strict=True) if s is Status.PENDING]
        )

    def close(self) -> None:
        if self._stage is not None:
            self.client.release(self._stage[1])
            self._stage = None


class Automatic:
    """
    Reduces at each step the rows of ``batch`` that ``runs`` picks, among those
    whose cells or records changed. A refused row does not hold back the
    others, and :attr:`refused` says why.
    """

    def __init__(
        self, batch: Batch, *, runs: Runs, settings: Callable[[], Mapping[str, Any]]
    ) -> None:
        if batch.source is None:
            raise ValueError('this batch has no source of rows')
        self.batch = batch
        self.runs = runs
        self.settings = settings
        self.refused: dict[str, str] = {}
        self._cursor = 0
        self._rows: dict[str, Row] = {}
        self._settings: Mapping[str, Any] | None = None

    def step(self) -> dict[str, Record]:
        """
        Scan, and reduce the rows that ``runs`` picks among those whose cells
        or records changed since the last step that returned. A step that
        raises keeps nothing, so the next step looks at the same rows.
        """
        batch = self.batch
        changes = batch.client.changes(after=self._cursor)
        touched = {
            record.member
            for record in batch.client.records(ids=changes.records)
            if record.label == batch.label
        }
        settings = self.settings()
        batch.scan()
        rows = {row.key: row for row in batch.rows}
        look_at = {k for k, row in rows.items() if self._rows.get(k) != row}
        look_at |= touched & rows.keys()
        if settings != self._settings:
            look_at |= self.refused.keys() & rows.keys()
        keys = [k for k in rows if k in look_at]  # in the order of the rows
        kept = {
            k: why for k, why in self.refused.items() if k in rows and k not in look_at
        }
        # CHANGE 5: the same state as a person's batch, and a function of it.
        todo = self.runs(batch.state(keys=keys))
        self.refused, was = kept, self.refused
        try:
            records = self._reduce(settings, todo)
        except BaseException:
            self.refused = was
            raise
        # A copy, so that settings changed in place count as changed.
        self._cursor, self._rows = changes.cursor, rows
        self._settings = copy.deepcopy(settings)
        return records

    def _reduce(
        self, settings: Mapping[str, Any], keys: list[str]
    ) -> dict[str, Record]:
        while keys:
            try:
                return self.batch.reduce(settings, keys)
            except SubmitError as error:  # noqa: PERF203
                named = set(keys).intersection(e.entry for e in error.errors)
                if not named:
                    self.refused |= dict.fromkeys(keys, str(error))
                    return {}
                for key in named:
                    errors = [e for e in error.errors if e.entry == key]
                    self.refused[key] = str(SubmitError(errors))
                keys = [k for k in keys if k not in named]
        return {}


def run(autos: Sequence[Automatic], interval: float = 1.0) -> None:
    while True:
        for auto in autos:
            auto.step()
        time.sleep(interval)

# ruff: noqa: T201
"""
Prototype for #222: one table of rows, and a chain of workflows over it.

A *step* is one workflow of the chain. It maps each parameter to a column of
the table (``takes``) or keeps it as a setting, and adds one *output column*
per output, named ``<step>.<output>``. An output cell holds a reference to the
step's *current* record for the row, or nothing:

    current = the newest record of the step for the row (label '<table>/<step>',
              member: the row's key, or the value of the column ``per``),
              not failed or cancelled, of the bound spec,
              whose request equals the row's request now at every taken
              parameter; with ``settings_count=True`` also at every setting.

A step whose input cell is empty waits; its status names the column. A cell
set by hand is a cell set by hand, at an input or an output column. A step
with ``accept=True`` fills no output cell itself: only a person does ("Use").

Built beside ``Batch`` on the public API; reuses ``RowTable`` and the plain-data
functions of ``definition``. Not staged, no caching of resolved values, no
scope (the source is the scope).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ess.apps import Client
from ess.apps.batch import FileInfo, Row
from ess.apps.batch import definition as d
from ess.apps.batch.reduction import file_infos
from ess.apps.batch.table import RowTable
from ess.apps.errors import SubmitError
from ess.apps.records import Record, Request, SpecId, Status, Template
from ess.spec import FileRef, OutputRef, WorkflowSpec

RERUN = (Status.FAILED, Status.CANCELLED)


@dataclass
class Step:
    id: str
    spec: WorkflowSpec[Any, Any]
    takes: dict[str, str]  # parameter -> column
    settings: dict[str, Any]
    accept: bool = False
    per: str | None = None

    @property
    def outputs(self) -> dict[str, str]:
        """Output column -> output name."""
        return {f'{self.id}.{o}': o for o in self.spec.outputs.model_fields}

    def column_of(self, param: str) -> str:
        return self.takes[param]


@dataclass
class Cell:
    """Where one row stands at one step."""

    record: Record | None = None
    status: Status | None = None
    waits: tuple[str, ...] = ()  # empty output columns of earlier steps
    problems: tuple[str, ...] = ()  # empty or refused input columns
    changed: frozenset[str] = frozenset()  # input columns that differ
    earlier: frozenset[str] = frozenset()  # settings that differ
    spec_changed: bool = False
    current: bool = False
    outputs: dict[str, Any] = field(default_factory=dict)  # output column -> value
    used_other: bool = False  # accept: the cell holds another record than the newest

    @property
    def ready(self) -> bool:
        return (
            not self.waits and not self.problems and self.status is not Status.PENDING
        )

    def text(self, settings_count: bool) -> str:
        if self.status is Status.PENDING:
            t = 'pending'
        elif self.waits:
            return 'waits for ' + ', '.join(self.waits)
        elif self.problems:
            return 'incomplete: ' + '; '.join(self.problems)
        elif self.status is None:
            return 'not reduced'
        elif self.status in RERUN:
            return str(self.status)
        elif self.changed or self.spec_changed:
            t = 'outdated'
        elif self.earlier:
            t = 'outdated' if settings_count else 'completed, earlier settings'
        else:
            t = 'completed'
        if self.used_other:
            t += ', another used'
        elif self.record is not None and not any(
            v is not None for v in self.outputs.values()
        ):
            t += ', not used' if self.current else ''
        return t


def _member(value: Any) -> str:
    return value.file if isinstance(value, FileRef) else json.dumps(value)


class Table:
    """
    One table per batch: a source, edits, and a chain of steps.

    ``fills`` names the columns that the source fills; Follow reduces a row
    again for a change at these (``at`` of today's ``Automatic``) and at any
    output column.
    """

    def __init__(
        self,
        client: Client,
        label: str,
        *,
        source: Callable[[Sequence[FileInfo]], Sequence[Row]] | None = None,
        fills: Sequence[str] = (),
        settings_count: bool = False,
    ) -> None:
        self.client = client
        self.label = label
        self.source = source
        self.fills = tuple(fills)
        self.settings_count = settings_count
        self.steps: list[Step] = []
        self.rows = RowTable()

    # -- the chain ---------------------------------------------------------

    @property
    def inputs(self) -> list[str]:
        """Input columns: those the source fills, then those steps added."""
        cols = list(self.fills)
        for s in self.steps:
            cols += [c for c in s.takes.values() if c not in cols and '.' not in c]
        return cols

    @property
    def outputs(self) -> dict[str, Step]:
        return {c: s for s in self.steps for c in s.outputs}

    @property
    def columns(self) -> list[str]:
        return [*self.inputs, *self.outputs]

    def add(
        self,
        spec: WorkflowSpec[Any, Any],
        *,
        id: str | None = None,
        takes: Mapping[str, str] | None = None,
        settings: Mapping[str, Any] | None = None,
        accept: bool = False,
        per: str | None = None,
    ) -> list[str]:
        """
        Add a step at the end of the chain; returns the input columns it added.

        A parameter takes the column ``takes`` names, a new one if the table
        has none of that name; else the column of its name if the table has
        one and ``settings`` do not name it; else it is a setting, given in
        ``settings`` or left to its default, or refused at each row until set
        if it has none.

        (The rule first tried made every required parameter without a setting
        a new column; for LoKI's I(Q) that is six columns that are settings
        by nature. See ``loki.py``.)
        """
        id = id or spec.name
        takes, settings = dict(takes or {}), dict(settings or {})
        fields = spec.params.model_fields
        if unknown := sorted((takes.keys() | settings.keys()) - fields.keys()):
            raise KeyError(f'{spec.name} has no parameters {unknown}')
        if both := sorted(takes.keys() & settings.keys()):
            raise ValueError(f'{both}: both taken and set')
        if id in {s.id for s in self.steps}:
            raise ValueError(f'a step {id!r} exists')
        if bad := sorted(
            c for c in takes.values() if '.' in c and c not in self.outputs
        ):
            raise KeyError(f'{bad} are not outputs of earlier steps')
        existing = set(self.columns)
        for name in fields:
            if name in takes or name in settings:
                continue
            if name in existing:
                takes[name] = name
        if per is not None and set(takes.values()) != {per}:
            raise ValueError(f'a step per {per!r} takes that column only')
        before = set(self.inputs)
        self.steps.append(Step(id, spec, takes, settings, accept, per))
        return [c for c in self.inputs if c not in before]

    def remove(self, id: str) -> list[str]:
        """
        Remove a step; refused while another takes its output. Returns the
        input columns that no step takes any more; edits at them are dropped.
        """
        step = next(s for s in self.steps if s.id == id)
        if users := [
            s.id for s in self.steps if set(s.takes.values()) & set(step.outputs)
        ]:
            raise ValueError(f'{users} take the output of {id!r}')
        before = set(self.columns)
        self.steps.remove(step)
        gone = sorted(before - set(self.columns))
        edited = [k for k, cells in self.rows.edits.items() if set(cells) & set(gone)]
        for key in edited:
            self.rows = self.rows.reset(key, *gone)
        return gone

    def step(self, id: str) -> Step:
        return next(s for s in self.steps if s.id == id)

    def label_of(self, step: Step) -> str:
        return f'{self.label}/{step.id}'

    # -- rows ----------------------------------------------------------------

    def scan(self) -> None:
        if self.source is not None:
            self.rows = self.rows.rescan(self.source(file_infos(self.client)))

    def edit(self, key: str, cells: Mapping[str, Any]) -> None:
        if unknown := sorted(set(cells) - set(self.columns)):
            raise KeyError(f'{unknown} are not columns')
        self.rows = self.rows.edit(key, cells)

    def use(self, key: str, column: str, record: Record) -> None:
        """'Use this one': set an output cell by hand to a completed record."""
        step = self.outputs[column]
        if SpecId.of(step.spec) != record.spec:
            raise ValueError(f'{record.id} is not a record of {step.id}')
        if self.client.status([record])[0] is not Status.COMPLETED:
            raise ValueError(f'{record.id} is not completed')
        self.edit(key, {column: record.ref(step.outputs[column])})

    def set_by_hand(self, key: str) -> frozenset[str]:
        if key in {r.key for r in self.rows.added}:
            return frozenset(self.inputs) | frozenset(self.rows.edits.get(key, {}))
        return frozenset(self.rows.edits.get(key, {}))

    # -- state ---------------------------------------------------------------

    def _newest(self, step: Step) -> dict[str, Record]:
        return {
            r.member: r
            for r in self.client.records(label=self.label_of(step))
            if r.member is not None
        }

    def _member(self, step: Step, row_key: str, cells: Mapping[str, Any]) -> str | None:
        if step.per is None:
            return row_key
        return _member(cells[step.per]) if step.per in cells else None

    def _step_state(
        self, step: Step, keys: Sequence[str], cells: Mapping[str, dict[str, Any]]
    ) -> dict[str, Cell]:
        """Where each row stands at ``step``, given each row's cells so far."""
        fields = step.spec.params.model_fields
        newest = self._newest(step)
        found = {k: newest.get(self._member(step, k, cells[k]) or '') for k in keys}
        refs = {
            v.record
            for k in keys
            for c in step.takes.values()
            if isinstance(v := cells[k].get(c), OutputRef)
        }
        known = [r for r in found.values() if r] + self.client.records(ids=sorted(refs))
        status = dict(
            zip([r.id for r in known], self.client.status(known), strict=True)
        )
        out: dict[str, Cell] = {}
        asked: dict[str, Request] = {}
        for k in keys:
            given = {p: cells[k][c] for p, c in step.takes.items() if c in cells[k]}
            missing = [
                p for p in step.takes if p not in given and fields[p].is_required()
            ]
            waits = tuple(step.takes[p] for p in missing if '.' in step.takes[p])
            problems = [
                f'{step.takes[p]} empty' for p in missing if '.' not in step.takes[p]
            ]
            problems += [
                f'{step.takes[p]} reads a {status[v.record]} record'
                for p, v in given.items()
                if isinstance(v, OutputRef) and status.get(v.record) in RERUN
            ]
            rec = found[k]
            out[k] = Cell(
                record=rec,
                status=None if rec is None else status[rec.id],
                waits=waits,
                problems=tuple(problems),
                spec_changed=rec is not None and rec.spec != SpecId.of(step.spec),
            )
            if not missing and not problems:
                asked[k] = Request(step.spec, params={**step.settings, **given})
        resolved = self.client.resolve(asked) if asked else {}
        for k, now in resolved.items():
            c = out[k]
            if isinstance(now, SubmitError):
                # the one place where a parameter's position becomes a column's
                c.problems += tuple(
                    f'{step.takes.get(e.loc[0], e.loc[0]) if e.loc else ""}: {e.msg}'
                    for e in now.errors
                )
                continue
            if c.record is None:
                continue
            was = c.record.request.params
            diff = {
                n
                for n in fields
                if n not in now.params or n not in was or now.params[n] != was[n]
            }
            c.changed = frozenset(step.takes[p] for p in diff if p in step.takes)
            c.earlier = frozenset(p for p in diff if p not in step.takes)
        for k, c in out.items():
            c.current = (
                c.record is not None
                and not c.problems
                and not c.waits
                and c.status not in RERUN
                and not c.spec_changed
                and not c.changed
                and not (self.settings_count and c.earlier)
            )
            edits = self.rows[k].values if k in self.rows else {}
            hand = self.set_by_hand(k)
            for col, name in step.outputs.items():
                if col in hand and col in edits:
                    c.outputs[col] = edits[col]
                    c.used_other = (
                        c.record is not None
                        and isinstance(edits[col], OutputRef)
                        and edits[col].record != c.record.id
                    )
                elif c.current and not step.accept:
                    c.outputs[col] = c.record.ref(name)
                else:
                    c.outputs[col] = None
        return out

    def _walk(
        self,
        keys: Sequence[str] | None,
        choose: Callable[[Step, dict[str, Cell]], Collection[str]] | None,
        upto: str | None = None,
    ) -> tuple[dict[str, dict[str, Cell]], dict[str, dict[str, Record]]]:
        """
        Walk the chain in order: the state at each step, and, with ``choose``,
        submit the rows it chooses there before the next step reads their
        output cells (a reference to a pending record fills a cell).
        """
        keys = [r.key for r in self.rows] if keys is None else list(keys)
        cells = {
            k: {c: v for c, v in self.rows[k].values.items() if c in self.inputs}
            for k in keys
        }
        states: dict[str, dict[str, Cell]] = {}
        submitted: dict[str, dict[str, Record]] = {}
        for step in self.steps:
            st = self._step_state(step, keys, cells)
            if choose is not None and (upto is None or self._before(step.id, upto)):
                if chosen := list(choose(step, st)):
                    submitted[step.id] = self._submit(step, chosen, cells)
                    st = self._step_state(step, keys, cells)
            states[step.id] = st
            for k, c in st.items():
                cells[k].update(
                    {col: v for col, v in c.outputs.items() if v is not None}
                )
        return states, submitted

    def _before(self, id: str, upto: str) -> bool:
        order = [s.id for s in self.steps]
        return order.index(id) <= order.index(upto)

    def _submit(
        self, step: Step, keys: Sequence[str], cells: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, Record]:
        requests = {}
        for k in keys:
            given = {p: cells[k][c] for p, c in step.takes.items() if c in cells[k]}
            requests[self._member(step, k, cells[k])] = Request(
                step.spec, params={**step.settings, **given}
            )
        # A record read by a cell may fail between the state and the
        # submission; as Automatic does, drop the requests named and go on.
        while requests:
            try:
                return self.client.submit(requests, label=self.label_of(step))
            except SubmitError as error:  # noqa: PERF203
                named = {e.entry for e in error.errors} & requests.keys()
                if not named:
                    raise
                requests = {m: r for m, r in requests.items() if m not in named}
        return {}

    def state(self, keys: Sequence[str] | None = None) -> dict[str, dict[str, Cell]]:
        return self._walk(keys, None)[0]

    def changed_by_hand(self, states: Mapping[str, Mapping[str, Cell]]) -> set[str]:
        """
        Rows with a cell set by hand that a step taking it has not reduced yet:
        no record, waiting, or a record that differs at that column.
        """
        rows = set()
        for step in self.steps:
            for k, c in states[step.id].items():
                hand = self.set_by_hand(k) & set(step.takes.values())
                if hand and (
                    c.record is None or c.waits or c.problems or hand & c.changed
                ):
                    rows.add(k)
        return rows

    # -- actions -------------------------------------------------------------

    def reduce(
        self,
        keys: Sequence[str] | None = None,
        *,
        upto: str | None = None,
        again: Collection[str] = (),
    ) -> dict[str, list[str]]:
        """
        A person's "Reduce": at each step in order, up to ``upto``, the rows of
        ``keys`` whose output is not current and that can run. At the steps of
        ``again``, also those that differ only at settings ("Reduce them again").
        """

        def choose(step: Step, st: dict[str, Cell]) -> list[str]:
            return [
                k
                for k, c in st.items()
                if c.ready
                and (
                    c.record is None
                    or c.status in RERUN
                    or c.changed
                    or c.spec_changed
                    or (c.earlier and (self.settings_count or step.id in again))
                )
            ]

        return _keys(self._walk(keys, choose, upto)[1])

    def follow(self) -> dict[str, list[str]]:
        """
        One step of Follow: scan, then at each step in order the rows that are
        not changed by hand and that have no record there, or one that differs
        at a column the source fills or at an output column.
        """
        self.scan()
        hand = self.changed_by_hand(self.state())
        at = set(self.fills) | set(self.outputs)

        def choose(step: Step, st: dict[str, Cell]) -> list[str]:
            return [
                k
                for k, c in st.items()
                if c.ready and k not in hand and (c.record is None or c.changed & at)
            ]

        return _keys(self._walk(None, choose)[1])

    def wait(self) -> None:
        recs = [
            r for s in self.steps for r in self.client.records(label=self.label_of(s))
        ]
        self.client.wait(recs)

    # -- showing -------------------------------------------------------------

    def show(self, title: str, keys: Sequence[str] | None = None) -> None:
        states = self.state(keys)
        hand = self.changed_by_hand(states)
        print(f'-- {title}')
        for k in [r.key for r in self.rows] if keys is None else keys:
            parts = [
                f'{s.id}: {states[s.id][k].text(self.settings_count)}'
                for s in self.steps
            ]
            mark = '  [changed by hand]' if k in hand else ''
            print(f'   {k:6} ' + ' | '.join(parts) + mark)

    # -- definitions -----------------------------------------------------------

    def definitions(self) -> dict[str, Any]:
        """What a restart needs, as the files would hold it."""
        return {
            'chain.json': [
                {
                    'id': s.id,
                    'takes': s.takes,
                    'accept': s.accept,
                    'per': s.per,
                    'template': d.settings_data(
                        Template(s.spec, params=s.settings, blanks=tuple(s.takes))
                    ),
                }
                for s in self.steps
            ],
            'edits.json': d.edits_data(self.rows),
        }

    @classmethod
    def restore(
        cls,
        client: Client,
        label: str,
        files: Mapping[str, Any],
        *,
        source: Callable[[Sequence[FileInfo]], Sequence[Row]] | None,
        fills: Sequence[str],
    ) -> Table:
        """A table made again from :meth:`definitions` and the records."""
        table = cls(client, label, source=source, fills=fills)
        specs = {SpecId.of(s): s for s in client.specs()}
        for entry in files['chain.json']:
            template = d.template_of(entry['template'])
            table.steps.append(
                Step(
                    entry['id'],
                    specs[template.spec],
                    dict(entry['takes']),
                    dict(template.params),
                    entry['accept'],
                    entry['per'],
                )
            )
        table.rows = d.edits_of(files['edits.json'])
        return table


def _keys(submitted: Mapping[str, Mapping[str, Record]]) -> dict[str, list[str]]:
    return {s: sorted(r) for s, r in submitted.items()}

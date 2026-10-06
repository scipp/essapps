# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a binding: :class:`PipelineBinding`, and
:class:`AccumulatingPipelineBinding` for a pipeline of one run behind a spec
over tables of runs.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import sciline

from .binding import Function, HeldState

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec.

    ``params`` maps each params field to the sciline key it sets, and
    ``outputs`` each output field to the key that computes it. A stage sets the
    fixed values on a copy of the pipeline and cuts it at the blanks with
    ``sciline.Stage``, which computes what does not depend on the blanks on the
    first call and holds it. A blank that no output depends on is not fed:
    its value cannot change the outputs, and the plain request succeeds too.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
    ) -> None:
        self._pipeline = pipeline
        self._params = dict(params)
        self._outputs = dict(outputs)

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        unknown = (fixed.keys() | set(blanks)) - self._params.keys()
        if unknown:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        pipeline = self._pipeline.copy()
        for name, value in fixed.items():
            pipeline[self._params[name]] = value
        targets = tuple(self._outputs.values())
        fed: dict[str, Key] = {}
        if blanks:
            used = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
            fed = {n: self._params[n] for n in blanks if self._params[n] in used}
        stage = sciline.Stage(pipeline, outputs=targets, inputs=tuple(fed.values()))

        def call(**values: Any) -> dict[str, Any]:
            results = stage.compute({key: values[n] for n, key in fed.items()})
            return {name: results[key] for name, key in self._outputs.items()}

        return call


class AccumulatingPipelineBinding:
    """
    A sciline pipeline of one row of each table, behind a spec over tables.

    ``tables`` maps each table field to the sciline key that each field of its
    rows sets, ``params`` every other field to its key, and ``outputs`` each
    output field to the key that computes it. The keys in ``accumulate`` are
    summed over the rows, and the outputs are computed from the sums::

        AccumulatingPipelineBinding(
            sans,
            params={'bins': QBins},
            tables={
                'sample_runs': {'run': Filename[SampleRun]},
                'can_runs': {'run': Filename[BackgroundRun]},
            },
            outputs={'iofq': BackgroundSubtractedIofQ},
            accumulate=(
                Numerator[SampleRun], Denominator[SampleRun],
                Numerator[BackgroundRun], Denominator[BackgroundRun],
            ),
        )

    Each accumulated key must depend on the rows of exactly one table, and the
    outputs on the rows only through accumulated keys; both are checked here.
    Then the order of rows across tables cannot change a sum. The workflow
    author promises that the sums are what the reduction of all rows computes
    from, such as a numerator and a denominator, not a normalized curve.

    A plain request, a stage, and a held state all compute this way, through
    ``sciline.Stage``: each row's accumulated keys are computed and added, the
    first copied, and the outputs are computed from the sums. What depends
    only on the fixed values is computed once per stage or held state. In a
    stage over fixed rows, each row's part that does not depend on the blanks
    is computed once too, so tuning the bins loads each run once. An output
    that needs the sums of a table with no rows is left out.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        tables: Mapping[str, Mapping[str, Key]],
        outputs: Mapping[str, Key],
        accumulate: Iterable[Key],
    ) -> None:
        self._pipeline = pipeline
        self._params = dict(params)
        self._tables = {table: dict(fields) for table, fields in tables.items()}
        self._outputs = dict(outputs)
        table_of = {k: t for t, fields in self._tables.items() for k in fields.values()}

        def tables_read(outputs: Sequence[Key], cut: Sequence[Key] = ()) -> set[str]:
            return {table_of[k] for k in _uses(pipeline, outputs, cut) if k in table_of}

        self._summed: dict[str, tuple[Key, ...]] = dict.fromkeys(self._tables, ())
        for key in accumulate:
            read = sorted(tables_read([key]))
            if len(read) != 1:
                raise ValueError(
                    f'the accumulated key {key} must depend on the rows of one '
                    f'table, not of {read}'
                )
            self._summed[read[0]] += (key,)
        unsummed = sorted(t for t, keys in self._summed.items() if not keys)
        if unsummed:
            raise ValueError(f'no accumulated key depends on the rows of {unsummed}')
        summed = {k: t for t, keys in self._summed.items() for k in keys}
        self._needs: dict[str, frozenset[str]] = {}
        for name, key in self._outputs.items():
            cut = [k for k in summed if k in _uses(pipeline, [key])]
            if rows := tables_read([key], cut):
                raise ValueError(
                    f'the output {name} depends on the rows of {sorted(rows)} '
                    'other than through accumulated keys'
                )
            self._needs[name] = frozenset(summed[k] for k in cut)

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        plan = self._plan(fixed, blanks)
        tuned = {n: self._params[n] for n in blanks if n in self._params}
        tables = [t for t in blanks if t in self._tables]

        def call(**values: Any) -> Mapping[str, Any]:
            given = {key: values[n] for n, key in tuned.items()}
            sums = plan.start(given)
            for table in tables:
                for row in values[table]:
                    plan.add(sums, table, row, given)
            return plan.outputs(sums, given)

        return call

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        return _HeldSums(self._plan(fixed, [t for t in self._tables if t not in fixed]))

    def _plan(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> _Plan:
        unknown = (
            (fixed.keys() | set(blanks)) - self._params.keys() - self._tables.keys()
        )
        if unknown:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        pipeline = self._pipeline.copy()
        for name, value in fixed.items():
            if name in self._params:
                pipeline[self._params[name]] = value
        tuned = [self._params[n] for n in blanks if n in self._params]
        fixed_rows = [
            _Cut(self._with_row(pipeline, t, row), self._summed[t], tuned)
            for t in self._tables
            if t in fixed
            for row in fixed[t]
        ]
        pushed = {
            t: _Cut(pipeline, self._summed[t], [*self._tables[t].values(), *tuned])
            for t in blanks
            if t in self._tables
        }

        def finish(tables: frozenset[str]) -> tuple[_Cut, dict[str, Key]]:
            keys = {n: k for n, k in self._outputs.items() if self._needs[n] <= tables}
            summed = [k for t in tables for k in self._summed[t]]
            return _Cut(pipeline, list(keys.values()), tuned, cut=summed), keys

        return _Plan(self._row, self._summed, fixed_rows, pushed, finish)

    def _row(self, table: str, row: Mapping[str, Any]) -> dict[Key, Any]:
        fields = self._tables[table]
        unknown = row.keys() - fields.keys()
        if unknown:
            raise ValueError(f'no sciline key for {sorted(unknown)} of {table}')
        return {fields[name]: value for name, value in row.items()}

    def _with_row(
        self, pipeline: sciline.Pipeline, table: str, row: Mapping[str, Any]
    ) -> sciline.Pipeline:
        pipeline = pipeline.copy()
        for key, value in self._row(table, row).items():
            pipeline[key] = value
        return pipeline


def _uses(
    pipeline: sciline.Pipeline, outputs: Sequence[Key], cut: Sequence[Key] = ()
) -> frozenset[Key]:
    """The keys that ``outputs`` depend on, above none of ``cut``."""
    return sciline.Stage(pipeline, outputs=outputs, inputs=cut).keys


class _Cut:
    """
    A ``sciline.Stage`` cut at those of ``cut`` that ``outputs`` use, and fed
    those of ``inputs`` that they use below the cut.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        outputs: Sequence[Key],
        inputs: Sequence[Key],
        cut: Sequence[Key] = (),
    ) -> None:
        cut = [k for k in cut if k in _uses(pipeline, outputs)]
        used = _uses(pipeline, outputs, cut)
        self._inputs = [*cut, *(k for k in inputs if k in used)]
        self._stage = sciline.Stage(pipeline, outputs=outputs, inputs=self._inputs)

    def __call__(self, values: Mapping[Key, Any]) -> dict[Key, Any]:
        return self._stage.compute({k: values[k] for k in self._inputs})


Sums = dict[Key, Any]


class _Plan:
    """
    How one stage or held state sums rows and computes outputs from the sums.

    ``given`` holds the values of the stage's blanks other than tables, by
    sciline key; it is empty for a held state. The plan holds no sums, so a
    stage may be called from several threads at once.
    """

    def __init__(
        self,
        row: Callable[[str, Mapping[str, Any]], dict[Key, Any]],
        summed: Mapping[str, tuple[Key, ...]],
        fixed_rows: list[_Cut],
        pushed: Mapping[str, _Cut],
        finish: Callable[[frozenset[str]], tuple[_Cut, dict[str, Key]]],
    ) -> None:
        self._row = row
        self._summed = summed
        self._fixed_rows = fixed_rows
        self._pushed = pushed
        self._finish = finish
        self._finishes: dict[frozenset[str], tuple[_Cut, dict[str, Key]]] = {}

    def start(self, given: Mapping[Key, Any]) -> Sums:
        """The sums over the fixed rows."""
        sums: Sums = {}
        for cut in self._fixed_rows:
            _add(sums, cut(given))
        return sums

    def add(
        self, sums: Sums, table: str, row: Mapping[str, Any], given: Mapping[Key, Any]
    ) -> None:
        _add(sums, self._pushed[table]({**self._row(table, row), **given}))

    def outputs(self, sums: Sums, given: Mapping[Key, Any]) -> Mapping[str, Any]:
        tables = frozenset(t for t, keys in self._summed.items() if keys[0] in sums)
        if tables not in self._finishes:
            self._finishes[tables] = self._finish(tables)
        cut, keys = self._finishes[tables]
        values = cut({**sums, **given})
        return {name: values[key] for name, key in keys.items()}


def _add(sums: Sums, values: Mapping[Key, Any]) -> None:
    """Add in place, starting from a copy so that no row's value changes."""
    for key, value in values.items():
        if key in sums:
            sums[key] += value
        else:
            sums[key] = copy.deepcopy(value)


class _HeldSums:
    def __init__(self, plan: _Plan) -> None:
        self._plan = plan
        self._sums = plan.start({})

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            self._plan.add(self._sums, table, row, {})

    def outputs(self) -> Mapping[str, Any]:
        return self._plan.outputs(self._sums, {})

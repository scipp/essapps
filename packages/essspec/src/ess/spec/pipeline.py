# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a binding: :class:`PipelineBinding`, and
:class:`AccumulatingPipelineBinding` for a pipeline of one row of each table
behind a spec over tables.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sciline

from .binding import Function, HeldState

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec.

    ``params`` maps each parameter to the sciline key it sets: a group or a
    table as a whole by its name, or each field of a group by its path, such as
    ``'sample.file'``. A group set to ``None`` sets each key of its paths to
    ``None``. ``outputs`` maps each output field to the key that computes it. A
    stage sets the fixed values on a copy of the pipeline and cuts it at the
    keys of the blanks with ``sciline.Stage``, which computes what does not
    depend on them on the first call and holds it. A key that no output depends
    on is not fed: its value cannot change the outputs, and the plain request
    succeeds too.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
    ) -> None:
        self._pipeline = pipeline
        self._keys = _Keys(params)
        self._outputs = dict(outputs)

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        blank_keys = self._keys.of(blanks)
        pipeline = self._keys.set(self._pipeline, fixed)
        cut = _Cut(pipeline, tuple(self._outputs.values()), blank_keys)

        def call(**values: Any) -> dict[str, Any]:
            results = cut(self._keys.by_key(values))
            return {name: results[key] for name, key in self._outputs.items()}

        return call


class AccumulatingPipelineBinding:
    """
    A sciline pipeline of one row of each table, behind a spec over tables.

    ``params`` and ``outputs`` map as for :class:`PipelineBinding`, with each
    row of a table in place of a group: ``'sample_runs.file'`` maps the field
    ``file`` of each row of the table ``sample_runs``. ``accumulate`` maps each
    table to its accumulated keys, which are summed over its rows, and the
    outputs are computed from the sums::

        AccumulatingPipelineBinding(
            sans,
            params={
                'sample_runs.file': Filename[SampleRun],
                'can_runs.file': Filename[BackgroundRun],
                'reduction.bins': QBins,
            },
            outputs={'iofq': BackgroundSubtractedIofQ},
            accumulate={
                'sample_runs': (Numerator[SampleRun], Denominator[SampleRun]),
                'can_runs': (Numerator[BackgroundRun], Denominator[BackgroundRun]),
            },
        )

    With ``'sample.file'`` and ``'can.file'`` in ``params``, the same pipeline
    and outputs make the ``PipelineBinding`` of the single-run spec. For a
    table with no rows, the keys of its fields are set to ``None`` and one row
    is computed, as for a group set to ``None``, so ``can_runs=[]`` gives what
    ``can=None`` gives.

    Each accumulated key of a table must depend on the rows of that table and
    of no other. The outputs must depend on the rows only through accumulated
    keys. The constructor checks both. So a held state keeps each table's sums
    apart, and the order of rows across tables cannot change a sum. The
    workflow author promises that the accumulated keys add over runs, such as
    a numerator and a denominator, not a normalized curve.

    A plain request, a stage, and a held state all compute this way, through
    ``sciline.Stage``: each row's accumulated keys are computed and added, the
    first copied, and the outputs are computed from the sums. What depends
    only on the fixed values is computed once per stage or held state, the
    sums over fixed rows included. If those sums depend on a blank, a stage
    holds each fixed row's part that does not, so tuning the bins loads each
    run once.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
        accumulate: Mapping[str, Sequence[Key]],
    ) -> None:
        self._pipeline = pipeline
        self._keys = _Keys(params)
        self._outputs = dict(outputs)
        for table, keys in accumulate.items():
            if not isinstance(keys, Sequence):
                raise TypeError(
                    f'accumulate[{table!r}]: a sequence of keys, not {keys}'
                )
        self._accumulate = {table: tuple(keys) for table, keys in accumulate.items()}
        row_keys = {table: set(self._keys.of([table])) for table in self._accumulate}

        def tables_read(outputs: Sequence[Key], cut: Sequence[Key] = ()) -> list[str]:
            used = _uses(pipeline, outputs, cut)
            return sorted(table for table, keys in row_keys.items() if keys & used)

        for table, keys in self._accumulate.items():
            for key in keys:
                if (read := tables_read([key])) != [table]:
                    raise ValueError(
                        f'the accumulated key {key} of {table} depends on the rows '
                        f'of {read or "no table"}, not of {table} alone'
                    )
        accumulated = [key for keys in self._accumulate.values() for key in keys]
        for name, key in self._outputs.items():
            used = _uses(pipeline, [key])
            if rows := tables_read([key], [k for k in accumulated if k in used]):
                raise ValueError(
                    f'the output {name} depends on the rows of {rows} '
                    'other than through accumulated keys'
                )

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        if missing := self._accumulate.keys() - fixed.keys() - set(blanks):
            raise ValueError(f'no rows given for the tables {sorted(missing)}')
        plan = self._plan(fixed, blanks)
        blank_groups = [name for name in blanks if name not in self._accumulate]
        blank_tables = [name for name in blanks if name in self._accumulate]

        def call(**values: Any) -> Mapping[str, Any]:
            given = self._keys.by_key({name: values[name] for name in blank_groups})
            sums = _Sums(plan, given)
            for table in blank_tables:
                for row in values[table]:
                    sums.push({table: row})
            return sums.outputs()

        return call

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        tables = [table for table in self._accumulate if table not in fixed]
        return _Sums(self._plan(fixed, tables), {})

    def _plan(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> _Plan:
        return _Plan(
            self._pipeline, self._keys, self._accumulate, self._outputs, fixed, blanks
        )


class _Keys:
    """The sciline keys of parameters, mapped as ``params`` of PipelineBinding."""

    def __init__(self, params: Mapping[str, Key]) -> None:
        keys: dict[str, dict[str, Key]] = defaultdict(dict)
        for path, key in params.items():
            keys[path.split('.')[0]][path] = key
        if both := [n for n, ks in keys.items() if n in ks and len(ks) > 1]:
            raise ValueError(f'{both}: mapped as a whole and by paths')
        self._keys = dict(keys)

    def of(self, names: Iterable[str]) -> list[Key]:
        """The keys that the parameters ``names`` set."""
        names = list(names)
        if unknown := set(names) - self._keys.keys():
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        return [key for name in names for key in self._keys[name].values()]

    def set(
        self, pipeline: sciline.Pipeline, values: Mapping[str, Any]
    ) -> sciline.Pipeline:
        """A copy of ``pipeline`` with ``values`` set."""
        pipeline = pipeline.copy()
        for key, value in self.by_key(values).items():
            pipeline[key] = value
        return pipeline

    def by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
        """``values`` by the sciline key each sets."""
        by_key: dict[Key, Any] = {}
        unknown: list[str] = []
        for name, value in values.items():
            keys = self._keys.get(name, {})
            if name in keys:
                by_key[keys[name]] = value
            elif keys and value is None:
                by_key.update(dict.fromkeys(keys.values()))
            elif keys and isinstance(value, Mapping):
                for field, v in value.items():
                    path = f'{name}.{field}'
                    if path in keys:
                        by_key[keys[path]] = v
                    else:
                        unknown.append(path)
            else:
                unknown.append(name)
        if unknown:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        return by_key


def _uses(
    pipeline: sciline.Pipeline, outputs: Sequence[Key], cut: Sequence[Key] = ()
) -> frozenset[Key]:
    """The keys that ``outputs`` depend on, with the pipeline cut at ``cut``."""
    return sciline.Stage(pipeline, outputs=outputs, inputs=cut).keys


class _Cut:
    """
    A ``sciline.Stage`` of ``outputs``, cut at those of ``cut`` that they use,
    and fed those of ``inputs`` that they use below the cut.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        outputs: Sequence[Key],
        inputs: Sequence[Key],
        cut: Sequence[Key] = (),
    ) -> None:
        used = _uses(pipeline, outputs) if inputs or cut else frozenset()
        if cut := [k for k in cut if k in used]:
            used = _uses(pipeline, outputs, cut)
        self.inputs = [*cut, *(k for k in inputs if k in used)]
        self._stage = sciline.Stage(pipeline, outputs=outputs, inputs=self.inputs)

    def __call__(self, values: Mapping[Key, Any]) -> dict[Key, Any]:
        return self._stage.compute({k: values[k] for k in self.inputs})


class _Plan:
    """
    The cuts of one stage or held state of an AccumulatingPipelineBinding.

    A cut per table computes one row's accumulated keys from the row and the
    blank groups, and a cut at the accumulated keys computes the outputs from
    the sums. The sums over fixed rows are computed here, except for a table
    whose accumulated keys depend on a blank group: each of its fixed rows
    gets a cut of its own, which holds the row's part that does not. A plan
    holds no sums of pushed rows, so a stage may be called from several
    threads at once.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        keys: _Keys,
        accumulate: Mapping[str, tuple[Key, ...]],
        outputs: Mapping[str, Key],
        fixed: Mapping[str, Any],
        blanks: Sequence[str],
    ) -> None:
        self._keys = keys
        self._accumulate = accumulate
        self._outputs = outputs
        fixed_groups = {n: v for n, v in fixed.items() if n not in accumulate}
        pipeline = keys.set(pipeline, fixed_groups)
        blank_keys = keys.of(n for n in blanks if n not in accumulate)
        self._rows = {
            table: _Cut(pipeline, summed, [*keys.of([table]), *blank_keys])
            for table, summed in accumulate.items()
        }
        self._fixed_sums: dict[Key, Any] = {}
        self._fixed_rows: list[_Cut] = []
        for table, summed in accumulate.items():
            blanks_used = [k for k in blank_keys if k in self._rows[table].inputs]
            for row in fixed.get(table, ()):
                if blanks_used:
                    with_row = keys.set(pipeline, {table: row})
                    self._fixed_rows.append(_Cut(with_row, summed, blanks_used))
                else:
                    _add(self._fixed_sums, self.row(table, row, {}))
        accumulated = [key for summed in accumulate.values() for key in summed]
        self._finish = _Cut(pipeline, list(outputs.values()), blank_keys, accumulated)

    def start(self, given: Mapping[Key, Any]) -> dict[Key, Any]:
        """The sums over the fixed rows."""
        sums = copy.deepcopy(self._fixed_sums)
        for cut in self._fixed_rows:
            _add(sums, cut(given))
        return sums

    def row(
        self, table: str, row: Mapping[str, Any] | None, given: Mapping[Key, Any]
    ) -> dict[Key, Any]:
        """The accumulated keys of one row of ``table``."""
        return self._rows[table]({**self._keys.by_key({table: row}), **given})

    def outputs(
        self, sums: Mapping[Key, Any], given: Mapping[Key, Any]
    ) -> dict[str, Any]:
        """The outputs from ``sums``; a table with no rows has one that is None."""
        sums = dict(sums)
        for table, summed in self._accumulate.items():
            if not any(key in sums for key in summed):
                sums.update(self.row(table, None, given))
        values = self._finish({**sums, **given})
        return {name: values[key] for name, key in self._outputs.items()}


class _Sums:
    """
    The sums of the accumulated keys over the rows so far, the fixed rows and
    those pushed: the held state, and one call through a stage. ``given``
    holds the values of the blank groups by sciline key; it is empty for a
    held state.
    """

    def __init__(self, plan: _Plan, given: Mapping[Key, Any]) -> None:
        self._plan = plan
        self._given = given
        self._sums = plan.start(given)

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            _add(self._sums, self._plan.row(table, row, self._given))

    def outputs(self) -> Mapping[str, Any]:
        return self._plan.outputs(self._sums, self._given)


def _add(sums: dict[Key, Any], values: Mapping[Key, Any]) -> None:
    """Add in place, starting from a copy so that no row's value changes."""
    for key, value in values.items():
        if key in sums:
            sums[key] += value
        else:
            sums[key] = copy.deepcopy(value)

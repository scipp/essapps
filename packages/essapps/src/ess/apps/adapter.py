# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a workflow: every run is a call of a ``sciline.Stage``.

The request's parameters are resolved and set on a copy of the pipeline. The
parameters it varies become the inputs of a :py:class:`sciline.Stage`.
Everything the outputs need that no input can affect is computed once and held
at the frontier, and each call computes only what lies downstream of the
inputs. A stage over no inputs is a plain run of the pipeline.

A list parameter, such as the runs of a sum, is the member table of a
``sciline.Aggregation`` that the package builds from the configured pipeline;
the binding wraps it and builds none of its own. An element of the list is one
member: a value of the one member key, or a row, a model whose fields are the
columns of a member table with several member keys. Each member is contributed
through the aggregation into its accumulators, and one final stage computes the
outputs from the accumulated values of every list parameter, as sciline does for
two aggregations that share a final stage. A stage holds the accumulation over
the members it has seen, so a call whose list extends the previous one
contributes only the new members.

References in the parameters not varied are resolved once, when the stage is
built; references in the stage inputs, and the members of a sum, when they are
used.

See docs/developer/workflow-contract.md.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sciline
from pydantic import BaseModel

from .binding import Form, Inputs, StageCall, resolve

Key = Any
Columns = Key | Mapping[str, Key]
"""The sciline key a field sets, or for a list of rows the key of each column."""
MakeAggregation = Callable[[sciline.Pipeline], sciline.Aggregation]
"""
Builds the package's aggregation from a pipeline with its parameters set.

A factory, not an aggregation, because an aggregation is a snapshot of the
parameters, and the binding sets them per request.
"""


@dataclass(frozen=True)
class Wiring:
    """
    How the fields of a params model reach a sciline pipeline.

    ``keys`` is the sciline key each field sets, or for a list of rows the key
    of each column, ``resolve`` the form a data reference is asked for, a local
    path or a scipp object. The two together are everything a spec's signature
    does not say and the pipeline needs.
    """

    keys: dict[str, Columns]
    resolve: dict[str, Form]

    def value(self, name: str, value: Any, inputs: Inputs) -> Any:
        """A value of the named field, references resolved."""
        form = self.resolve.get(name)
        return value if form is None else resolve(value, form, inputs)

    def values(
        self, params: BaseModel, inputs: Inputs, names: Iterable[str]
    ) -> dict[Key, Any]:
        """The value of each named field, by sciline key, references resolved."""
        return {
            self.keys[name]: self.value(name, getattr(params, name), inputs)
            for name in names
        }

    def columns(self, name: str) -> tuple[Key, ...]:
        """The member keys an element of the named list parameter sets."""
        columns = self.keys[name]
        return tuple(columns.values()) if isinstance(columns, Mapping) else (columns,)

    def row(self, name: str, member: Any, inputs: Inputs) -> dict[Key, Any]:
        """One element of the named list parameter as a row of the member table."""
        columns = self.keys[name]
        if not isinstance(columns, Mapping):
            return {columns: self.value(name, member, inputs)}
        return {
            key: self.value(name, getattr(member, column), inputs)
            for column, key in columns.items()
        }


class _Sum:
    """
    The accumulation over one list parameter, which a stage holds.

    ``aggregation`` is the package's, built from the pipeline with every
    parameter set that the request does not vary. A varied parameter its
    contributions read, one of ``reads``, is set as well, so a new value builds
    a new aggregation, as it would for any changed parameter. What was pushed is
    kept with the values of those parameters, so a call whose list begins with
    the members pushed so far, under the same values, contributes only the
    rest. Any other call accumulates afresh.
    """

    def __init__(
        self,
        field: str,
        wiring: Wiring,
        pipeline: sciline.Pipeline,
        make: MakeAggregation,
        aggregation: sciline.Aggregation,
        reads: Sequence[str],
        keys: Sequence[Key],
    ) -> None:
        self.field = field
        self.keys = tuple(keys)
        self._wiring = wiring
        self._pipeline = pipeline
        self._make = make
        self._aggregation = aggregation
        self._reads = tuple(reads)
        self._context: list[Any] | None = None
        self._held: dict[Key, sciline.Accumulator[Any]] = {}
        self._pushed: list[Any] = []

    def accumulated(
        self, members: Sequence[Any], varied: BaseModel, inputs: Inputs
    ) -> dict[Key, Any]:
        """The accumulated value of each of ``keys`` over ``members``."""
        if not members:
            raise ValueError(f'{self.field} lists no members')
        context = [getattr(varied, name) for name in self._reads]
        if context != self._context:
            if self._reads:
                pipeline = self._pipeline.copy()
                for key, value in self._wiring.values(
                    varied, inputs, self._reads
                ).items():
                    pipeline[key] = value
                self._aggregation = self._make(pipeline)
            self._context = context
            self._held, self._pushed = self._aggregation.accumulators(), []
        elif list(members[: len(self._pushed)]) != self._pushed:
            self._held, self._pushed = self._aggregation.accumulators(), []
        for member in members[len(self._pushed) :]:
            row = self._wiring.row(self.field, member, inputs)
            for key, value in self._aggregation.contribute(row).items():
                self._held[key].push(value)
            self._pushed.append(member)
        return {key: self._held[key].value for key in self.keys}


class PipelineAdapter:
    """
    The workflow contract over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output field
    names, intermediates included, to the keys they compute, and ``resolve``
    names the form, a path or a scipp object, in which each data-reference
    parameter is set on the pipeline. ``aggregations`` gives, for each list
    parameter, the package's function that builds its aggregation. The key of
    a list parameter is the aggregation's member key, or for a list of rows a
    mapping from each field of the row model to a member key.

    A parameter input whose key the outputs do not need is held rather than fed.
    It cannot change what the stage returns, and which parameters a caller
    varies must not decide whether a run succeeds.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Columns],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        aggregations: Mapping[str, MakeAggregation] = {},
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._wiring = Wiring(self._keys, dict(resolve))
        self._aggregations = dict(aggregations)
        unknown = (
            self._wiring.resolve.keys() | self._aggregations.keys()
        ) - self._keys.keys()
        if unknown:
            raise ValueError(f'parameters without a key: {sorted(unknown)}')
        rows = [
            name
            for name, columns in self._keys.items()
            if isinstance(columns, Mapping) and name not in self._aggregations
        ]
        if rows:
            raise ValueError(f'rows of {rows} need an aggregation')

    def stage(
        self,
        params: BaseModel,
        inputs: Collection[str],
        outputs: Collection[str],
        data: Inputs,
    ) -> StageCall:
        """A ``sciline.Stage`` from ``inputs`` to ``outputs`` over the set values."""
        pipeline = self._pipeline.copy()
        given = type(params).model_fields
        fixed = [n for n in given if n in self._keys and n not in self._aggregations]
        for key, value in self._wiring.values(params, data, fixed).items():
            pipeline[key] = value
        targets = [self._targets[name] for name in outputs]
        needed = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
        varied = [name for name in inputs if name not in self._aggregations]
        sums = [
            self._sum(pipeline, field, varied, needed, outputs)
            for field in self._aggregations
            if any(key in needed for key in self._wiring.columns(field))
        ]
        accumulated = [key for s in sums for key in s.keys]
        cut = sciline.Stage(pipeline, outputs=targets, inputs=accumulated).keys
        fed = [name for name in varied if self._keys[name] in cut]
        finalize = sciline.Stage(
            pipeline,
            outputs=targets,
            inputs=[
                *accumulated,
                *(key for name in fed for key in self._wiring.columns(name)),
            ],
        )
        each = [
            s.field
            for s in sums
            if any(key in finalize.keys for key in self._wiring.columns(s.field))
        ]
        if each:
            raise ValueError(
                f'{sorted(outputs)} need each member of {each}, '
                'not only what the members accumulate to'
            )
        lists = {s.field: getattr(params, s.field) for s in sums if s.field in given}

        def call(params: BaseModel, inputs: Inputs) -> dict[str, Any]:
            values = self._wiring.values(params, inputs, fed)
            for s in sums:
                members = (
                    lists[s.field] if s.field in lists else getattr(params, s.field)
                )
                values |= s.accumulated(members, params, inputs)
            results = finalize.compute(values)
            return {name: results[self._targets[name]] for name in outputs}

        return call

    def _sum(
        self,
        pipeline: sciline.Pipeline,
        field: str,
        varied: Sequence[str],
        needed: Collection[Key],
        outputs: Collection[str],
    ) -> _Sum:
        """The accumulation over ``field`` that the outputs need."""
        make = self._aggregations[field]
        aggregation = make(pipeline)
        members = aggregation.contribute_stage.inputs
        if set(members) != set(self._wiring.columns(field)):
            raise ValueError(
                f'{field} sets {list(self._wiring.columns(field))}, but its '
                f'aggregation has the member keys {list(members)}'
            )
        keys = [key for key in aggregation.accumulation_keys if key in needed]
        if not keys:
            raise ValueError(
                f'{sorted(outputs)} need {field}, but no value they need '
                'accumulates over its members'
            )
        own = aggregation.contribute_stage.keys
        reads = [name for name in varied if self._keys[name] in own]
        return _Sum(field, self._wiring, pipeline, make, aggregation, reads, keys)

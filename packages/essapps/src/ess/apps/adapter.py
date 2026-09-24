# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a workflow: every run is a call of a ``sciline.Stage``.

The request's parameters are resolved and set on a copy of the pipeline. The
parameters it varies become the inputs of a :py:class:`sciline.Stage`.
Everything the outputs need that no input can affect is computed once and held
at the frontier, and each call computes only what lies downstream of the
inputs. A stage over no inputs is a plain run of the pipeline.

A member parameter holds a list, one element per member, such as the runs of a
sum. Its sciline key takes one element at a time, the author's accumulators
combine what each element contributes at the accumulation keys, and the outputs
are computed from the accumulated values. That is ``sciline.Aggregation``, run
inside one call, and the accumulators are those an ``Aggregation`` over the
same pipeline takes, so a package defines them once for notebooks and for the
binding. A stage holds the accumulation over the members it has seen, so a call
whose list extends the previous one contributes only the new members.

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


@dataclass(frozen=True)
class Wiring:
    """
    How the fields of a params model reach a sciline pipeline.

    ``keys`` is the sciline key each field sets, ``resolve`` the form a data
    reference is asked for, a local path or a scipp object. The two together are
    everything a spec's signature does not say and the pipeline needs.
    """

    keys: dict[str, Key]
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


class _Sum:
    """
    The accumulation over one member parameter, which a stage holds.

    ``contribute`` goes from the member key, and the keys of the varied
    parameters the contributions read, to the accumulation keys. What was
    pushed is kept with the values of those varied parameters, so a call whose
    list begins with the members pushed so far, under the same values,
    contributes only the rest. Any other call starts afresh; nothing is ever
    taken out.
    """

    def __init__(
        self,
        field: str,
        wiring: Wiring,
        contribute: sciline.Stage,
        reads: Sequence[str],
        accumulators: Mapping[Key, Callable[[], sciline.Accumulator[Any]]],
    ) -> None:
        self.field = field
        self._wiring = wiring
        self._contribute = contribute
        self._reads = tuple(reads)
        self._accumulators = accumulators
        self._held = self._fresh()
        self._pushed: list[Any] = []
        self._context: list[Any] = []

    @property
    def accumulation_keys(self) -> tuple[Key, ...]:
        return self._contribute.outputs

    def _fresh(self) -> dict[Key, sciline.Accumulator[Any]]:
        return {key: self._accumulators[key]() for key in self.accumulation_keys}

    def accumulated(
        self, members: Sequence[Any], varied: BaseModel, inputs: Inputs
    ) -> dict[Key, Any]:
        """The accumulated value of each accumulation key over ``members``."""
        if not members:
            raise ValueError(f'{self.field} lists no members')
        context = [getattr(varied, name) for name in self._reads]
        extends = list(members[: len(self._pushed)]) == self._pushed
        if context != self._context or not extends:
            self._held, self._pushed, self._context = self._fresh(), [], context
        row = self._wiring.values(varied, inputs, self._reads)
        for member in members[len(self._pushed) :]:
            row[self._wiring.keys[self.field]] = self._wiring.value(
                self.field, member, inputs
            )
            for key, value in self._contribute.compute(row).items():
                self._held[key].push(value)
            self._pushed.append(member)
        return {key: accumulator.value for key, accumulator in self._held.items()}


class PipelineAdapter:
    """
    The workflow contract over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output field
    names, intermediates included, to the keys they compute, and ``resolve``
    names the form, a path or a scipp object, in which each data-reference
    parameter is set on the pipeline. ``members`` names the fields that hold a
    list of members, each element a value of the field's key, and
    ``accumulators`` gives, by sciline key, a factory for the accumulator of
    each value the members contribute to.

    A parameter input whose key the outputs do not need is held rather than fed.
    It cannot change what the stage returns, and which parameters a caller
    varies must not decide whether a run succeeds.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        members: Collection[str] = (),
        accumulators: Mapping[Key, Callable[[], sciline.Accumulator[Any]]] = {},
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._wiring = Wiring(self._keys, dict(resolve))
        self._members = tuple(members)
        self._accumulators = dict(accumulators)
        unknown = (self._wiring.resolve.keys() | set(self._members)) - self._keys.keys()
        if unknown:
            raise ValueError(f'parameters without a key: {sorted(unknown)}')
        if self._members and not self._accumulators:
            raise ValueError(f'members {list(self._members)} need accumulators')
        absent = [k for k in self._accumulators if k not in pipeline.underlying_graph]
        if absent:
            raise ValueError(f'accumulators for keys not in the pipeline: {absent}')

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
        fixed = [n for n in given if n in self._keys and n not in self._members]
        for key, value in self._wiring.values(params, data, fixed).items():
            pipeline[key] = value
        targets = [self._targets[name] for name in outputs]
        needed = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
        varied = [name for name in inputs if name not in self._members]
        sums = [
            self._sum(pipeline, field, varied, needed, outputs)
            for field in self._members
            if self._keys[field] in needed
        ]
        accumulated = [key for s in sums for key in s.accumulation_keys]
        cut = sciline.Stage(pipeline, outputs=targets, inputs=accumulated).keys
        fed = [name for name in varied if self._keys[name] in cut]
        finalize = sciline.Stage(
            pipeline,
            outputs=targets,
            inputs=[*accumulated, *(self._keys[name] for name in fed)],
        )
        each = [s.field for s in sums if self._keys[s.field] in finalize.keys]
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
        key = self._keys[field]
        candidates = [k for k in self._accumulators if k in needed]
        try:
            probe = sciline.Stage(pipeline, outputs=candidates, inputs=[key])
        except ValueError:
            raise ValueError(
                f'{sorted(outputs)} need {field}, but no value they need '
                'accumulates over its members'
            ) from None
        accumulated = probe.dynamic_outputs
        own = sciline.Stage(pipeline, outputs=accumulated, inputs=[key]).keys
        reads = [name for name in varied if self._keys[name] in own]
        contribute = sciline.Stage(
            pipeline,
            outputs=accumulated,
            inputs=[key, *(self._keys[name] for name in reads)],
        )
        return _Sum(field, self._wiring, contribute, reads, self._accumulators)

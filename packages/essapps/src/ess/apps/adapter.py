# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a workflow: every run is a call of a ``sciline.Stage``.

The request's parameters are resolved and set on a copy of the pipeline.
The stage's inputs become the inputs of a :py:class:`sciline.Stage`: parameters
the request varies, and intermediates it supplies, whose
providers and ancestors the stage cuts off. Everything the outputs need that no
input can affect is computed once and held at the frontier, and each call
computes only what lies downstream of the inputs. A stage over no inputs is a
plain run of the pipeline.

References in the parameters not varied are resolved once, when the stage is
built; references in the stage inputs on every call.

The accumulators are those a ``sciline.Aggregation`` over the same pipeline
takes, by sciline key, so a package defines them once for notebooks and for the
binding.

See docs/developer/workflow-contract.md.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import sciline
from pydantic import BaseModel

from .binding import Accumulator, Form, Inputs, StageCall, resolve

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

    def values(
        self, params: BaseModel, inputs: Inputs, names: Iterable[str]
    ) -> dict[Key, Any]:
        """The value of each named field, by sciline key, references resolved."""
        values = {}
        for name in names:
            value = getattr(params, name)
            form = self.resolve.get(name)
            values[self.keys[name]] = (
                value if form is None else resolve(value, form, inputs)
            )
        return values


class PipelineAdapter:
    """
    The workflow contract over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output field
    names, intermediates included, to the keys they compute, and ``resolve``
    names the form, a path or a scipp object, in which each data-reference
    parameter is set on the pipeline. ``accumulators`` gives, by sciline key, a
    factory for the accumulator of each intermediate that may be accumulated.

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
        accumulators: Mapping[Key, Callable[[], Accumulator]] = {},
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._wiring = Wiring(self._keys, dict(resolve))
        self._accumulators = {
            name: accumulators[key]
            for name, key in self._targets.items()
            if key in accumulators
        }
        unknown = self._wiring.resolve.keys() - self._keys.keys()
        if unknown:
            raise ValueError(f'parameters without a key: {sorted(unknown)}')
        stray = set(accumulators) - set(self._targets.values())
        if stray:
            raise ValueError(f'accumulators for keys that are no output: {stray}')

    def stage(
        self,
        params: BaseModel,
        inputs: Collection[str],
        outputs: Collection[str],
        data: Inputs,
    ) -> StageCall:
        """A ``sciline.Stage`` from ``inputs`` to ``outputs`` over the set values."""
        pipeline = self._pipeline.copy()
        fixed = [name for name in type(params).model_fields if name in self._keys]
        for key, value in self._wiring.values(params, data, fixed).items():
            pipeline[key] = value
        targets = [self._targets[name] for name in outputs]
        intermediates = [name for name in inputs if name in self._targets]
        cut = [self._targets[name] for name in intermediates]
        needed = sciline.Stage(pipeline, outputs=targets, inputs=cut).keys
        fed = [
            name for name in inputs if name in self._keys and self._keys[name] in needed
        ]
        stage = sciline.Stage(
            pipeline,
            outputs=targets,
            inputs=[*cut, *(self._keys[name] for name in fed)],
        )

        def call(
            params: BaseModel, intermediates: Mapping[str, Any], inputs: Inputs
        ) -> dict[str, Any]:
            values = self._wiring.values(params, inputs, fed)
            values |= {self._targets[n]: v for n, v in intermediates.items()}
            results = stage.compute(values)
            return {name: results[self._targets[name]] for name in outputs}

        return call

    def accumulator(self, name: str) -> Accumulator:
        try:
            return self._accumulators[name]()
        except KeyError:
            raise ValueError(f'{name!r} has no accumulator') from None

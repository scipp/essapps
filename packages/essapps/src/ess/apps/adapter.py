# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a workflow, and as a stage over some of its parameters (D8).

The callable is stateless: it sets every parameter on a copy of the pipeline and
computes the targets, so no call can affect a later one.

``stage`` is the offer a session takes up when it expects a parameter to move.
The fields outside ``stage_inputs`` are resolved and set on a copy of the
pipeline, and the keys of the stage inputs become the inputs of a
:py:class:`sciline.Stage`: everything the targets need that no stage input can
affect is computed once and held at the frontier, and each call recomputes only
what lies downstream of the inputs. Correctness follows from the graph for any
choice of inputs, so the choice decides only where the frontier sits and with it
what a rerun costs. Which fields to feed is therefore the session's to decide
and not the binding author's; ``default_stage_inputs`` is the author's hint for
a request the session has nothing better to go on.

References in the fixed fields are resolved once, when the stage is built;
references in the stage inputs are resolved on every call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

import sciline
from pydantic import BaseModel

from .binding import Form, Inputs, Workflow, resolve

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
    The callable contract over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output field
    names to the keys to compute, and ``resolve`` names the form, a path or a
    scipp object, in which each data-reference parameter is set on the pipeline.
    ``default_stage_inputs`` names the fields to feed a stage when the session
    has not yet seen which parameter moves.

    A stage input whose key the targets do not need is held rather than fed. It
    cannot change what the stage returns, and which fields the session feeds is a
    caching choice, which must not decide whether a run succeeds.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        default_stage_inputs: Iterable[str] = (),
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._wiring = Wiring(self._keys, dict(resolve))
        self.default_stage_inputs = frozenset(default_stage_inputs)
        unknown = (
            self.default_stage_inputs | self._wiring.resolve.keys()
        ) - self._keys.keys()
        if unknown:
            raise ValueError(f'parameters without a key: {sorted(unknown)}')

    def _outputs(self, results: Mapping[Key, Any]) -> dict[str, Any]:
        return {name: results[key] for name, key in self._targets.items()}

    def __call__(self, params: BaseModel, inputs: Inputs) -> dict[str, Any]:
        pipeline = self._pipeline.copy()
        for key, value in self._wiring.values(params, inputs, self._keys).items():
            pipeline[key] = value
        return self._outputs(pipeline.compute(tuple(self._targets.values())))

    def stage(
        self, params: BaseModel, stage_inputs: AbstractSet[str], inputs: Inputs
    ) -> Workflow:
        """A callable over the pipeline with ``stage_inputs`` fed on every call."""
        pipeline = self._pipeline.copy()
        fixed = [name for name in self._keys if name not in stage_inputs]
        for key, value in self._wiring.values(params, inputs, fixed).items():
            pipeline[key] = value
        targets = list(self._targets.values())
        needed = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
        fed = [
            name
            for name, key in self._keys.items()
            if name in stage_inputs and key in needed
        ]
        stage = sciline.Stage(
            pipeline, outputs=targets, inputs=[self._keys[name] for name in fed]
        )

        def call(params: BaseModel, inputs: Inputs) -> dict[str, Any]:
            return self._outputs(
                stage.compute(self._wiring.values(params, inputs, fed))
            )

        return call

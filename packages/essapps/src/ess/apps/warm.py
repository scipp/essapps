# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a warm workflow (D8).

The wrapper builds a :py:class:`sciline.Stage` whose inputs are the parameters
the binding names as varying per call. The stage holds the values at its
frontier, everything the targets need that no input can affect, and recomputes
only what lies downstream of the inputs. A rerun that changes only inputs calls
the stage again; a change to any other parameter builds a new stage.
Correctness follows from the graph for any choice of inputs; the choice only
decides where the frontier sits, and with it what a rerun costs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sciline
import scipp as sc
from pydantic import BaseModel

from .binding import Form, Inputs, resolve

Key = Any


def equal(a: Any, b: Any) -> bool:
    if a is b:
        return True
    if isinstance(a, sc.Variable | sc.DataArray | sc.Dataset | sc.DataGroup):
        return type(a) is type(b) and sc.identical(a, b)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    try:
        return bool(a == b)
    except Exception:
        return False


class WarmPipeline:
    """
    The callable contract over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output field
    names to the keys to compute, ``resolve`` names the form, a path or a scipp
    object, in which each data-reference parameter is set on the pipeline, and
    ``stage_inputs`` names the parameters the stage takes per call. The other
    parameters are set on a copy of the pipeline, the stage inputs are the
    inputs of a :py:class:`sciline.Stage`, and each run calls that stage;
    ``reused`` says whether the run found the stage of the previous one. A stage
    input that the targets do not need is refused when the stage is built.

    Which parameters to make stage inputs is the binding's choice, and
    correctness does not depend on it: a parameter with expensive work
    downstream can be one, and the rerun then costs that work and nothing the
    parameter cannot affect. Fewer stage inputs hold more at the frontier; a
    change to a held parameter costs a rebuild.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        stage_inputs: Iterable[str] = (),
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._resolve = dict(resolve)
        self._stage_inputs = set(stage_inputs)
        unknown = (self._stage_inputs | self._resolve.keys()) - self._keys.keys()
        if unknown:
            raise ValueError(f'parameters without a key: {sorted(unknown)}')
        self._held: dict[str, Any] | None = None
        self._stage = self._build({})
        self.reused = False

    def _build(self, held: Mapping[str, Any]) -> sciline.Stage:
        pipeline = self._pipeline.copy()
        for name, value in held.items():
            pipeline[self._keys[name]] = value
        return sciline.Stage(
            pipeline,
            outputs=list(self._targets.values()),
            inputs=[self._keys[name] for name in self._stage_inputs],
        )

    def __call__(self, params: BaseModel, inputs: Inputs) -> dict[str, Any]:
        values = {name: getattr(params, name) for name in self._keys}
        for name, form in self._resolve.items():
            values[name] = resolve(values[name], form, inputs)
        held = {n: v for n, v in values.items() if n not in self._stage_inputs}
        self.reused = self._held is not None and equal(held, self._held)
        if not self.reused:
            self._stage = self._build(held)
            self._held = held
        results = self._stage.compute(
            {self._keys[name]: values[name] for name in self._stage_inputs}
        )
        return {name: results[key] for name, key in self._targets.items()}

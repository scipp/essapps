# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a warm workflow (D8).

The wrapper builds a :py:class:`sciline.Stage` whose inputs are the cheap
parameters. The stage holds the values at its frontier, everything the targets
need that a cheap parameter cannot affect, and recomputes only what lies
downstream of them. A rerun that changes only cheap parameters calls the stage
again; any other change builds a new stage. Correctness follows from the graph,
given the declared cheap parameters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sciline
import scipp as sc
from pydantic import BaseModel

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
    names to the keys to compute, and ``cheap`` names the parameters whose change
    must not recompute the expensive part. The expensive parameters are set on a
    copy of the pipeline, the cheap ones are the inputs of a
    :py:class:`sciline.Stage`, and each run calls that stage; ``reused`` says
    whether the run found the stage of the previous one. A cheap parameter that
    the targets do not need is refused when the stage is built.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        cheap: Iterable[str] = (),
    ) -> None:
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._cheap = set(cheap)
        unknown = self._cheap - self._keys.keys()
        if unknown:
            raise ValueError(f'cheap parameters without a key: {sorted(unknown)}')
        self._expensive: dict[str, Any] | None = None
        self._stage = self._build({})
        self.reused = False

    def _build(self, expensive: Mapping[str, Any]) -> sciline.Stage:
        pipeline = self._pipeline.copy()
        for name, value in expensive.items():
            pipeline[self._keys[name]] = value
        return sciline.Stage(
            pipeline,
            outputs=list(self._targets.values()),
            inputs=[self._keys[name] for name in self._cheap],
        )

    def __call__(self, params: BaseModel) -> dict[str, Any]:
        values = {name: getattr(params, name) for name in self._keys}
        expensive = {n: v for n, v in values.items() if n not in self._cheap}
        self.reused = self._expensive is not None and equal(expensive, self._expensive)
        if not self.reused:
            self._stage = self._build(expensive)
            self._expensive = expensive
        results = self._stage.compute(
            {self._keys[name]: values[name] for name in self._cheap}
        )
        return {name: results[key] for name, key in self._targets.items()}

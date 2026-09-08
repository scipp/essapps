# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a warm workflow (D8).

The wrapper keeps the pipeline and caches the values of the nodes just upstream
of the cheap parameters: everything that a change of a cheap parameter cannot
affect but that its consumers need. A rerun that changes only cheap parameters
starts from the cache; any other change recomputes from scratch and refreshes
it. Correctness follows from the graph, given the declared cheap parameters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import networkx as nx
import sciline
import scipp as sc
from pydantic import BaseModel

Key = Any


def frontier(pipeline: sciline.Pipeline, cheap: Iterable[Key]) -> set[Key]:
    """
    Nodes to cache: not downstream of a cheap key, feeding a node that is.

    Other parameters feeding the same consumers are included; caching them is
    harmless and keeps the rule one sentence.
    """
    graph = pipeline.underlying_graph
    cheap = set(cheap)
    downstream: set[Key] = set().union(*(nx.descendants(graph, k) for k in cheap))
    return {
        node
        for node in graph.nodes
        if node not in downstream
        and node not in cheap
        and any(s in downstream for s in graph.successors(node))
    }


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
    must not recompute the expensive part.
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
        self.frontier = frontier(pipeline, {self._keys[n] for n in self._cheap})
        self._cache: dict[Key, Any] | None = None
        self._expensive: dict[str, Any] | None = None
        self.reused = False

    def __call__(self, params: BaseModel) -> dict[str, Any]:
        values = {name: getattr(params, name) for name in self._keys}
        expensive = {n: v for n, v in values.items() if n not in self._cheap}
        pipeline = self._pipeline.copy()
        for name, key in self._keys.items():
            pipeline[key] = values[name]
        if self._cache is not None and equal(expensive, self._expensive):
            for key, value in self._cache.items():
                pipeline[key] = value
            results = pipeline.compute(list(self._targets.values()))
            self.reused = True
        else:
            wanted = set(self._targets.values()) | self.frontier
            results = pipeline.compute(list(wanted))
            self._cache = {k: results[k] for k in self.frontier}
            self._expensive = expensive
            self.reused = False
        return {name: results[key] for name, key in self._targets.items()}

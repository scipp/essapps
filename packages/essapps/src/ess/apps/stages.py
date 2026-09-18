# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The stages a session holds (D8).

A workflow is a stateless callable, so nothing carries over from one run to the
next unless the session decides it should. What it decides is which parameters it
expects to move: a request under a label that differs from the previous request
under that label names, by the fields in which it differs, what a person is
tuning. The session asks the workflow for a stage over those fields, holds it,
and serves later requests that differ only in them from it. Before a label has
seen a second request there is nothing to read, and the workflow's
``default_stage_inputs`` is the author's hint for that case.

A stage is identified by what it holds: the spec, the fields it feeds per call,
and the values of every other field. Stages are therefore not per label, and two
labels tuning the same parameter on the same data share one stage; the label
scopes only what "the previous request" means. Values are compared as plain data,
so two references to the same output compare equal and no array is loaded to
decide it.

What a stage holds is a cache, so dropping one is always safe: the store is
bounded and the least recently used goes first.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .binding import Inputs, Workflow, staged
from .spec import SpecId


def _fixed(values: dict[str, Any], stage_inputs: AbstractSet[str]) -> dict[str, Any]:
    """The fields a stage over ``stage_inputs`` holds, as plain data."""
    return {name: v for name, v in values.items() if name not in stage_inputs}


@dataclass(frozen=True)
class _Held:
    """A stage, and what makes it valid for a request."""

    spec: SpecId
    stage_inputs: frozenset[str]
    fixed: dict[str, Any]
    workflow: Workflow


class Stages:
    """The stages of one session, keyed by what they hold."""

    def __init__(self, *, limit: int = 4) -> None:
        self._limit = limit
        self._held: list[_Held] = []  # least recently used first
        self._previous: dict[tuple[SpecId, str], dict[str, Any]] = {}

    def workflow_for(
        self,
        spec_id: SpecId,
        workflow: Workflow,
        params: BaseModel,
        inputs: Inputs,
        label: str | None = None,
    ) -> tuple[Workflow, bool]:
        """
        The callable to run one request with, and whether it came out of a stage.

        A held stage serves the request if it fixes the values the request has;
        of several, the one feeding the fewest fields, which is the one holding
        the most. Otherwise the session asks for a stage over the fields the
        request moved and holds it. A workflow that offers no stage, and a
        request that moved nothing without the author naming a default, are
        called as they are and nothing is held.
        """
        values = params.model_dump(mode='json')
        found = self._find(spec_id, values)
        moved = self._moved(spec_id, label, values)
        if label is not None:
            self._previous[spec_id, label] = values
        if found is not None:
            self._held.remove(found)
            self._held.append(found)
            return found.workflow, True
        offer = staged(workflow)
        if offer is None:
            return workflow, False
        stage_inputs = moved or frozenset(offer.default_stage_inputs)
        if not stage_inputs:
            return workflow, False
        held = _Held(
            spec=spec_id,
            stage_inputs=stage_inputs,
            fixed=_fixed(values, stage_inputs),
            workflow=offer.stage(params, stage_inputs, inputs),
        )
        self._held.append(held)
        del self._held[: max(0, len(self._held) - self._limit)]
        return held.workflow, False

    def forget(self, spec_id: SpecId) -> None:
        """Drop everything held for a spec."""
        self._held = [h for h in self._held if h.spec != spec_id]
        self._previous = {k: v for k, v in self._previous.items() if k[0] != spec_id}

    def _find(self, spec_id: SpecId, values: dict[str, Any]) -> _Held | None:
        matching = [
            held
            for held in self._held
            if held.spec == spec_id and _fixed(values, held.stage_inputs) == held.fixed
        ]
        return min(matching, key=lambda held: len(held.stage_inputs), default=None)

    def _moved(
        self, spec_id: SpecId, label: str | None, values: dict[str, Any]
    ) -> frozenset[str]:
        """The fields this request changed against the previous one of its label."""
        if label is None:
            return frozenset()
        previous = self._previous.get((spec_id, label))
        if previous is None:
            return frozenset()
        return frozenset(n for n, v in values.items() if previous.get(n) != v)

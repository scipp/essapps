# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The stages a session holds.

A workflow is a stateless callable, so nothing carries over from one run to the
next unless the session decides it should. What it decides is which parameters it
expects to move: a request that differs from its **predecessor** names, by the
fields in which it differs, what a person is tuning. The session asks the
workflow for a stage over those fields, holds it, and serves later requests that
differ only in them from it.

A request's predecessor is the request it supersedes, which under one label and
member key is the previous request the session saw, or, for a new member of
a batch, the latest earlier request under the label. A request without a label
has no predecessor and nothing to differ from, and the workflow's
``default_stage_inputs`` is the author's hint for that case.

A stage is identified by what it holds: the spec, the fields it feeds per call,
the values of every other field, and the checksums of the datasets among them.
Stages are therefore not per label, and two labels tuning the same parameter on
the same data share one stage; the label only scopes the search for the
predecessor. Values are compared as plain data, so two references to the same
output compare equal and no array is loaded to decide it; the checksums keep a
file that changed on disk from finding the stage built from its earlier bytes.

What a stage holds is a cache, so dropping one is always safe: the store is
bounded and the least recently used goes first.

See docs/developer/stages.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .binding import Inputs, Workflow, staged
from .spec import DatasetRef, SpecId, walk_refs


@dataclass(frozen=True)
class _Address:
    """What a stage over ``stage_inputs`` holds, as plain data."""

    values: dict[str, Any]
    checksums: dict[str, str]


def _address(
    values: dict[str, Any],
    stage_inputs: AbstractSet[str],
    checksums: Mapping[str, str],
) -> _Address:
    fixed = {name: v for name, v in values.items() if name not in stage_inputs}
    datasets = {str(ref) for _, ref in walk_refs(fixed) if isinstance(ref, DatasetRef)}
    return _Address(
        values=fixed,
        checksums={k: v for k, v in checksums.items() if k in datasets},
    )


@dataclass(frozen=True)
class _Held:
    """A stage, and what makes it valid for a request."""

    spec: SpecId
    stage_inputs: frozenset[str]
    address: _Address
    workflow: Workflow


class Stages:
    """The stages of one session, keyed by what they hold."""

    def __init__(self, *, limit: int = 4) -> None:
        self._limit = limit
        self._held: list[_Held] = []  # least recently used first
        self._superseded: dict[tuple[SpecId, str, str | None], dict[str, Any]] = {}
        self._under_label: dict[tuple[SpecId, str], dict[str, Any]] = {}

    def workflow_for(
        self,
        spec_id: SpecId,
        workflow: Workflow,
        params: BaseModel,
        inputs: Inputs,
        label: str | None = None,
        member_key: str | None = None,
        checksums: Mapping[str, str] = {},
    ) -> tuple[Workflow, bool]:
        """
        The callable to run one request with, and whether it came out of a stage.

        A held stage serves the request if it fixes the values the request has;
        of several, the one feeding the fewest fields, which is the one holding
        the most. Otherwise the session asks for a stage over the fields the
        request moved against its predecessor and holds it. A workflow that
        offers no stage, and a request that moved nothing without the author
        naming a default, are called as they are and nothing is held.
        """
        values = params.model_dump(mode='json')
        found = self._find(spec_id, values, checksums)
        moved = self._moved(spec_id, label, member_key, values)
        self._remember(spec_id, label, member_key, values)
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
            address=_address(values, stage_inputs, checksums),
            workflow=offer.stage(params, stage_inputs, inputs),
        )
        self._held.append(held)
        del self._held[: max(0, len(self._held) - self._limit)]
        return held.workflow, False

    def forget(self, spec_id: SpecId) -> None:
        """Drop everything held for a spec."""
        self._held = [h for h in self._held if h.spec != spec_id]
        self._superseded = {
            k: v for k, v in self._superseded.items() if k[0] != spec_id
        }
        self._under_label = {
            k: v for k, v in self._under_label.items() if k[0] != spec_id
        }

    def _find(
        self, spec_id: SpecId, values: dict[str, Any], checksums: Mapping[str, str]
    ) -> _Held | None:
        matching = [
            held
            for held in self._held
            if held.spec == spec_id
            and _address(values, held.stage_inputs, checksums) == held.address
        ]
        return min(matching, key=lambda held: len(held.stage_inputs), default=None)

    def _predecessor(
        self, spec_id: SpecId, label: str | None, member_key: str | None
    ) -> dict[str, Any] | None:
        """
        The request this one differs from: the one it supersedes, or the one before.

        Under a label and member key a request supersedes the previous one the
        session saw; a member key the session has not seen is a new member of a
        batch, whose predecessor is the latest earlier request under the label.
        """
        if label is None:
            return None
        superseded = self._superseded.get((spec_id, label, member_key))
        if superseded is not None:
            return superseded
        return self._under_label.get((spec_id, label))

    def _remember(
        self,
        spec_id: SpecId,
        label: str | None,
        member_key: str | None,
        values: dict[str, Any],
    ) -> None:
        if label is None:
            return
        self._superseded[spec_id, label, member_key] = values
        self._under_label[spec_id, label] = values

    def _moved(
        self,
        spec_id: SpecId,
        label: str | None,
        member_key: str | None,
        values: dict[str, Any],
    ) -> frozenset[str]:
        """The fields this request changed against its predecessor."""
        previous = self._predecessor(spec_id, label, member_key)
        if previous is None:
            return frozenset()
        return frozenset(n for n, v in values.items() if previous.get(n) != v)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Binding specs to code (D8).

A workflow is one callable from the validated params model to the outputs model.
A factory makes the callable; a throwaway runner calls it once, a session runner
keeps it. Specs come from entry points in the group ``ess.apps.workflows``, each
resolving to a ``(spec, factory)`` pair, or are bound in-process by a notebook,
which may not shadow an installed spec.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal

from pydantic import BaseModel

from .spec import FILE_SPEC, SpecId, WorkflowSpec

Workflow = Callable[[BaseModel], BaseModel | dict[str, Any]]
Factory = Callable[[], Workflow]
ENTRY_POINT_GROUP = 'ess.apps.workflows'


@dataclass(frozen=True)
class Binding:
    spec: WorkflowSpec
    factory: Factory | None
    how: Literal['entry_point', 'in_process', 'file']


class Registry:
    """The specs an environment can run, with the factory for each."""

    def __init__(self) -> None:
        self._bindings: dict[SpecId, Binding] = {
            FILE_SPEC.id: Binding(FILE_SPEC, None, 'file')
        }

    def bind(
        self,
        spec: WorkflowSpec,
        factory: Factory,
        *,
        how: Literal['entry_point', 'in_process'] = 'in_process',
    ) -> None:
        existing = self._bindings.get(spec.id)
        if existing is not None and existing.how != 'in_process':
            raise ValueError(f'{spec.id} is provided by an installed package')
        self._bindings[spec.id] = Binding(spec, factory, how)

    def load_entry_points(self, group: str = ENTRY_POINT_GROUP) -> None:
        for ep in entry_points(group=group):
            spec, factory = ep.load()
            self._bindings[spec.id] = Binding(spec, factory, 'entry_point')

    def __contains__(self, spec_id: SpecId) -> bool:
        return spec_id in self._bindings

    def __getitem__(self, spec_id: SpecId) -> Binding:
        try:
            return self._bindings[spec_id]
        except KeyError:
            raise KeyError(f'No workflow bound for {spec_id}') from None

    def specs(self) -> Iterable[WorkflowSpec]:
        return [b.spec for b in self._bindings.values()]


def import_object(path: str) -> Any:
    """Resolve ``module:name``."""
    module, name = path.split(':')
    return getattr(importlib.import_module(module), name)


def entry_point_registry() -> Registry:
    """The registry of an environment: every spec its installed packages provide."""
    registry = Registry()
    registry.load_entry_points()
    return registry


ENTRY_POINT_REGISTRY = f'{__name__}:entry_point_registry'

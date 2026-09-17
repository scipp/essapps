# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Binding specs to code (D8).

A workflow is one callable from the validated params model to the outputs model.
A factory makes the callable; a throwaway runner calls it once, a session runner
keeps it. Installed packages provide specs through the entry-point group
``ess.apps.specs`` and factories through ``ess.apps.workflows`` under the same
entry-point name, so a backend can load every spec without importing any
workflow code; only a runner asks for a factory. A notebook may bind a spec
in-process, but may not shadow an installed one.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal

from pydantic import BaseModel

from .spec import SpecId, WorkflowSpec

Workflow = Callable[[BaseModel], BaseModel | dict[str, Any]]
Factory = Callable[[], Workflow]
Loader = Callable[[], Factory]
SPEC_GROUP = 'ess.apps.specs'
WORKFLOW_GROUP = 'ess.apps.workflows'
How = Literal['entry_point', 'in_process']


@dataclass(frozen=True)
class Binding:
    spec: WorkflowSpec
    factory: Factory | None
    how: How


class Registry:
    """The specs an environment knows, and how a runner gets the code for each."""

    def __init__(self) -> None:
        self._specs: dict[SpecId, WorkflowSpec] = {}
        self._how: dict[SpecId, How] = {}
        self._loaders: dict[SpecId, Loader] = {}

    def bind(self, spec: WorkflowSpec, factory: Factory) -> None:
        """Bind in this process; refused if an installed package provides the spec."""
        if self._how.get(spec.id, 'in_process') != 'in_process':
            raise ValueError(f'{spec.id} is provided by an installed package')
        self._specs[spec.id] = spec
        self._how[spec.id] = 'in_process'
        self._loaders[spec.id] = lambda: factory

    def load_entry_points(self) -> None:
        """Load every installed spec; workflow code stays unimported until asked."""
        factories = {ep.name: ep for ep in entry_points(group=WORKFLOW_GROUP)}
        for ep in entry_points(group=SPEC_GROUP):
            spec: WorkflowSpec = ep.load()
            self._specs[spec.id] = spec
            self._how[spec.id] = 'entry_point'
            if ep.name in factories:
                self._loaders[spec.id] = factories[ep.name].load

    def __contains__(self, spec_id: SpecId) -> bool:
        return spec_id in self._specs

    def spec(self, spec_id: SpecId) -> WorkflowSpec:
        try:
            return self._specs[spec_id]
        except KeyError:
            raise KeyError(f'No spec {spec_id}') from None

    def binding(self, spec_id: SpecId) -> Binding:
        """Spec plus factory, importing the workflow code; runner side only."""
        spec = self.spec(spec_id)
        loader = self._loaders.get(spec_id)
        return Binding(spec, None if loader is None else loader(), self._how[spec_id])

    def specs(self) -> Iterable[WorkflowSpec]:
        return list(self._specs.values())


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

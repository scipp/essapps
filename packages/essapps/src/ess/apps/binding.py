# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Binding specs to code (D8).

A workflow is one callable from the validated params model to its outputs by
field name. The params model holds references where the request does; the
callable asks the runner's :class:`Inputs` for the form it wants, a local path or
a scipp object, so which form each parameter takes is decided here, next to the
sciline key it maps to, and never by the spec (D13). Where the bytes come from,
a session's memory, the data store, or a work directory, is the runner's.
The callable is stateless: no call affects a later one. It may offer a stage as
well, a callable over a subset of its parameters that holds what those
parameters cannot affect, which a session asks for and keeps; see
:class:`StagedWorkflow`.
A factory makes the callable; a throwaway runner calls it
once, a session runner keeps it. Installed packages provide specs through the
entry-point group ``ess.apps.specs`` and factories through
``ess.apps.workflows`` under the same
entry-point name, so a backend can load every spec without importing any
workflow code; only a runner asks for a factory. A notebook may bind a spec
in-process, but may not shadow an installed one.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from .spec import Ref, SpecId, WorkflowSpec, as_ref


class Inputs(Protocol):
    """How a callable gets at the bytes a reference names."""

    def path(self, ref: Ref) -> Path:
        """A local file holding the bytes."""
        ...

    def array(self, ref: Ref) -> Any:
        """The scipp object a scipp-format reference names; fails for other bytes."""
        ...


Form = Literal['path', 'array']
"""The form a binding asks for: the name of the :class:`Inputs` method."""


def resolve(value: Any, form: Form, inputs: Inputs) -> Any:
    """``value`` with every reference in it, through collections, in ``form``."""
    if (ref := as_ref(value)) is not None:
        return getattr(inputs, form)(ref)
    if isinstance(value, list):
        return [resolve(v, form, inputs) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, form, inputs) for k, v in value.items()}
    return value


Workflow = Callable[[BaseModel, Inputs], Mapping[str, Any]]
Factory = Callable[[], Workflow]
Loader = Callable[[], Factory]
SPEC_GROUP = 'ess.apps.specs'
WORKFLOW_GROUP = 'ess.apps.workflows'
How = Literal['entry_point', 'in_process']


class StagedWorkflow(Protocol):
    """
    A workflow that also offers a stage over some of its parameters (D8).

    ``stage`` returns a callable with the workflow's own signature. It is valid
    for every request that equals ``params`` in all fields outside
    ``stage_inputs``, and for those it returns what the workflow returns:
    ``wf.stage(p0, s, inputs)(p, inputs) == wf(p, inputs)``. It may hold whatever
    the stage inputs cannot affect, which makes what it holds a cache and
    dropping it always safe. ``default_stage_inputs`` names the fields to feed
    when the session has not yet seen which parameter moves.

    The workflow itself stays stateless; only the session decides whether to ask
    for a stage, and holds the ones it asked for. A plain function offers none.
    """

    default_stage_inputs: Collection[str]

    def __call__(self, params: BaseModel, inputs: Inputs) -> Mapping[str, Any]: ...

    def stage(
        self, params: BaseModel, stage_inputs: AbstractSet[str], inputs: Inputs
    ) -> Workflow: ...


def staged(workflow: Workflow) -> StagedWorkflow | None:
    """The workflow as a stage offer, or None if it offers no stage."""
    if not callable(getattr(workflow, 'stage', None)):
        return None
    return workflow  # type: ignore[return-value]


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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Binding specs to code.

A workflow is the code behind a spec: given the parameters of a stage request,
it builds the stage a stage record names, from the stage's inputs to its
outputs, as ``sciline.Stage`` does for a pipeline. The params model holds
references where the request does; the code asks the runner's :class:`Inputs`
for the form it wants, a local path or a scipp object, so which form each
parameter takes is decided here, next to the sciline key it maps to, and never
by the spec. Where the bytes come from, a session's memory, the data store, or
a work directory, is the runner's. A workflow holds nothing between calls; a
session holds the stages it built. A factory makes the workflow, and a plain
function ``(params, inputs) -> outputs`` is one too (:class:`FunctionWorkflow`).
Installed packages provide specs through the entry-point group
``ess.apps.specs`` and factories through ``ess.apps.workflows`` under the same
entry-point name, so a backend can load every spec without importing any
workflow code; only a runner asks for a factory. A notebook may bind a spec
in-process, but may not shadow an installed one.

See docs/developer/workflow-contract.md.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Collection, Iterable, Mapping
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


class StageCall(Protocol):
    """
    One stage of a workflow, called once per stage record.

    ``params`` holds the parameters the stage takes as inputs, ``intermediates``
    the intermediates it takes as inputs, already as objects, and the result
    holds the stage's outputs by field name.
    """

    def __call__(
        self, params: BaseModel, intermediates: Mapping[str, Any], inputs: Inputs
    ) -> Mapping[str, Any]: ...


class Accumulator(Protocol):
    """sciline's accumulator: push values, read their accumulation."""

    def push(self, value: Any) -> None: ...

    @property
    def value(self) -> Any: ...


class Workflow(Protocol):
    """
    The code behind a spec: builds the stages that stage records name.

    ``params`` holds the request's parameters, with the spec's defaults for
    the fields neither it nor the stage sets; ``inputs`` and ``outputs`` are the
    stage's names. What a stage holds between calls is what its inputs cannot
    affect, so holding it is a cache and dropping it always safe. A stage that
    leaves a needed parameter unset fails here or when called, since only the
    code knows what depends on what.

    ``accumulator`` gives a fresh accumulator for an intermediate that an
    :class:`ess.apps.records.Accumulate` may fill. Every accumulator must be
    associative, which :func:`ess.apps.testing.assert_accumulator_is_associative`
    checks.
    """

    def stage(
        self,
        params: BaseModel,
        inputs: Collection[str],
        outputs: Collection[str],
        data: Inputs,
    ) -> StageCall: ...

    def accumulator(self, name: str) -> Accumulator: ...


class FunctionWorkflow:
    """
    A plain function ``(params, inputs) -> outputs`` as a workflow.

    A function has no graph to cut, so its only stages are those whose inputs
    are parameters; they compute everything and hold nothing, and the outputs
    not asked for are dropped. Nothing it computes can be accumulated.
    """

    def __init__(
        self,
        function: Callable[[BaseModel, Inputs], Mapping[str, Any]],
        spec: WorkflowSpec,
    ) -> None:
        self._function = function
        self._spec = spec

    def stage(
        self,
        params: BaseModel,
        inputs: Collection[str],
        outputs: Collection[str],
        data: Inputs,
    ) -> StageCall:
        intermediates = set(inputs) - set(self._spec.params.model_fields)
        if intermediates:
            raise ValueError(
                f'{self._spec.id} is a plain function and takes no intermediates; '
                f'asked for {sorted(intermediates)}'
            )
        fixed = params.model_dump()

        def call(
            params: BaseModel, intermediates: Mapping[str, Any], inputs: Inputs
        ) -> dict[str, Any]:
            full = self._spec.params.model_validate({**fixed, **params.model_dump()})
            computed = self._function(full, inputs)
            return {name: computed[name] for name in outputs if name in computed}

        return call

    def accumulator(self, name: str) -> Accumulator:
        raise ValueError(f'{self._spec.id} is a plain function and accumulates nothing')


def as_workflow(code: Any, spec: WorkflowSpec) -> Workflow:
    """What a factory made, as a workflow: a plain function is wrapped."""
    if callable(getattr(code, 'stage', None)):
        return code  # type: ignore[no-any-return]
    return FunctionWorkflow(code, spec)


Factory = Callable[[], Any]
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

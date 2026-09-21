# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Two plain specs cut from one sciline pipeline: contribute and combine.

A spec is the signature of one callable and a record one call of it, so an
aggregation over runs is not one spec with three entry points but two specs
whose callables the framework composes across records: the contribute spec takes
the parameters of one member and produces its contribution, the combine spec
takes a collection of contributions and the finalize parameters and produces the
combined contribution and the results. The pipeline's own spec, one run start to
finish, is a third cut, and needs nothing but
:class:`ess.apps.adapter.PipelineAdapter`.

Both callables are stateless. Neither this class nor the callables it returns
hold a :class:`sciline.Aggregation` or a :class:`sciline.Stage` between calls;
the only thing held between calls is what a session asks for through
``combine_workflow().stage`` and then owns.

A contribution is a scipp data group with one entry per accumulation key, so
that one reference names a member's whole contribution and the store keeps it
like any other scipp output. One further entry, :data:`SHARED`, holds the
contribute parameters that are not member parameters, as the JSON of a model of
exactly those fields. Which parameters may differ between members is sciline's
member keys, which only this adapter knows, so the check that members agree
cannot live in the backend: the combine refuses contributions whose
:data:`SHARED` entries differ, and carries the agreed one into the combined
contribution, so the check holds along a chain without walking it.

When built, the adapter reads the split back off the graph and refuses specs
that disagree with it: the parameters of the contribute spec are the parameters
in ``contribute_stage.keys``, those of the combine spec are the parameters in
``finalize_stage.keys`` that are not contribute's plus the collection the
declaration names, and an accumulation key that does not depend on the members
is not part of a contribution at all.

See docs/developer/aggregation.md.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from collections.abc import Set as AbstractSet
from typing import Any

import sciline
import scipp as sc
from pydantic import BaseModel

from .adapter import Key, PipelineAdapter, Wiring
from .binding import Form, Inputs, Workflow, resolve
from .spec import WorkflowSpec, submodel

SHARED = 'shared'
"""Entry of a contribution holding what every member of a combine must agree on."""


class Aggregation:
    """
    The contribute and combine callables of one sciline pipeline.

    ``keys`` maps parameter field names to sciline keys and ``resolve`` names the
    form each data-reference parameter is set in, both as
    :class:`ess.apps.adapter.PipelineAdapter` takes them, over the union of the
    two specs' parameters. ``targets`` maps the result output fields to the keys
    the finalize half computes. ``members`` names the parameters that may differ
    between members; ``accumulation_keys`` maps the entries of a contribution to
    the sciline keys they hold.

    Each accumulation key is accumulated by ``sciline.Buffered(accumulate)``,
    unless ``accumulators`` gives a factory for it, which is where a running
    total belongs when a sum over large dense arrays should hold one array rather
    than one per member.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        contribute: WorkflowSpec,
        combine: WorkflowSpec,
        run: WorkflowSpec | None = None,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        members: Iterable[str],
        accumulation_keys: Mapping[str, Key],
        accumulate: Callable[..., Any],
        accumulators: Mapping[str, Callable[[], Any]] | None = None,
    ) -> None:
        if not targets:
            raise ValueError('an aggregation needs the outputs its finalize computes')
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._wiring = Wiring(self._keys, dict(resolve))
        self._accumulation_keys = dict(accumulation_keys)
        if SHARED in self._accumulation_keys:
            raise ValueError(f'{SHARED!r} is the reserved entry of a contribution')
        factories = dict(accumulators or {})
        self._accumulators = {
            name: factories.get(name, sciline.Buffered(accumulate))
            for name in self._accumulation_keys
        }
        self._members = list(members)
        try:
            ((self._collection, self._chained),) = combine.chain.items()
        except ValueError:
            raise ValueError(
                f'{combine.id} must chain exactly one collection parameter to the '
                f'output that stands for it; it chains {dict(combine.chain)}'
            ) from None
        if len(contribute.outputs.model_fields) != 1:
            raise ValueError(
                f'{contribute.id} must have exactly one output, the contribution; '
                f'it has {sorted(contribute.outputs.model_fields)}'
            )
        (self._contribution,) = contribute.outputs.model_fields
        self._check(contribute, combine, run)
        self._shared_model = submodel(
            contribute.params,
            [n for n in contribute.params.model_fields if n not in self._members],
            'Shared',
        )

    # Reading the split back off the graph

    def _check(
        self, contribute: WorkflowSpec, combine: WorkflowSpec, run: WorkflowSpec | None
    ) -> None:
        """Refuse specs the graph disagrees with, when the adapter is built."""
        probe = sciline.Aggregation(
            self._pipeline,
            members=[self._keys[name] for name in self._members],
            accumulators={
                self._accumulation_keys[name]: factory
                for name, factory in self._accumulators.items()
            },
            outputs=list(self._targets.values()),
        )
        static = sorted(
            name
            for name, key in self._accumulation_keys.items()
            if key not in probe.accumulation_keys
        )
        if static:
            raise ValueError(
                f'the accumulation keys {static} do not depend on the members, so '
                'they are computed once and never combined; they are not part of a '
                'contribution'
            )
        contribute_params = self._params_in(probe.contribute_stage.keys)
        finalize_keys = probe.finalize_stage.keys  # type: ignore[union-attr]
        self._finalize_params = sorted(
            self._params_in(finalize_keys) - contribute_params
        )
        self._shared_for_finalize = sorted(
            name
            for name in contribute_params - set(self._members)
            if self._keys[name] in finalize_keys
        )
        literal = set(self._finalize_params)
        _expect(contribute, 'params', contribute_params)
        _expect(combine, 'params', literal | {self._collection})
        _expect(combine, 'outputs', set(self._targets) | {self._chained})
        if run is not None:
            _expect(run, 'params', contribute_params | literal)
            _expect(run, 'outputs', set(self._targets))

    def _params_in(self, keys: AbstractSet[Key]) -> set[str]:
        return {name for name, key in self._keys.items() if key in keys}

    # The three callables

    def contribute_workflow(self) -> Workflow:
        """Member parameters and what the members share in, one contribution out."""

        def call(params: BaseModel, inputs: Inputs) -> dict[str, Any]:
            pipeline = self._pipeline.copy()
            names = [*self._members, *self._shared_model.model_fields]
            for key, value in self._wiring.values(params, inputs, names).items():
                pipeline[key] = value
            computed = pipeline.compute(tuple(self._accumulation_keys.values()))
            group = sc.DataGroup(
                {name: computed[key] for name, key in self._accumulation_keys.items()}
            )
            group[SHARED] = sc.scalar(
                self._shared_model(
                    **{
                        name: getattr(params, name)
                        for name in self._shared_model.model_fields
                    }
                ).model_dump_json()
            )
            return {self._contribution: group}

        return call

    def combine_workflow(self) -> Workflow:
        """Contributions and the finalize parameters in, results out."""
        return _Combine(self)

    def run_workflow(self) -> Workflow:
        """The pipeline's own spec: one run, start to finish."""
        return PipelineAdapter(
            self._pipeline,
            keys=self._keys,
            targets=self._targets,
            resolve=self._wiring.resolve,
        )

    # What the combine callable is made of

    def _combined(self, params: BaseModel, inputs: Inputs) -> sc.DataGroup:
        """Combine the referenced contributions; refuses ones that disagree."""
        contributions = resolve(getattr(params, self._collection), 'array', inputs)
        if isinstance(contributions, dict):
            contributions = list(contributions.values())
        shared = contributions[0][SHARED]
        for other in contributions[1:]:
            if other[SHARED].value != shared.value:
                raise ValueError(
                    'contributions of members reduced with different values of the '
                    'parameters they must share: '
                    f'{shared.value} and {other[SHARED].value}'
                )
        accumulators = {name: make() for name, make in self._accumulators.items()}
        for contribution in contributions:
            for name, accumulator in accumulators.items():
                accumulator.push(contribution[name])
        combined = sc.DataGroup({name: a.value for name, a in accumulators.items()})
        combined[SHARED] = shared
        return combined

    def _from_contribution(
        self, combined: sc.DataGroup, inputs: Inputs
    ) -> dict[Key, Any]:
        """
        What the finalize half reads off a contribution.

        The values at the accumulation keys, and the shared parameters the
        finalize half reads as well, which reach it with the values the members
        were reduced with and never from the combine request.
        """
        values = {key: combined[name] for name, key in self._accumulation_keys.items()}
        if self._shared_for_finalize:
            shared = self._shared_model.model_validate_json(combined[SHARED].value)
            values |= self._wiring.values(shared, inputs, self._shared_for_finalize)
        return values


class _Combine:
    """
    The combine callable, and the stage a session may ask it for.

    A session that moves a finalize parameter over one set of contributions gets
    a stage that has combined them once and holds everything downstream that the
    parameter does not affect; a session that adds members to a series gets one
    that holds what neither the accumulation keys nor a moving parameter affect.
    """

    default_stage_inputs: frozenset[str] = frozenset()

    def __init__(self, aggregation: Aggregation) -> None:
        self._aggregation = aggregation

    def __call__(self, params: BaseModel, inputs: Inputs) -> dict[str, Any]:
        agg = self._aggregation
        combined = agg._combined(params, inputs)
        pipeline = agg._pipeline.copy()
        values = agg._from_contribution(combined, inputs)
        values |= agg._wiring.values(params, inputs, agg._finalize_params)
        for key, value in values.items():
            pipeline[key] = value
        results = pipeline.compute(tuple(agg._targets.values()))
        return {
            agg._chained: combined,
            **{name: results[key] for name, key in agg._targets.items()},
        }

    def stage(
        self, params: BaseModel, stage_inputs: AbstractSet[str], inputs: Inputs
    ) -> Workflow:
        agg = self._aggregation
        pipeline = agg._pipeline.copy()
        moving = [n for n in agg._finalize_params if n in stage_inputs]
        fixed = [n for n in agg._finalize_params if n not in stage_inputs]
        for key, value in agg._wiring.values(params, inputs, fixed).items():
            pipeline[key] = value
        held: sc.DataGroup | None = None
        keys = [agg._keys[name] for name in moving]
        if agg._collection in stage_inputs:
            keys += [
                *agg._accumulation_keys.values(),
                *(agg._keys[name] for name in agg._shared_for_finalize),
            ]
        else:
            held = agg._combined(params, inputs)
            for key, value in agg._from_contribution(held, inputs).items():
                pipeline[key] = value
        targets = list(agg._targets.values())
        stage = sciline.Stage(pipeline, outputs=targets, inputs=keys)

        def call(params: BaseModel, inputs: Inputs) -> dict[str, Any]:
            combined = held if held is not None else agg._combined(params, inputs)
            values = (
                {} if held is not None else agg._from_contribution(combined, inputs)
            )
            values |= agg._wiring.values(params, inputs, moving)
            results = stage.compute(values)
            return {
                agg._chained: combined,
                **{name: results[key] for name, key in agg._targets.items()},
            }

        return call


def _expect(spec: WorkflowSpec, part: str, names: AbstractSet[str]) -> None:
    """Refuse a spec whose fields are not the ones the graph says they are."""
    declared = set(getattr(spec, part).model_fields)
    if declared != names:
        raise ValueError(
            f'{spec.id} declares {part} {sorted(declared)}, but the pipeline says '
            f'{sorted(names)}'
        )

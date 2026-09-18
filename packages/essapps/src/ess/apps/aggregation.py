# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a workflow with a declared contribution (D15).

A workflow that declares a contribution has three entry points instead of one:
``contribute``, from the parameters of one member to the values at the
accumulation keys; ``combine``, from contributions to one contribution; and
``finalize``, from a contribution and the finalize parameters to the outputs.
The single callable of D8 is contribute then finalize.

The wrapper holds two things. A :py:class:`sciline.Aggregation`, built from the
accumulation keys, an accumulator factory per key, and the member keys, serves
``contribute`` and ``combine``. A :py:class:`sciline.Stage` serves ``finalize``:
its inputs are the accumulation keys together with the keys of the finalize
parameters, so one call takes the combined contribution and the parameter
values, and its frontier holds everything that depends on neither. Both stages
are built once; the expensive work sits upstream of the accumulation keys, so
nothing is gained by holding a finalize parameter on the pipeline.

The wrapper maps between field names and sciline keys as
:py:class:`ess.apps.adapter.PipelineAdapter` does, and carries the contribution as a
scipp data group keyed by field name, so that it is stored and referenced like
any other array output.

At bind time the wrapper reads the split back off the stages and refuses a spec
whose declaration disagrees with the graph: a parameter finalize is declared to
read but contribute also reads is contribute's, because changing it invalidates
the contributions, an accumulation key that does not depend on the members is
not a contribution at all, and the finalize stage refuses a parameter the
outputs do not need.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import sciline
import scipp as sc
from pydantic import BaseModel

from .adapter import Key
from .binding import Form, Inputs, resolve

Contribution = sc.DataGroup
"""The contribution as the spec declares it: one field per accumulation key."""


class AggregatePipeline:
    """
    The three entry points over a sciline pipeline.

    ``keys`` maps parameter field names to sciline keys, ``targets`` output
    field names to the keys finalize computes, and ``resolve`` names the form
    each data-reference parameter takes, all as
    :py:class:`ess.apps.adapter.PipelineAdapter` takes them. ``contribution`` is the
    output field the spec marks, ``accumulation_keys`` maps the fields of that
    data group to the sciline keys at which contributions are combined, and
    ``finalize_params`` is the spec's declaration of which parameters finalize
    reads; the rest are the member keys.

    Each accumulation key is accumulated by ``sciline.Buffered(combine)``, unless
    ``accumulators`` gives a factory for it, which is where a running total
    belongs when a sum over large dense arrays should hold one array rather than
    one per member.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        keys: Mapping[str, Key],
        targets: Mapping[str, Key],
        resolve: Mapping[str, Form] = {},
        contribution: str,
        accumulation_keys: Mapping[str, Key],
        combine: Callable[..., Any],
        accumulators: Mapping[str, Callable[[], Any]] | None = None,
        finalize_params: Iterable[str] = (),
    ) -> None:
        if not targets:
            raise ValueError('a workflow with a contribution needs outputs')
        self._pipeline = pipeline
        self._keys = dict(keys)
        self._targets = dict(targets)
        self._resolve = dict(resolve)
        self.contribution = contribution
        self._accumulation_keys = dict(accumulation_keys)
        factories = dict(accumulators or {})
        self._accumulators = {
            key: factories.get(name, sciline.Buffered(combine))
            for name, key in self._accumulation_keys.items()
        }
        self._finalize_params = set(finalize_params)
        unknown = self._finalize_params - self._keys.keys()
        if unknown:
            raise ValueError(f'finalize parameters without a key: {sorted(unknown)}')
        self._members = [n for n in self._keys if n not in self._finalize_params]
        self._check_members()
        self._aggregation = sciline.Aggregation(
            self._pipeline,
            members=[self._keys[name] for name in self._members],
            accumulators=self._accumulators,
        )
        self._check_contribution()
        self._finalizer = sciline.Stage(
            self._pipeline,
            outputs=list(self._targets.values()),
            inputs=[
                *self._accumulation_keys.values(),
                *(self._keys[name] for name in self._finalize_params),
            ],
        )

    def _check_members(self) -> None:
        """Every parameter that is contribute's must reach the accumulation keys."""
        reaches = sciline.Stage(
            self._pipeline,
            outputs=list(self._accumulation_keys.values()),
            inputs=(),
        ).keys
        outside = [n for n in self._members if self._keys[n] not in reaches]
        if outside:
            raise ValueError(
                f'the contribution does not depend on {sorted(outside)}, which the '
                "spec leaves to contribute; declare them as finalize's parameters"
            )

    def _check_contribution(self) -> None:
        """What the contribute stage says about the declared split."""
        static = sorted(
            name
            for name, key in self._accumulation_keys.items()
            if key not in self._aggregation.accumulation_keys
        )
        if static:
            raise ValueError(
                f'the accumulation keys {static} do not depend on the members, so '
                'they are computed once and never combined; they are not part of a '
                'contribution'
            )
        contribute_reads = sorted(
            name
            for name in self._finalize_params
            if self._keys[name] in self._aggregation.contribute_stage.keys
        )
        if contribute_reads:
            raise ValueError(
                f'{contribute_reads} are declared as the parameters finalize reads, '
                'but contribute reads them too, so changing one invalidates every '
                "contribution; they are contribute's"
            )

    def _values(self, params: BaseModel, inputs: Inputs, names: Iterable[str]) -> dict:
        return {
            self._keys[name]: resolve(
                getattr(params, name), self._resolve[name], inputs
            )
            if name in self._resolve
            else getattr(params, name)
            for name in names
        }

    def contribute(self, params: BaseModel, inputs: Inputs) -> Contribution:
        """The contribution of one member, at the accumulation keys."""
        row = self._values(params, inputs, self._members)
        return self._group(self._aggregation.contribute(row))

    def combine(self, contributions: Iterable[Contribution]) -> Contribution:
        """Combine contributions, or combinations of such, into one."""
        return self._group(
            self._aggregation.combine(self._keyed(c) for c in contributions)
        )

    def finalize(
        self, contribution: Contribution, params: Any, inputs: Inputs
    ) -> dict[str, Any]:
        """The outputs finalize computes from a contribution, by field name."""
        values = self._keyed(contribution)
        values.update(self._values(params, inputs, self._finalize_params))
        results = self._finalizer.compute(values)
        return {name: results[key] for name, key in self._targets.items()}

    def __call__(self, params: BaseModel, inputs: Inputs) -> dict[str, Any]:
        """The single callable of D8: contribute, then finalize."""
        contribution = self.contribute(params, inputs)
        finalized = self.finalize(contribution, params, inputs)
        return {self.contribution: contribution, **finalized}

    def _group(self, contribution: Mapping[Key, Any]) -> Contribution:
        return sc.DataGroup(
            {name: contribution[key] for name, key in self._accumulation_keys.items()}
        )

    def _keyed(self, contribution: Mapping[str, Any]) -> dict[Key, Any]:
        keys = self._accumulation_keys
        return {key: contribution[name] for name, key in keys.items()}

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec: :class:`PipelineBinding`.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any

import sciline
from pydantic import BaseModel

from ._workflow_spec import WorkflowSpec
from .binding import StageCall, fixed_values
from .field_types import optional, plain

Key = Any


class PipelineBinding[P: BaseModel, O: BaseModel]:
    """
    A sciline pipeline behind a spec: plain requests and stages.

    ``make_pipeline`` makes the pipeline from the fixed values, by name, with
    defaults filled in, so that the graph may depend on them; it gets no
    blank, and each call must return a pipeline of its own, such as a copy.
    Each parameter sets the sciline key of its type as written, without
    ``Annotated`` metadata, such as ``Reduction`` or ``BeamCentre | None``; a
    table sets the key of its type, a list, to its list. Each output is
    computed as the key of its type without ``| None``, since an optional
    output means that the graph may lack it, not that a provider returns
    ``None``; an output that a provider returns as ``X | None`` needs
    ``keys=``. ``keys`` names another key of a parameter or an output, for a
    workflow with keys of its own, such as for two parameters of one type; for
    a parameter, ``None`` sets no key, for a field that ``make_pipeline`` uses
    itself::

        PipelineBinding(
            IOFQ,
            make_pipeline,
            keys={'sample': Filename[SampleRun], 'iofq': BackgroundSubtractedIofQ},
        )

    :attr:`keys` and :meth:`pipeline` give these keys and the graph, so that
    the driver of a multi-run spec can reuse them.

    A stage cuts :meth:`pipeline` of the fixed values at the keys of the
    blanks with ``sciline.Stage``, which computes what does not depend on
    them on the first call and holds it. A plain request is a stage with no
    blanks, called once. ``make_pipeline`` gets no blank, so a field that
    changes the graph cannot be a blank. A key that no output depends on is
    not fed: its value cannot change the outputs. An optional output is
    computed if the pipeline has a value or a provider for its key, and is
    ``None`` otherwise. ``fixed`` and ``blanks`` are checked by
    ``fixed_values`` of :mod:`ess.spec.binding`, and a field without a key
    cannot be a blank.

    The author of the pipeline promises that it is pure: it modifies no value
    it is given, and returns the same results for the same values.
    """

    def __init__(
        self,
        spec: WorkflowSpec[P, O],
        make_pipeline: Callable[[Mapping[str, Any]], sciline.Pipeline],
        *,
        keys: Mapping[str, Key | None] | None = None,
    ) -> None:
        keys = dict(keys or {})
        declared = spec.params.model_fields
        outputs = spec.outputs.model_fields
        if unknown := keys.keys() - declared.keys() - outputs.keys():
            raise ValueError(f'{sorted(unknown)}: not fields of {spec.name}')
        if unset := [n for n in outputs if n in keys and keys[n] is None]:
            raise ValueError(f'{unset}: an output is computed as a key')
        all_keys = {
            **{n: keys.get(n, plain(f.annotation)) for n, f in declared.items()},
            **{n: keys.get(n, optional(f.annotation)[0]) for n, f in outputs.items()},
        }
        names: dict[Key, list[str]] = {}
        for name, key in all_keys.items():
            if key is not None:
                names.setdefault(key, []).append(name)
        if shared := [' and '.join(n) for n in names.values() if len(n) > 1]:
            raise ValueError(
                f'{spec.name}: {"; ".join(shared)}: two fields have one key'
            )
        self.spec = spec
        self._keys = MappingProxyType(all_keys)
        self._make_pipeline = make_pipeline

    @property
    def keys(self) -> Mapping[str, Key | None]:
        """
        The sciline key of each parameter and output, by name; ``None`` for a
        parameter that sets none.
        """
        return self._keys

    def pipeline(self, fixed: Mapping[str, Any]) -> sciline.Pipeline:
        """
        The pipeline ``make_pipeline`` makes from ``fixed``, by name, with each
        value of ``fixed`` set at its key: the graph that a stage cuts.

        A stage passes its fixed values with defaults filled in. A driver
        passes the fixed values it is made with: every value that is not a
        table, defaults filled in. So a field that is a table of the
        multi-run spec cannot shape the graph, as a blank cannot.
        """
        spec = self.spec
        if unknown := sorted(fixed.keys() - spec.params.model_fields.keys()):
            raise ValueError(f'{unknown}: not parameters of {spec.name}')
        pipeline = self._make_pipeline(fixed)
        for key, value in self._by_key(fixed).items():
            pipeline[key] = value
        return pipeline

    def compute(self, params: P) -> O:
        return self.make_stage(dict(params), ())({})

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[O]:
        values = fixed_values(self.spec, fixed, blanks)
        left_open = frozenset(blanks)
        if keyless := sorted(n for n in left_open if self._keys[n] is None):
            raise ValueError(f'{keyless}: without a key, cannot be blanks')
        pipeline = self.pipeline(values)
        output_fields = self.spec.outputs.model_fields
        outputs = [
            self._keys[name]
            for name, field in output_fields.items()
            if field.is_required() or _computable(pipeline, self._keys[name])
        ]
        cut = _Cut(pipeline, outputs, [self._keys[n] for n in left_open])
        output_model = self.spec.outputs

        def call(blank_values: Mapping[str, Any]) -> O:
            if blank_values.keys() != left_open:
                raise ValueError(f'give the blanks {sorted(left_open)}')
            results = cut(self._by_key(blank_values))
            return output_model(
                **{n: results.get(self._keys[n]) for n in output_fields}
            )

        return call

    def _by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
        """``values`` by the key each sets; a field without a key is left out."""
        return {
            self._keys[n]: v for n, v in values.items() if self._keys[n] is not None
        }


def _computable(pipeline: sciline.Pipeline, key: Key) -> bool:
    """Whether the pipeline has a value or a provider for ``key``."""
    # sciline keeps a node's value or provider as an attribute of the node in
    # its graph, under the names 'value' and 'provider'.
    node = pipeline.underlying_graph.nodes.get(key, {})
    return 'value' in node or 'provider' in node


class _Cut:
    """A ``sciline.Stage`` of ``outputs``, fed those of ``inputs`` that they use."""

    def __init__(
        self, pipeline: sciline.Pipeline, outputs: Sequence[Key], inputs: Sequence[Key]
    ) -> None:
        if inputs:
            used = sciline.Stage(pipeline, outputs=outputs, inputs=()).keys
            inputs = [k for k in inputs if k in used]
        self._inputs = inputs
        self._stage = sciline.Stage(pipeline, outputs=outputs, inputs=inputs)

    def __call__(self, values: Mapping[Key, Any]) -> dict[Key, Any]:
        return self._stage.compute({k: values[k] for k in self._inputs})

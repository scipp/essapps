# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec: :class:`PipelineBinding`.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

import sciline
from pydantic import BaseModel

from ._workflow_spec import WorkflowSpec
from .binding import StageCall, by_name, fixed_values
from .field_types import optional

Key = Any


class PipelineBinding[P: BaseModel, I: BaseModel, O: BaseModel]:
    """
    A sciline pipeline behind a spec: plain requests and stages.

    ``make_pipeline`` makes the pipeline from the fixed values, by name, with
    defaults filled in, so that the graph may depend on them; it gets no
    blank, and each call must return a pipeline of its own, such as a copy.
    Each parameter and input sets the sciline key of its type as written,
    such as ``Reduction`` or ``CanFile | None``, and each output is computed
    as the key of its type without ``| None``. A table sets the key of each
    of its columns to its list. ``keys`` names another key of a parameter,
    an input, or an output, for a workflow with keys of its own; for a
    parameter or an input, ``None`` sets no key, for a field that
    ``make_pipeline`` uses itself::

        PipelineBinding(
            IOFQ,
            make_pipeline,
            keys={'sample': Filename[SampleRun], 'iofq': BackgroundSubtractedIofQ},
        )

    A stage sets the fixed values on the pipeline and cuts it at the keys of
    the blanks with ``sciline.Stage``, which computes what does not depend on
    them on the first call and holds it. A plain request is a stage with no
    blanks, called once. ``make_pipeline`` gets no blank, so a field that
    changes the graph cannot be a blank. A key that no output depends on is not fed: its
    value cannot change the outputs. An optional output is computed if the
    pipeline has a value or a provider for its key, and is ``None``
    otherwise. ``fixed`` and ``blanks`` are checked by ``fixed_values`` of
    :mod:`ess.spec.binding`, and a field without a key cannot be a blank.

    The author of the pipeline promises that it is pure: it modifies no input,
    and returns the same results for the same inputs.
    """

    def __init__(
        self,
        spec: WorkflowSpec[P, I, O],
        make_pipeline: Callable[[Mapping[str, Any]], sciline.Pipeline],
        *,
        keys: Mapping[str, Key | None] | None = None,
    ) -> None:
        keys = dict(keys or {})
        declared = {
            **spec.params.model_fields,
            **spec.inputs.model_fields,
        }
        outputs = spec.outputs.model_fields
        if unknown := keys.keys() - declared.keys() - outputs.keys():
            raise ValueError(f'{sorted(unknown)}: not fields of {spec.name}')
        if unset := [n for n in outputs if n in keys and keys[n] is None]:
            raise ValueError(f'{unset}: an output is computed as a key')
        self._keys = {n: keys.get(n, f.annotation) for n, f in declared.items()}
        self._outputs = {
            n: keys.get(n, optional(f.annotation)[0]) for n, f in outputs.items()
        }
        self._required = {n for n, f in outputs.items() if f.is_required()}
        set_keys = [k for k in [*self._keys.values(), *self._outputs.values()] if k]
        if len(set(set_keys)) < len(set_keys):
            raise ValueError(f'{spec.name}: two fields have one key')
        self.spec = spec
        self._make_pipeline = make_pipeline

    def compute(self, params: P, inputs: I) -> O:
        return self.make_stage(by_name(self.spec, params, inputs), ())({})

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[O]:
        values = fixed_values(self.spec, fixed, blanks)
        blank_keys = list(self._by_key(dict.fromkeys(blanks), blanks=True))
        pipeline = self._make_pipeline(values)
        for key, value in self._by_key(values).items():
            pipeline[key] = value
        outputs = {
            name: key
            for name, key in self._outputs.items()
            if name in self._required or _computable(pipeline, key)
        }
        cut = _Cut(pipeline, tuple(outputs.values()), blank_keys)
        output_model = self.spec.outputs

        def call(blank_values: Mapping[str, Any]) -> O:
            if blank_values.keys() != set(blanks):
                raise ValueError(f'give the blanks {sorted(blanks)}')
            results = cut(self._by_key(blank_values))
            return output_model(**{n: results.get(k) for n, k in self._outputs.items()})

        return call

    def _by_key(
        self, values: Mapping[str, Any], *, blanks: bool = False
    ) -> dict[Key, Any]:
        """
        ``values`` by the key each sets, a table's columns each by its own;
        a field without a key is left out, or refused if ``blanks``.
        """
        flat: dict[str, Any] = {}
        for name, value in values.items():
            if name in self.spec.tables:
                columns = self.spec.columns(name)
                flat.update(value if not blanks else dict.fromkeys(columns))
            else:
                flat[name] = value
        if blanks and (keyless := sorted(n for n in flat if self._keys[n] is None)):
            raise ValueError(f'{keyless}: without a key, cannot be blanks')
        return {self._keys[n]: v for n, v in flat.items() if self._keys[n] is not None}


def _computable(pipeline: sciline.Pipeline, key: Key) -> bool:
    """Whether the pipeline has a value or a provider for ``key``."""
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec: :class:`PipelineBinding`.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

import sciline

from ._workflow_spec import WorkflowSpec
from .binding import fixed_values

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec: plain requests and stages.

    ``params`` maps each parameter to the sciline key it sets: a group or a
    table as a whole by its name, or each field of a group by its path, such as
    ``'sample.file'``. A group set to ``None`` sets each key of its paths to
    ``None``. ``outputs`` maps each output of the spec to the key that
    computes it, every required one included. A stage sets the fixed values on
    a copy of the pipeline and cuts it at the keys of the blanks with
    ``sciline.Stage``, which computes what does not depend on them on the
    first call and holds it. A plain request is a stage with no blanks, called
    once. A key that no output depends on is not fed: its value cannot change
    the outputs.

    The author of the pipeline promises that it is pure: it modifies no input,
    and returns the same results for the same inputs.
    """

    def __init__(
        self,
        spec: WorkflowSpec[Any],
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
    ) -> None:
        names = {path.split('.')[0] for path in params}
        if unknown := names - spec.params.model_fields.keys():
            raise ValueError(f'{sorted(unknown)}: not parameters of {spec.name}')
        declared = spec.outputs.model_fields
        if unknown := outputs.keys() - declared.keys():
            raise ValueError(f'{sorted(unknown)}: not outputs of {spec.name}')
        required = {n for n, f in declared.items() if f.is_required()}
        if missing := required - outputs.keys():
            raise ValueError(f'{sorted(missing)}: outputs of {spec.name} not mapped')
        self.spec = spec
        self._pipeline = pipeline
        self._keys = _Keys(params)
        self._outputs = dict(outputs)

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.make_stage(params, ())({})

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        values = fixed_values(self.spec, fixed, blanks)
        blank_keys = self._keys.of(blanks)
        pipeline = self._keys.set(self._pipeline, values)
        cut = _Cut(pipeline, tuple(self._outputs.values()), blank_keys)

        def call(blank_values: Mapping[str, Any]) -> dict[str, Any]:
            results = cut(self._keys.by_key(blank_values))
            return {name: results[key] for name, key in self._outputs.items()}

        return call


class _Keys:
    """The sciline keys of parameters, mapped as ``params`` of PipelineBinding."""

    def __init__(self, params: Mapping[str, Key]) -> None:
        keys: dict[str, dict[str, Key]] = defaultdict(dict)
        for path, key in params.items():
            keys[path.split('.')[0]][path] = key
        if both := [n for n, ks in keys.items() if n in ks and len(ks) > 1]:
            raise ValueError(f'{both}: mapped as a whole and by paths')
        self._keys = dict(keys)

    def of(self, names: Iterable[str]) -> list[Key]:
        """The keys that the parameters ``names`` set."""
        names = list(names)
        if unknown := set(names) - self._keys.keys():
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        return [key for name in names for key in self._keys[name].values()]

    def set(
        self, pipeline: sciline.Pipeline, values: Mapping[str, Any]
    ) -> sciline.Pipeline:
        """A copy of ``pipeline`` with ``values`` set."""
        pipeline = pipeline.copy()
        for key, value in self.by_key(values).items():
            pipeline[key] = value
        return pipeline

    def by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
        """``values`` by the sciline key each sets."""
        by_key: dict[Key, Any] = {}
        unknown: list[str] = []
        for name, value in values.items():
            keys = self._keys.get(name, {})
            if name in keys:
                by_key[keys[name]] = value
            elif keys and value is None:
                by_key.update(dict.fromkeys(keys.values()))
            elif keys and isinstance(value, Mapping):
                for field, v in value.items():
                    path = f'{name}.{field}'
                    if path in keys:
                        by_key[keys[path]] = v
                    else:
                        unknown.append(path)
            else:
                unknown.append(name)
        if unknown:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        return by_key


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

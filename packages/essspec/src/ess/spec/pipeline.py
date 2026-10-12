# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a binding.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sciline

from .binding import Function

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec.

    ``params`` maps each params field to the sciline key it sets: a group or a
    table as a whole by its name, or a field of a group by its path, such as
    ``'sample.run'``. ``outputs`` maps each output field to the key that
    computes it. A stage sets the fixed values on a copy of the pipeline and
    cuts it at the keys of the blanks with ``sciline.Stage``, which computes
    what does not depend on them on the first call and holds it. A key that no
    output depends on is not fed: its value cannot change the outputs, and the
    plain request succeeds too.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
    ) -> None:
        self._pipeline = pipeline
        self._params = dict(params)
        self._outputs = dict(outputs)

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        pipeline = self._pipeline.copy()
        for key, value in self._by_key(fixed).items():
            pipeline[key] = value
        blank_keys = {
            path: key
            for path, key in self._params.items()
            if path.split('.')[0] in blanks
        }
        if unknown := set(blanks) - {path.split('.')[0] for path in blank_keys}:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        targets = tuple(self._outputs.values())
        fed: tuple[Key, ...] = ()
        if blanks:
            used = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
            fed = tuple(key for key in blank_keys.values() if key in used)
        stage = sciline.Stage(pipeline, outputs=targets, inputs=fed)

        def call(**values: Any) -> dict[str, Any]:
            by_key = self._by_key(values)
            results = stage.compute({key: by_key[key] for key in fed})
            return {name: results[key] for name, key in self._outputs.items()}

        return call

    def _by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
        """
        ``values`` by the sciline key each sets: a field mapped as a whole,
        or each field of a group mapped by its path.
        """
        paths: dict[str, Any] = {}
        for name, value in values.items():
            if name not in self._params and isinstance(value, Mapping):
                paths.update({f'{name}.{field}': v for field, v in value.items()})
            else:
                paths[name] = value
        if unknown := paths.keys() - self._params.keys():
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        return {self._params[path]: value for path, value in paths.items()}

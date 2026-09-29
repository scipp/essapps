# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a binding.

This is the only module that imports sciline. It belongs in ``ess.reduce``,
where specs meet sciline workflows, and lives here while the binding protocol
settles.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sciline

from .bindings import Function

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec.

    ``params`` maps each params field to the sciline key it sets, and
    ``outputs`` each output field to the key that computes it. A stage sets the
    fixed values on a copy of the pipeline and cuts it at the blanks with
    ``sciline.Stage``, which computes what does not depend on the blanks on the
    first call and holds it. A blank that no output depends on is not fed:
    its value cannot change the outputs, and the plain request succeeds too.
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
        unknown = (fixed.keys() | set(blanks)) - self._params.keys()
        if unknown:
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        pipeline = self._pipeline.copy()
        for name, value in fixed.items():
            pipeline[self._params[name]] = value
        targets = tuple(self._outputs.values())
        used = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
        fed = {n: self._params[n] for n in blanks if self._params[n] in used}
        stage = sciline.Stage(pipeline, outputs=targets, inputs=tuple(fed.values()))

        def call(**values: Any) -> dict[str, Any]:
            results = stage.compute({key: values[n] for n, key in fed.items()})
            return {name: results[key] for name, key in self._outputs.items()}

        return call

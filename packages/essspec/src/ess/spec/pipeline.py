# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline as a binding.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

import sciline

from .binding import Function

Key = Any


class PipelineBinding:
    """
    A sciline pipeline behind a spec.

    ``params`` maps each parameter to the sciline key it sets: a group or a
    table as a whole by its name, or each field of a group by its path, such as
    ``'sample.file'``. A group set to ``None`` sets each key of its paths to
    ``None``. ``outputs`` maps each output field to the key that computes it. A
    stage sets the fixed values on a copy of the pipeline and cuts it at the
    keys of the blanks with ``sciline.Stage``, which computes what does not
    depend on them on the first call and holds it. A key that no output depends
    on is not fed: its value cannot change the outputs, and the plain request
    succeeds too.
    """

    def __init__(
        self,
        pipeline: sciline.Pipeline,
        *,
        params: Mapping[str, Key],
        outputs: Mapping[str, Key],
    ) -> None:
        self._pipeline = pipeline
        self._keys: dict[str, dict[str, Key]] = defaultdict(dict)
        for path, key in params.items():
            self._keys[path.split('.')[0]][path] = key
        if both := [n for n, keys in self._keys.items() if n in keys and len(keys) > 1]:
            raise ValueError(f'{both}: mapped as a whole and by paths')
        self._outputs = dict(outputs)

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        if unknown := set(blanks) - self._keys.keys():
            raise ValueError(f'no sciline key for the parameters {sorted(unknown)}')
        pipeline = self._pipeline.copy()
        for key, value in self._by_key(fixed).items():
            pipeline[key] = value
        targets = tuple(self._outputs.values())
        fed: tuple[Key, ...] = ()
        if blanks:
            used = sciline.Stage(pipeline, outputs=targets, inputs=()).keys
            blank_keys = (key for name in blanks for key in self._keys[name].values())
            fed = tuple(key for key in blank_keys if key in used)
        stage = sciline.Stage(pipeline, outputs=targets, inputs=fed)

        def call(**values: Any) -> dict[str, Any]:
            by_key = self._by_key(values)
            results = stage.compute({key: by_key[key] for key in fed})
            return {name: results[key] for name, key in self._outputs.items()}

        return call

    def _by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
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

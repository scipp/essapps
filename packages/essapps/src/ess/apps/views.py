# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Views: a small piece of an output for display, never the volume.

A view is a pure function of a value and a view spec, returning plain arrays, so
it can be served by whichever process holds a copy and later by a view worker.
Event data is never viewed.

See docs/developer/stages.md.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import scipp as sc
from pydantic import BaseModel, Field


class ViewSpec(BaseModel, frozen=True):
    """Select fixed indices along dims, then return what is left as plain arrays."""

    select: dict[str, int] = Field(default_factory=dict)
    max_points: int = Field(default=1_000_000, description="Refuse larger views.")


def view(value: Any, spec: ViewSpec) -> dict[str, Any]:
    data = value if isinstance(value, sc.DataArray) else sc.DataArray(value)
    if data.bins is not None:
        raise ValueError('Event data is never viewed; histogram it in the workflow')
    for dim, index in spec.select.items():
        data = data[dim, index]
    if np.prod(data.shape, dtype=int) > spec.max_points:
        raise ValueError(f'{data.shape} exceeds {spec.max_points} points')
    return {
        'dims': list(data.dims),
        'values': np.asarray(data.values),
        'unit': None if data.unit is None else str(data.unit),
        'coords': {
            name: np.asarray(coord.values)
            for name, coord in data.coords.items()
            if set(coord.dims) <= set(data.dims)
        },
    }

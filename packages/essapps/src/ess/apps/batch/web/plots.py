# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Scipp curves as a Plotly figure, a ``dict`` that Plotly.js draws as it is, so
no Plotly package is needed in Python.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
import scipp as sc


def _axis(var: sc.Variable, name: str, log: bool) -> dict[str, Any]:
    unit = '' if var.unit in (None, sc.units.dimensionless) else f' [{var.unit}]'
    return {
        'title': {'text': f'{name}{unit}'},
        'type': 'log' if log else 'linear',
        'exponentformat': 'power',
        'showline': True,
        'mirror': True,
        'ticks': 'outside',
    }


def _centres(coord: sc.Variable, size: int) -> np.ndarray:
    values = coord.values
    return (values[:-1] + values[1:]) / 2 if len(values) == size + 1 else values


def figure(
    curves: Mapping[str, sc.DataArray],
    *,
    log_x: bool = True,
    log_y: bool = True,
    y_name: str = 'Intensity',
) -> dict[str, Any]:
    """
    A figure of 1-D ``curves`` by legend name, overlaid, with error bars from
    their variances and the axes titled by name and unit. A coordinate of bin
    edges is drawn at the bin centres. Raises ``ValueError`` for a curve that
    is not 1-D, and for curves of other dimensions or units.
    """
    if not curves:
        raise ValueError('no curves to draw')
    first = next(iter(curves.values()))
    if any(c.ndim != 1 for c in curves.values()):
        raise ValueError('only 1-D curves are drawn')
    dim = first.dim
    if any(c.dim != dim or c.unit != first.unit for c in curves.values()):
        raise ValueError('the curves differ in their dimension or unit')
    traces = []
    for name, curve in curves.items():
        trace: dict[str, Any] = {
            'type': 'scatter',
            'mode': 'lines+markers',
            'name': name,
            'x': _centres(curve.coords[dim], curve.size).tolist(),
            'y': curve.values.tolist(),
            'marker': {'size': 4},
            'line': {'width': 1.5},
        }
        if curve.variances is not None:
            trace['error_y'] = {
                'type': 'data',
                'array': np.sqrt(curve.variances).tolist(),
                'thickness': 1,
                'width': 0,
            }
        traces.append(trace)
    return {
        'data': traces,
        'layout': {
            'xaxis': _axis(first.coords[dim], dim.capitalize(), log_x),
            'yaxis': _axis(first.data, y_name, log_y),
            'legend': {'orientation': 'v'},
        },
    }

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Scipp curves as a Plotly figure, a ``dict`` that Plotly.js draws as it is, so
no Plotly package is needed in Python.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import scipp as sc

PALETTE = (
    '#2f6fb3',
    '#d1495b',
    '#2e9d6c',
    '#e0902f',
    '#7a5bb5',
    '#1f9aa8',
    '#9c6b3f',
    '#c25a9c',
    '#5f6b7a',
    '#8aa832',
    '#b33f3f',
    '#3f5fb3',
)


def color(index: int) -> str:
    """The colour of the row at ``index``, the same in every figure."""
    return PALETTE[index % len(PALETTE)]


@dataclass(frozen=True)
class Curve:
    """A 1-D curve to draw, named ``name``, ``dashed`` if it is outdated."""

    name: str
    data: sc.DataArray
    color: str
    dashed: bool = False


def _unit(var: sc.Variable) -> str:
    return '' if var.unit in (None, sc.units.dimensionless) else str(var.unit)


def _axis(var: sc.Variable, name: str, values: np.ndarray) -> dict[str, Any]:
    unit = _unit(var)
    axis: dict[str, Any] = {
        'title': {'text': f'{name} [{unit}]' if unit else name},
        'type': 'log',
        'exponentformat': 'power',
        'showline': True,
        'mirror': True,
        'ticks': 'outside',
    }
    positive = values[np.isfinite(values) & (values > 0)]
    if positive.size and np.log10(positive.max() / positive.min()) >= 1:
        # A label at each decade alone, none at 2 or 5 between.
        axis |= {'dtick': 1, 'minor': {'ticks': 'outside', 'ticklen': 3}}
    return axis


def _centres(coord: sc.Variable, size: int) -> np.ndarray:
    values = coord.values
    return (values[:-1] + values[1:]) / 2 if len(values) == size + 1 else values


def _listed(values: np.ndarray) -> list[float | None]:
    """``values`` for JSON, which has no NaN or infinity: ``None`` there."""
    return [float(v) if np.isfinite(v) else None for v in values]


def figure(curves: Sequence[Curve], *, quantity: str = 'Intensity') -> dict[str, Any]:
    """
    A figure of 1-D ``curves``, overlaid on log axes, with error bars from
    their variances and the axes titled by name and unit. ``quantity`` names
    the values; ``layout.meta.quantity`` holds it for a page's title. A
    coordinate of bin edges is drawn at the bin centres. Raises ``ValueError``
    for no curves, a curve that is not 1-D, and curves of other dimensions or
    units.
    """
    if not curves:
        raise ValueError('no curves to draw')
    first = curves[0].data
    if any(c.data.ndim != 1 for c in curves):
        raise ValueError('only 1-D curves are drawn')
    dim = first.dim
    if any(c.data.dim != dim or c.data.unit != first.unit for c in curves):
        raise ValueError('the curves differ in their dimension or unit')
    x_unit, y_unit = _unit(first.coords[dim]), _unit(first.data)

    def hover(errors: bool) -> str:
        spread = ' ± %{customdata:.2g}' if errors else ''
        return (
            f'%{{fullData.name}}<br>{dim} = %{{x:.3g}} {x_unit}<br>'
            f'{quantity} = %{{y:.4g}}{spread} {y_unit}<extra></extra>'
        )

    traces = []
    for curve in curves:
        data = curve.data
        errors = np.sqrt(data.variances) if data.variances is not None else None
        trace: dict[str, Any] = {
            'type': 'scatter',
            'mode': 'lines+markers',
            'name': curve.name,
            'showlegend': True,
            'x': _listed(_centres(data.coords[dim], data.size)),
            'y': _listed(data.values),
            'hovertemplate': hover(errors is not None),
            'marker': {'size': 4, 'color': curve.color},
            'line': {
                'width': 1.5,
                'color': curve.color,
                'dash': 'dash' if curve.dashed else 'solid',
            },
            'opacity': 0.55 if curve.dashed else 1.0,
        }
        if errors is not None:
            trace['customdata'] = _listed(errors)
            trace['error_y'] = {
                'type': 'data',
                'array': _listed(errors),
                'thickness': 1,
                'width': 2,
                'color': curve.color,
            }
        traces.append(trace)
    x = np.concatenate([c.data.coords[dim].values for c in curves])
    y = np.concatenate([c.data.values for c in curves])
    return {
        'data': traces,
        'layout': {
            'xaxis': _axis(first.coords[dim], dim, x),
            'yaxis': _axis(first.data, quantity, y),
            'meta': {'quantity': quantity},
        },
    }

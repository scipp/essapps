# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The provenance of a record as a graph: the record, each record it read
through all references (``client.provenance``), and their files, with an
arrow from each to the records whose requests name it.

Files are in the first column, and each record is a column to the right of
the rightmost of what it reads, so arrows point right; :func:`layout`
places the nodes in their columns. A record is coloured
by its status, so the graph shows which records are pending and where a
failure started. Clicking a node shows it: a record as
:func:`~ess.apps.panel.records.describe` does, a file by its metadata.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Hashable, Mapping
from itertools import pairwise
from statistics import mean
from typing import Any

import panel as pn
from bokeh.models import Arrow, ColumnDataSource, LabelSet, VeeHead
from bokeh.plotting import figure
from pydantic_core import to_jsonable_python

from ess.apps import Client, Record, Status
from ess.spec import FileRef
from ess.spec.refs import iter_refs

from .listing import Listing
from .records import describe

type Node = Record | FileRef
type Point = tuple[float, float]

_COLORS = {
    Status.PENDING: '#e8a33d',
    Status.COMPLETED: '#4c9a5e',
    Status.FAILED: '#c8453b',
    Status.CANCELLED: '#999999',
}
_FILE_COLOR = '#5b7fb8'
_EDGE_COLOR = '#888888'


class Graph:
    """
    The provenance graph of the record shown, and below it the node
    picked, by clicking it or with :meth:`pick`.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._record: Record | None = None
        self._picked: Node | None = None
        self.nodes: list[Node] = []
        self.pane = pn.pane.Bokeh(sizing_mode='stretch_width')
        self.detail = pn.pane.Markdown('', sizing_mode='stretch_width')

    def show(self, record: Record, listing: Listing) -> None:
        """
        Show the provenance of ``record``. The node picked stays so if it is
        in the graph.
        """
        self._record = record
        self._listing = listing
        provenance = self._client.provenance(record)
        records = [record, *provenance.records()]
        # What each record reads: a file, or a record by its id.
        reads = {
            r.id: [
                ref if isinstance(ref, FileRef) else ref.record
                for _, ref in iter_refs(r.request.params)
            ]
            for r in records
        }
        column: dict[Any, int] = dict.fromkeys(provenance.files(), 0)

        def place(key: Any) -> int:
            if key not in column:
                column[key] = 1 + max(map(place, reads[key]), default=0)
            return column[key]

        for r in records:
            place(r.id)
        self.nodes = [*provenance.files(), *reversed(records)]
        keys = [n if isinstance(n, FileRef) else n.id for n in self.nodes]
        edges = list(dict.fromkeys((k, r.id) for r in records for k in reads[r.id]))
        at, paths = layout({k: column[k] for k in keys}, edges)
        self.pane.object = self._figure([at[k] for k in keys], paths)
        picked = self._picked if self._picked in self.nodes else record
        self.pick(picked)

    def refresh(self, listing: Listing) -> None:
        """Show the record shown again, with the statuses of ``listing``."""
        if self._record is not None:
            self.show(self._record, listing)

    def pick(self, node: Node) -> None:
        """Show a node of the graph below it."""
        self._picked = node
        if isinstance(node, Record):
            self.detail.object = describe(self._client, self._listing, node)
        else:
            metadata = to_jsonable_python(self._client.files.metadata(node))
            self.detail.object = (
                f'### {self._listing.file_label(node)}\n\n'
                f'```json\n{json.dumps(metadata, indent=2)}\n```'
            )

    def _label(self, node: Node) -> str:
        if isinstance(node, FileRef):
            return self._listing.file_label(node)
        return self._listing.labels[node.id]

    def _color(self, node: Node) -> str:
        if isinstance(node, FileRef):
            return _FILE_COLOR
        return _COLORS[self._listing.status[node.id]]

    def _figure(self, at: list[Point], paths: list[list[Point]]) -> Any:
        x = [p[0] for p in at]
        y = [p[1] for p in at]
        source = ColumnDataSource(
            {
                'x': x,
                'y': y,
                'label': [self._label(n) for n in self.nodes],
                'color': [self._color(n) for n in self.nodes],
                'marker': [
                    'square' if isinstance(n, FileRef) else 'circle' for n in self.nodes
                ],
            }
        )
        ys = [p[1] for path in paths for p in path]
        fig = figure(
            height=120 + 60 * round(max(ys) - min(ys)),
            sizing_mode='stretch_width',
            tools='tap',
            toolbar_location=None,
            x_range=(-0.5, max(x) + 0.5),
            y_range=(min(ys) - 0.5, max(ys) + 0.7),
        )
        fig.axis.visible = False
        fig.grid.visible = False
        fig.outline_line_color = None
        # Each edge ends short of the node it points to, so the head shows.
        ends = [_short_of(path[-2], path[-1]) for path in paths]
        fig.multi_line(
            [
                [p[0] for p in path[:-1]] + [e[0]]
                for path, e in zip(paths, ends, strict=True)
            ],
            [
                [p[1] for p in path[:-1]] + [e[1]]
                for path, e in zip(paths, ends, strict=True)
            ],
            line_color=_EDGE_COLOR,
        )
        fig.add_layout(
            Arrow(
                end=VeeHead(size=8, line_color=_EDGE_COLOR, fill_color=_EDGE_COLOR),
                line_alpha=0,
                source=ColumnDataSource(
                    {
                        'x0': [path[-2][0] for path in paths],
                        'y0': [path[-2][1] for path in paths],
                        'x1': [e[0] for e in ends],
                        'y1': [e[1] for e in ends],
                    }
                ),
                x_start='x0',
                y_start='y0',
                x_end='x1',
                y_end='y1',
            )
        )
        fig.scatter('x', 'y', source=source, size=16, color='color', marker='marker')
        fig.add_layout(
            LabelSet(
                x='x',
                y='y',
                text='label',
                source=source,
                y_offset=10,
                text_align='center',
                text_font_size='9pt',
            )
        )
        source.selected.on_change(
            'indices', lambda _, __, new: self.pick(self.nodes[new[0]]) if new else None
        )
        return fig

    def panel(self) -> Any:
        return pn.Column(self.pane, self.detail, sizing_mode='stretch_width')


def layout[K: Hashable](
    column: Mapping[K, int], edges: list[tuple[K, K]], sweeps: int = 4
) -> tuple[dict[K, Point], list[list[Point]]]:
    """
    Where each node is drawn, in its column, and the path of each edge, as a
    layered graph is drawn (Sugiyama).

    An edge points from a column to one further right. Where it spans more
    than one, it bends at a point in each column between, which takes a
    place there as a node does, so that it passes beside the nodes of that
    column, not through them. The places of each column are ordered by the
    mean place of their neighbours in the column to the left, then to the
    right, ``sweeps`` times, which removes most crossings of edges. A column
    is centred on 0.

    Parameters
    ----------
    column:
        The column of each node.
    edges:
        Each edge, from a node to one in a column further right.

    Returns
    -------
    :
        The point of each node, and the path of each edge, from its first
        node, through its bends, to its second.
    """
    layers: list[list[Any]] = [[] for _ in range(max(column.values()) + 1)]
    for key, c in column.items():
        layers[c].append(key)
    chains = []
    for i, (a, b) in enumerate(edges):
        bends = [('bend', i, c) for c in range(column[a] + 1, column[b])]
        for bend in bends:
            layers[bend[2]].append(bend)
        chains.append([a, *bends, b])
    left: dict[Any, list[Any]] = defaultdict(list)
    right: dict[Any, list[Any]] = defaultdict(list)
    for chain in chains:
        for a, b in pairwise(chain):
            right[a].append(b)
            left[b].append(a)

    def order(
        layer: list[Any], by: list[Any], neighbours: dict[Any, list[Any]]
    ) -> None:
        place = {k: i for i, k in enumerate(by)}
        mine = {k: i for i, k in enumerate(layer)}
        # A place without neighbours there keeps its place.
        layer.sort(
            key=lambda k: (
                mean(place[n] for n in neighbours[k]) if neighbours[k] else mine[k]
            )
        )

    for _ in range(sweeps):
        for c in range(1, len(layers)):
            order(layers[c], layers[c - 1], left)
        for c in range(len(layers) - 2, -1, -1):
            order(layers[c], layers[c + 1], right)
    at = {
        k: (float(c), (len(layer) - 1) / 2 - i)
        for c, layer in enumerate(layers)
        for i, k in enumerate(layer)
    }
    return {k: at[k] for k in column}, [[at[k] for k in chain] for chain in chains]


def _short_of(start: Point, end: Point, by: float = 0.06) -> Point:
    """A point short of ``end``, by a fraction ``by`` of the way to ``start``."""
    return (end[0] - by * (end[0] - start[0]), end[1] - by * (end[1] - start[1]))

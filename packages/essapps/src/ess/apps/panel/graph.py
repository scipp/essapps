# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The provenance of a record as a graph: the record, each record it read
through all references (``client.provenance``), and their files, with an
arrow from each to the records whose requests name it.

Files are in the first column, and each record is a column to the right of
the rightmost of what it reads, so arrows point right. A record is coloured
by its status, so the graph shows which records are pending and where a
failure started. Clicking a node shows it: a record as
:func:`~ess.apps.panel.records.describe` does, a file by its metadata.
"""

from __future__ import annotations

import json
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

_COLORS = {
    Status.PENDING: '#e8a33d',
    Status.COMPLETED: '#4c9a5e',
    Status.FAILED: '#c8453b',
    Status.CANCELLED: '#999999',
}
_FILE_COLOR = '#5b7fb8'


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
        rows: dict[int, int] = {}
        x, y = [], []
        for key in keys:
            x.append(column[key])
            y.append(-rows.setdefault(column[key], 0))
            rows[column[key]] += 1
        at = dict(zip(keys, zip(x, y, strict=True), strict=True))
        edges = [(at[k], at[r.id]) for r in records for k in reads[r.id]]
        self.pane.object = self._figure(x, y, edges)
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

    def _figure(
        self,
        x: list[int],
        y: list[int],
        edges: list[tuple[tuple[int, int], tuple[int, int]]],
    ) -> Any:
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
        fig = figure(
            height=120 + 60 * (1 - min(y)),
            sizing_mode='stretch_width',
            tools='tap',
            toolbar_location=None,
            x_range=(-0.5, max(x) + 0.5),
            y_range=(min(y) - 0.5, 0.7),
        )
        fig.axis.visible = False
        fig.grid.visible = False
        fig.outline_line_color = None
        fig.add_layout(
            Arrow(
                end=VeeHead(size=8),
                line_color='#888888',
                source=ColumnDataSource(
                    {
                        'x0': [a[0] for a, _ in edges],
                        'y0': [a[1] for a, _ in edges],
                        'x1': [b[0] - 0.06 for _, b in edges],
                        'y1': [b[1] for _, b in edges],
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

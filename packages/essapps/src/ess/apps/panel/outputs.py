# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The outputs of the client's records, by type: what a scientist picks, to
plot it, to see where it came from, or to remove it.

Each output is to be a group, so that its type says what it is,
``BeamCentre``, where an array's type says only ``DataArray``. An output is
listed by its type and the time its record was created,
``BeamCentre (11:45)``, as the pickers of a form offer it (:class:`Listing`).

What an output is comes from the record's spec: the output's description,
else the first paragraph of the docstring of its type. The value is read only
for the output selected, once its record has completed, since a read copies
it whole: the client has no call that tells an output's sizes and unit
without its value. Where it came from is the provenance graph
(:class:`~ess.apps.panel.graph.Graph`), which shows the records pending and
where a failure started.

The output selected is shown with its plot or its provenance graph, as
picked; the pick stays as other outputs are selected. Each array field of
an output is plotted with plopp, in a figure of its own, once its record
has completed. The spec declares no number of dimensions, so an array of
none, or of more than two, is found only once read, and is not plotted.

Removing an output releases its record (``client.release``), which drops the
value of each of the record's outputs, and cancels it if pending; the
record stays in the proposal's history, and in the graphs of the records
that read it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import panel as pn
import plopp as pp
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ess.apps import Client, Record, Status
from ess.spec import field_types

from .graph import Graph
from .listing import Listing
from .table import Table


class Outputs:
    """
    A table of the outputs of the client's records, grouped by type, newest
    first, and below it the output selected, with a button that removes it,
    and its plot or its provenance graph, as ``views`` picks.

    An output is listed while a request of the client may reference it, so
    while its record is pending or completed, and when its record has failed,
    until removed. ``refresh`` is called once an output is removed; by
    default it lists the outputs again.
    """

    def __init__(
        self,
        client: Client,
        listing: Listing,
        refresh: Callable[[], object] | None = None,
    ) -> None:
        self._client = client
        self._refresh = refresh or (lambda: self.refresh(Listing(client)))
        self._removed: set[str] = set()
        self.table = Table[tuple[str, str]](  # by record id and output name
            ['Type', 'Output', 'Status'],
            self._show,
            groupby=['Type'],
            hidden_columns=['Type'],
        )
        self.detail = pn.pane.Markdown('', sizing_mode='stretch_width')
        self.views = pn.widgets.RadioButtonGroup(options=['Plot', 'Provenance'])
        self.views.param.watch(lambda _: self._show_view(), 'value')
        self.remove_button = pn.widgets.Button(
            label='Remove',
            disabled=True,
            description="Drop the value of each of the record's outputs",
        )
        self.remove_button.on_click(lambda _: self.remove())
        self.graph = Graph(client)
        self.view = pn.Column(sizing_mode='stretch_width')
        self.refresh(listing)

    @property
    def selected(self) -> tuple[Record, str] | None:
        """The record and the name of the output selected, if one is."""
        if (selected := self.table.selected) is None:
            return None
        record, name = selected
        return self._records[record], name

    def select(self, record: Record, name: str) -> None:
        """Select the output ``name`` of ``record``."""
        self.table.select((record.id, name))

    def refresh(self, listing: Listing) -> None:
        """
        List the outputs again. The output selected stays so if listed, and
        is shown again, which draws its plots again, only if its status
        changed. A provenance graph is drawn again in any case.
        """
        self._listing = listing
        self._records = {r.id: r for r in listing.records}
        self.table.set(
            {
                (r.id, n): {
                    'Type': listing.type_name(r, n),
                    'Output': listing.output_label(r.ref(n)),
                    'Status': str(listing.status[r.id]),
                }
                for r in reversed(listing.records)
                if r.id not in self._removed
                for n in r.outputs
                if n in listing.readable[r.id] or listing.status[r.id] is Status.FAILED
            }
        )
        if self.views.value == 'Provenance':
            self.graph.refresh(listing)

    def _show(self) -> None:
        """Show the output selected, and its view; nothing if none is."""
        selected = self.selected
        self.remove_button.disabled = selected is None
        if selected is None:
            self.detail.object = ''
        else:
            self.detail.object = self._describe(*selected)
        self._show_view()

    def _describe(self, record: Record, name: str) -> str:
        listing = self._listing
        info = listing.specs[record.id].outputs.model_fields[name]
        status = listing.status[record.id]
        lines = [f'### {listing.output_label(record.ref(name))}: {status}']
        if about := _about(info):
            lines.append(about)
        lines.append(f'**Record** {listing.labels[record.id]}')
        if status is Status.FAILED:
            lines.append(f'**Failure** {self._client.failure(record)}')
        elif status is Status.COMPLETED:
            value = self._client.output(record, name)
            lines.append(f'**Value**\n\n{_summary(value)}')
        return '\n\n'.join(lines)

    def _show_view(self) -> None:
        """
        The provenance graph of the output selected, or each of its arrays of
        one or two dimensions plotted, once its record has completed.
        """
        self.view.objects = []
        if (selected := self.selected) is None:
            return
        record, name = selected
        if self.views.value == 'Provenance':
            self.graph.show(record, self._listing)
            self.view.objects = [self.graph.panel()]
            return
        info = self._listing.specs[record.id].outputs.model_fields[name]
        completed = self._listing.status[record.id] is Status.COMPLETED
        if not completed or not field_types.has_array(info.annotation):
            return
        value = self._client.output(record, name)
        label = self._listing.output_label(record.ref(name))
        arrays = (
            {f'{label} {n}': v for n, v in value if hasattr(v, 'dims')}
            if isinstance(value, BaseModel)
            else {label: value}
        )
        self.view.objects = [_figure(title, array) for title, array in arrays.items()]

    def remove(self) -> None:
        """Release the record of the output selected, and stop listing it."""
        if (selected := self.selected) is None:
            return
        record, _ = selected
        self._client.release(record)
        self._removed.add(record.id)
        self._refresh()

    def panel(self) -> Any:
        return pn.Column(
            self.table.widget,
            self.detail,
            pn.Row(self.views, self.remove_button),
            self.view,
            sizing_mode='stretch_width',
        )


def _about(info: FieldInfo) -> str | None:
    """
    The description of an output, or the first paragraph of the docstring of
    its type if that is a group.
    """
    if info.description:
        return info.description
    tp = field_types.value_type(info.annotation)
    if field_types.is_group(tp) and tp.__doc__:
        return inspect.cleandoc(tp.__doc__).split('\n\n')[0]
    return None


def _summary(value: Any) -> str:
    """A list of the fields of a group, or of the one value, each in brief."""
    if isinstance(value, BaseModel):
        return '\n'.join(f'- {n}: {_brief(v)}' for n, v in value)
    return f'- {_brief(value)}'


def _brief(value: Any) -> str:
    """An array by its sizes, unit, and coordinates; a 0-D one by its value."""
    if not hasattr(value, 'dims'):
        return repr(value)
    unit = '' if getattr(value, 'unit', None) is None else f' [{value.unit}]'
    if not value.dims:
        return f'{value.value}{unit}'
    sizes = ', '.join(f'{d}: {n}' for d, n in zip(value.dims, value.shape, strict=True))
    coords = getattr(value, 'coords', {})
    names = f', coordinates {", ".join(coords)}' if coords else ''
    return f'array ({sizes}){unit}{names}'


def _figure(title: str, array: Any) -> Any:
    if array.ndim not in (1, 2):
        return pn.pane.Markdown(f'{title}: {array.ndim}-D, not plotted')
    return pn.pane.Matplotlib(pp.plot(array, title=title).fig, tight=True)

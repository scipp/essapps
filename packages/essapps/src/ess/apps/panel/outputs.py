# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The outputs of the client's records: a table with a row per output, and the
output selected, what it is, how it was made, and its value.

What an output is comes from the record's spec: the output's title, type,
and description, else the first paragraph of the docstring of a group type.
How it was made comes from the record: its request, and the records and
files it read (``client.provenance``). The value is read only for the output
selected, once its record has completed, since a read copies it whole: the
client has no call that tells an output's sizes and unit without its value.

An output declared as an array, or as a group with an array field
(:func:`ess.spec.field_types.has_array`), can be plotted with plopp, each
array of it in a figure of its own. The spec declares no number of
dimensions, so an array of none, or of more than two, is found only once
read, and is not plotted.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pandas as pd
import panel as pn
import plopp as pp
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python

from ess.apps import Client, Record, Status
from ess.spec import field_types, places

from .form import Listing


class Outputs:
    """
    A table with a row per output of each of the client's records, newest
    first, and below it the output selected, with a button that plots it.

    A row's status is its record's, or ``not readable`` for an output that a
    request of the client may not reference, of a record that has not
    failed: one of another client's records, not persisted, or an optional
    output that the workflow did not return.
    """

    def __init__(self, client: Client, listing: Listing) -> None:
        self._client = client
        self._rows: list[tuple[Record, str]] = []
        self._status: dict[str, Status] = {}
        self.table = pn.widgets.Tabulator(
            disabled=True,
            selectable=1,
            sortable=False,
            show_index=False,
            layout='fit_columns',
            sizing_mode='stretch_width',
        )
        self.table.param.watch(lambda _: self._show(), 'selection')
        self.detail = pn.pane.Markdown('', sizing_mode='stretch_width')
        self.button = pn.widgets.Button(label='Plot', disabled=True)
        self.button.on_click(lambda _: self.plot())
        self.plots = pn.Column(sizing_mode='stretch_width')
        self.refresh(listing)

    @property
    def selected(self) -> tuple[Record, str] | None:
        """The record and the name of the output selected, if one is."""
        selection = self.table.selection
        return self._rows[selection[0]] if len(selection) == 1 else None

    @property
    def pending(self) -> list[Record]:
        """The records that were pending at the last refresh."""
        return [
            r for r in self._listing.records if self._status[r.id] is Status.PENDING
        ]

    def select(self, record: Record, name: str) -> None:
        """Select the output ``name`` of ``record``."""
        self.table.selection = [self._rows.index((record, name))]

    def refresh(self, listing: Listing) -> None:
        """
        List the outputs again. The output selected stays so if listed, and
        is shown again, which drops its plots, only if its status changed.
        """
        selected = self.selected
        shown = None if selected is None else self._row_status(*selected)
        self._listing = listing
        statuses = self._client.status(listing.records)
        self._status = {r.id: s for r, s in zip(listing.records, statuses, strict=True)}
        self._rows = [(r, n) for r in reversed(listing.records) for n in r.outputs]
        self.table.value = pd.DataFrame(
            {
                'Record': [listing.labels[r.id] for r, _ in self._rows],
                'Output': [n for _, n in self._rows],
                'Type': [self._type(r, n) for r, n in self._rows],
                'Status': [self._row_status(r, n) for r, n in self._rows],
            },
            dtype=str,
        )
        index = [self._rows.index(selected)] if selected in self._rows else []
        if self.table.selection != index:
            self.table.selection = index  # which shows it
        elif selected is not None and self._row_status(*selected) != shown:
            self._show()

    def _info(self, record: Record, name: str) -> FieldInfo:
        return self._listing.specs[record.id].outputs.model_fields[name]

    def _type(self, record: Record, name: str) -> str:
        return field_types.type_name(self._info(record, name).annotation)

    def _readable(self, record: Record, name: str) -> bool:
        return name in self._listing.readable[record.id]

    def _row_status(self, record: Record, name: str) -> str:
        status = self._status[record.id]
        if status in (Status.FAILED, Status.CANCELLED) or self._readable(record, name):
            return str(status)
        return 'not readable'

    def _show(self) -> None:
        """Show the output selected; nothing if none is."""
        self.plots.objects = []
        self.button.disabled = True
        if (selected := self.selected) is None:
            self.detail.object = ''
            return
        record, name = selected
        listing = self._listing
        spec = listing.specs[record.id]
        info = self._info(record, name)
        status = self._row_status(record, name)
        lines = [f'### {places.title(name, info)}: `{self._type(record, name)}`']
        if about := _about(info):
            lines.append(about)
        lines += [
            f'**Record** {listing.labels[record.id]}, {status}, by '
            f'{record.submitter} at {record.created:%Y-%m-%d %H:%M:%S}',
            f'**Spec** {spec.title} (`{record.spec}`): {spec.description}',
        ]
        provenance = self._client.provenance(record)
        if upstream := provenance.records():
            labels = (listing.labels.get(r.id, r.id[:6]) for r in upstream)
            lines.append('**Reads** ' + '; '.join(labels))
        if files := provenance.files():
            lines.append('**Files** ' + ', '.join(map(listing.file_label, files)))
        params = to_jsonable_python(record.request.params, by_alias=True)
        lines.append(f'**Params**\n```json\n{json.dumps(params, indent=2)}\n```')
        if self._status[record.id] in (Status.FAILED, Status.CANCELLED):
            lines.append(f'**Failure** {self._client.failure(record)}')
        elif self._status[record.id] is Status.COMPLETED and self._readable(
            record, name
        ):
            value = self._client.output(record, name)
            lines.append(f'**Value**\n\n{_summary(value)}')
            self.button.disabled = not field_types.has_array(info.annotation)
        self.detail.object = '\n\n'.join(lines)

    def plot(self) -> None:
        """Plot each array of the output selected, of one or two dimensions."""
        if (selected := self.selected) is None:
            return
        record, name = selected
        value = self._client.output(record, name)
        arrays = (
            {f'{name}.{n}': v for n, v in value if hasattr(v, 'dims')}
            if isinstance(value, BaseModel)
            else {name: value}
        )
        self.plots.objects = [_figure(title, array) for title, array in arrays.items()]

    def panel(self) -> Any:
        return pn.Column(
            self.table,
            self.detail,
            self.button,
            self.plots,
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client's records, for a look below the outputs: a table with a row per
record, and the record selected, as :func:`describe` describes it.
"""

from __future__ import annotations

import json
from typing import Any

import panel as pn
from pydantic_core import to_jsonable_python

from ess.apps import Client, Record, Status

from .listing import Listing
from .table import Table


def describe(client: Client, listing: Listing, record: Record) -> str:
    """
    A record in Markdown: its status, spec, who submitted it and when, its
    outputs, what its request names, its params, and why it failed.
    """
    spec = listing.specs[record.id]
    status = listing.status[record.id]
    created = record.created.astimezone()
    outputs = (listing.output_label(record.ref(n)) for n in record.outputs)
    lines = [
        f'### {listing.labels[record.id]}: {status}',
        f'**Spec** {spec.title} (`{record.spec}`): {spec.description}',
        f'**Submitted** by {record.submitter} at {created:%Y-%m-%d %H:%M:%S}',
        '**Outputs** ' + ', '.join(outputs),
    ]
    if reads := listing.reads(record):
        lines.append('**Reads** ' + '; '.join(reads))
    params = to_jsonable_python(record.request.params, by_alias=True)
    lines.append(f'**Params**\n```json\n{json.dumps(params, indent=2)}\n```')
    if status in (Status.FAILED, Status.CANCELLED):
        lines.append(f'**Failure** {client.failure(record)}')
    return '\n\n'.join(lines)


class Records:
    """
    A table with a row per record of the proposal, newest first, each with
    what its request names, and below it the record selected.
    """

    def __init__(self, client: Client, listing: Listing) -> None:
        self._client = client
        self.table = Table[str](['Record', 'Reads', 'Status'], self._show)  # by id
        self.detail = pn.pane.Markdown('', sizing_mode='stretch_width')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        """List the records again."""
        self._listing = listing
        self._records = {r.id: r for r in listing.records}
        self.table.set(
            {
                r.id: {
                    'Record': listing.labels[r.id],
                    'Reads': ', '.join(listing.reads(r)),
                    'Status': str(listing.status[r.id]),
                }
                for r in reversed(listing.records)
            }
        )

    @property
    def selected(self) -> Record | None:
        """The record selected, if one is."""
        selected = self.table.selected
        return None if selected is None else self._records[selected]

    def select(self, record: Record) -> None:
        """Select ``record``."""
        self.table.select(record.id)

    def _show(self) -> None:
        record = self.selected
        self.detail.object = (
            '' if record is None else describe(self._client, self._listing, record)
        )

    def panel(self) -> Any:
        return pn.Column(self.table.widget, self.detail, sizing_mode='stretch_width')

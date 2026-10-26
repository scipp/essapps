# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A minimal app on a client: a form for each spec the backend runs, a button
that computes, and the client's records.

The app needs a client only. It submits the shown form's request through the
client and shows what the backend says: each reason a request is refused, by
its place, or why a record failed. Each place of the form with nothing given
is shown as such a reason, and nothing is submitted. After each computation
the forms list the records again, so a place that takes a reference offers
the new record.
"""

from __future__ import annotations

from typing import Any

import panel as pn

from ess.dispatch import Client, NotReadable, Record, RecordFailed, SubmitError

from .form import Form, Listing


class App:
    """
    A form per spec, a button that computes, and the client's records.

    ``forms`` holds the forms by spec title, and ``picker`` picks the one
    shown. ``message`` shows the outcome of the last computation.
    """

    def __init__(self, client: Client) -> None:
        self.client = client
        self.forms = {s.title: Form(s, client) for s in client.specs()}
        self.picker = pn.widgets.RadioButtonGroup(options=list(self.forms))
        self.button = pn.widgets.Button(label='Compute', color='primary')
        self.button.on_click(lambda _: self.compute())
        self.message = pn.pane.Markdown('')
        self.results = pn.pane.Str('', sizing_mode='stretch_width')
        self._form = pn.Column(sizing_mode='stretch_width')
        self.picker.param.watch(lambda _: self._show(), 'value')
        self._show()

    @property
    def form(self) -> Form:
        """The form shown."""
        return self.forms[self.picker.value]

    def _show(self) -> None:
        self._form.objects = [self.form.panel()]

    def compute(self) -> Record | None:
        """
        Compute the shown form's request; its record, or ``None`` if it is
        refused, or if a place of the form has nothing given.
        """
        try:
            record = self.client.compute(self.form.spec, params=self.form.params)
        except SubmitError as error:
            self.message.object = 'Refused:\n\n' + '\n'.join(
                f'- {e}' for e in error.errors
            )
            return None
        failure = self.client.failure(record)
        listing = self.refresh()
        self.message.object = (
            f'Computed {listing.labels[record.id]}.'
            if failure is None
            else f'Failed: {failure}'
        )
        return record

    def refresh(self) -> Listing:
        """
        List the records again, in the pickers and below the forms; what is
        listed.
        """
        listing = Listing(self.client)
        for form in self.forms.values():
            form.refresh(listing)
        self.results.object = '\n'.join(
            f'{listing.labels[r.id]}\n  {self._outputs(r)}' for r in listing.records
        )
        return listing

    def _outputs(self, record: Record) -> str:
        """The outputs of ``record``, or why they cannot be read."""
        try:
            return repr(self.client.output(record))
        except (NotReadable, RecordFailed) as error:
            return str(error)

    def panel(self) -> Any:
        return pn.Column(
            self.picker,
            self._form,
            self.button,
            self.message,
            pn.pane.Markdown('**Records**'),
            self.results,
            sizing_mode='stretch_width',
        )

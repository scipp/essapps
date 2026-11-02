# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A minimal app on a client: a form for each spec the backend runs, a button
that submits, and the outputs of the client's records, with the records
themselves in a tab of their own.

The app needs a client only. It submits the shown form's request through the
client and shows what the backend says: each reason a request is refused, by
its place, or why a record failed. Each place of the form with nothing given
is shown as such a reason, and nothing is submitted. A submission returns
once the backend has logged it, so the app lists the new record pending.

The client tells no one that a record has finished, so the app asks:
:meth:`App.poll` lists the records again once one listed pending has
finished, and a periodic callback of Panel calls it while the app is served.
Each time the records are listed again, the forms are too, so a place that
takes a reference offers each new record, pending or not.
"""

from __future__ import annotations

from typing import Any

import panel as pn

from ess.apps import Client, Record, Status, SubmitError

from .form import Form
from .listing import Listing
from .outputs import Outputs
from .records import Records


class App:
    """
    A form per spec, a button that submits, and the outputs of the client's
    records.

    ``forms`` holds the forms by spec title, and ``picker`` picks the one
    shown. ``message`` shows the outcome of the last submission, once its
    record has finished. ``outputs`` lists the outputs of the records, and
    ``records`` the records.
    """

    def __init__(self, client: Client) -> None:
        self.client = client
        self.forms = {s.title: Form(s, client) for s in client.specs()}
        self.picker = pn.widgets.RadioButtonGroup(options=list(self.forms))
        self.button = pn.widgets.Button(label='Submit', color='primary')
        self.button.on_click(lambda _: self.submit())
        self.message = pn.pane.Markdown('')
        self._listing = Listing(client)
        self.outputs = Outputs(client, self._listing, refresh=self.refresh)
        self.records = Records(client, self._listing)
        self._form = pn.Column(sizing_mode='stretch_width')
        self._submitted: Record | None = None  # until it has finished
        self.picker.param.watch(lambda _: self._show(), 'value')
        self._show()
        # Outside a server or a notebook, nothing runs the callback.
        pn.state.add_periodic_callback(self.poll, period=500)  # ms

    @property
    def form(self) -> Form:
        """The form shown."""
        return self.forms[self.picker.value]

    def _show(self) -> None:
        self._form.objects = [self.form.panel()]

    def submit(self) -> Record | None:
        """
        Submit the shown form's request; its record, pending, or ``None`` if
        it is refused, or if a place of the form has nothing given.
        """
        try:
            record = self.client.submit(self.form.spec, params=self.form.params)
        except SubmitError as error:
            self.message.object = 'Refused:\n\n' + '\n'.join(
                f'- {e}' for e in error.errors
            )
            return None
        self._submitted = record
        listing = self.refresh()
        self.message.object = f'Submitted {listing.labels[record.id]}.'
        self.poll()  # in case it has finished already
        return record

    def poll(self) -> None:
        """
        List the records again if one that was listed pending has finished,
        and show the outcome of the last submission once it is listed
        finished.
        """
        pending = self._listing.pending
        if any(s is not Status.PENDING for s in self.client.status(pending)):
            self.refresh()
        record = self._submitted
        if record is None or record in self._listing.pending:
            return
        failure = self.client.failure(record)
        self.message.object = (
            f'Computed {self._listing.labels[record.id]}.'
            if failure is None
            else f'Failed: {failure}'
        )
        self._submitted = None

    def refresh(self) -> Listing:
        """
        List the records again, in the pickers, the outputs, and the records;
        what is listed.
        """
        self._listing = Listing(self.client)
        for form in self.forms.values():
            form.refresh(self._listing)
        self.outputs.refresh(self._listing)
        self.records.refresh(self._listing)
        return self._listing

    def panel(self) -> Any:
        return pn.Column(
            self.picker,
            self._form,
            self.button,
            self.message,
            pn.Tabs(
                ('Outputs', self.outputs.panel()),
                ('Records', self.records.panel()),
            ),
            sizing_mode='stretch_width',
        )

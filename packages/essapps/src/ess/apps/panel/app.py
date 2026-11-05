# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A minimal app on a client: a form for each spec the backend runs, a button
that submits, and the outputs of the client's records, with the records
themselves in a tab of their own.

The app needs a client only. It submits the shown form's request through the
client and shows what the backend says: each reason a request is refused, by
its position, or why a record failed. Each field of the form with nothing given
is shown as such a reason, and nothing is submitted. A submission returns
once the backend has logged it, so the app lists the new record pending.

The client tells no one what changed, so the app asks: :meth:`App.poll` asks
for the changes since the last listing, a record of another client of the
proposal among them, and lists only the records that changed again. A periodic
callback of Panel calls it while the app is served. Each time the records are
listed again, the forms are too, so a field that takes a reference offers each
new record, pending or not. The changes do not tell that an output was released,
so after a release the outputs are listed from scratch.
"""

from __future__ import annotations

from typing import Any

import panel as pn

from ess.apps import Client, Listing, Record, SubmitError

from .form import Form
from .outputs import Outputs, Plots
from .records import Records


class App:
    """
    A form per spec, a button that submits, and the outputs of the client's
    records. ``plots`` are the plots configured for a spec, as :class:`Outputs`
    takes them.

    ``forms`` holds the forms by spec title, and ``picker`` picks the one
    shown. ``message`` shows the outcome of the last submission, once its
    record has finished. ``outputs`` lists the outputs of the records, and
    ``records`` lists the records.
    """

    def __init__(self, client: Client, plots: Plots | None = None) -> None:
        self.client = client
        self.forms = {s.title: Form(s, client) for s in client.specs()}
        self.picker = pn.widgets.RadioButtonGroup(options=list(self.forms))
        self.button = pn.widgets.Button(label='Submit', color='primary')
        self.button.on_click(lambda _: self.submit())
        self.message = pn.pane.Markdown('')
        self._listing = Listing(client)
        self.outputs = Outputs(
            client, self._listing, refresh=self._refresh_all, plots=plots
        )
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
        it is refused, or if a field of the form has nothing given.
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
        List the records again if any changed, and show the outcome of the
        last submission once it is listed finished.
        """
        if self.client.changes(after=self._listing.cursor).records:
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
        List the records that changed again, in the pickers, the outputs, and
        the records; what is listed.
        """
        return self._show_listing(Listing(self.client, previous=self._listing))

    def _refresh_all(self) -> Listing:
        """List every record again, as after a release, which is not a change."""
        return self._show_listing(Listing(self.client))

    def _show_listing(self, listing: Listing) -> Listing:
        self._listing = listing
        for form in self.forms.values():
            form.refresh(listing)
        self.outputs.refresh(listing)
        self.records.refresh(listing)
        return listing

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

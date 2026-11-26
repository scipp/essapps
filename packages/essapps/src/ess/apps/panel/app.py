# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A minimal app on a client: a form per spec, the outputs, and the records.

The client does not notify anyone of changes. So a periodic callback calls
:meth:`App.poll`, which lists again the records changed since the last listing.
These include the records of other clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import panel as pn

from ess.apps import Client, Listing, Record, SubmitError
from ess.spec import WorkflowSpec

from .form import Form
from .outputs import Outputs, Plots
from .records import Records


class Tab(Protocol):
    """
    A tab that an application adds to the app.

    The app calls :meth:`refresh` with each new listing.
    """

    def refresh(self, listing: Listing) -> None: ...

    def panel(self) -> Any: ...


class App:
    """
    A form per spec with a button that submits, and the outputs and records.

    ``forms`` holds the forms by spec title. ``message`` shows the outcome of the
    last submission: rejected, submitted, then computed or failed.

    Parameters
    ----------
    plots:
        The plots configured for each spec, as :class:`Outputs` accepts them.
    tabs:
        The application's own tabs by title, shown before those of the app.
    """

    def __init__(
        self,
        client: Client,
        plots: Plots | None = None,
        tabs: Mapping[str, Tab] | None = None,
    ) -> None:
        self.client = client
        self.tabs = dict(tabs or {})
        self._listing = Listing(client)
        self._specs = {s.title: s for s in client.specs()}
        self.forms = {
            title: Form(s.params, self._listing, title=title, description=s.description)
            for title, s in self._specs.items()
        }
        self.picker = pn.widgets.RadioButtonGroup(options=list(self.forms))
        self.button = pn.widgets.Button(label='Submit', color='primary')
        self.button.on_click(lambda _: self.submit())
        self.message = pn.pane.Markdown('', width=320)
        self.outputs = Outputs(
            client, self._listing, refresh=self._refresh_all, plots=plots
        )
        self.records = Records(client, self._listing)
        self._form = pn.Column(scroll=True, sizing_mode='stretch_both')
        self._submitted: Record | None = None  # until it has finished
        self.picker.param.watch(lambda _: self._show(), 'value')
        self._show()
        # Outside a server or a notebook, nothing runs the callback.
        pn.state.add_periodic_callback(self.poll, period=500)  # ms

    @property
    def form(self) -> Form:
        """The form shown."""
        return self.forms[self.picker.value]

    @property
    def spec(self) -> WorkflowSpec[Any, Any]:
        """The spec of the form shown."""
        return self._specs[self.picker.value]

    def _show(self) -> None:
        self._form.objects = [self.form.panel()]

    def submit(self) -> Record | None:
        """
        Submits the request of the form shown.

        Returns its record, or ``None`` if the form or the client rejects the
        request, for example because a field has nothing given.
        """
        try:
            record = self.client.submit(self.spec, params=self.form.params)
        except SubmitError as error:
            self.message.object = 'Rejected:\n\n' + '\n'.join(
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
        Lists the records again if any changed.

        Shows the outcome of the last submission once it is listed as finished.
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
        """Lists again the records that changed, in every tab. Returns the listing."""
        return self._show_listing(Listing(self.client, previous=self._listing))

    def _refresh_all(self) -> Listing:
        """Lists every record again, as after a release, which ``changes`` omits."""
        return self._show_listing(Listing(self.client))

    def _show_listing(self, listing: Listing) -> Listing:
        self._listing = listing
        for form in self.forms.values():
            form.refresh(listing)
        for tab in self.tabs.values():
            tab.refresh(listing)
        self.outputs.refresh(listing)
        self.records.refresh(listing)
        return listing

    def panel(self) -> pn.Tabs:
        return pn.Tabs(
            *((title, tab.panel()) for title, tab in self.tabs.items()),
            (
                'Workflows',
                pn.Column(
                    pn.Row(self.picker, self.button),
                    pn.Row(self._form, self.message, sizing_mode='stretch_both'),
                    sizing_mode='stretch_both',
                ),
            ),
            ('Outputs', self.outputs.panel()),
            ('Records', self.records.panel()),
            sizing_mode='stretch_both',
        )

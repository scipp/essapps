# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The SANS application of LoKI: the generic app of ``ess.apps.panel`` with plots
for LoKI's specs, and a tab that reduces a batch of samples. It needs the extra
``[loki-app]``.

The order of use is the order of the specs. The masks come first, then the beam
centre found with them, each a record to look at before the next. The batch then
reduces every sample run that has a transmission run (:func:`.batch.rows`) with
the same settings, against one can. A run whose result is not right is edited, and
submitted by itself; the new record is the row's result from then on.
"""

import panel as pn

from ess.apps import (
    Client,
    FieldError,
    Listing,
    NotReadable,
    Record,
    Stage,
    Status,
    SubmitError,
    Template,
)
from ess.apps.panel import App, Form, Table, figure_pane
from ess.apps.plotting import overlay
from ess.spec import FileRef

from .batch import Row, latest_results, rows
from .plots import PLOTS
from .specs import IOFQ

_ROW = ('sample', 'sample_transmission')
"""The parameters of I(Q) that a row of the batch gives."""


def _refused(errors: tuple[FieldError, ...]) -> str:
    return 'Refused:\n\n' + '\n'.join(f'- {e}' for e in errors)


class Batch:
    """
    A table of the sample runs, and a button that reduces all that have a
    transmission run with the ``settings``, through one stage, so the can is
    reduced once. The stage stays open while the settings are unchanged and it
    refuses no row: a stage that has failed refuses every later row for the same
    reason, so the next **Reduce all** opens a new one.

    ``tweak`` is a form for one run: **Edit** shows the selected row's latest
    result in it, or the settings and the row's files if it has none, and
    **Submit** submits it by itself. ``view`` draws the I(Q) of every row whose
    latest result completed, on one plot. ``message`` shows the outcome of the
    last action.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._listing = Listing(client)
        self._rows: list[Row] = []
        self._results: dict[FileRef, Record] = {}
        self._stage: tuple[Template, Stage] | None = None
        self._drawn: tuple[tuple[tuple[str, str], ...], bool] | None = None
        self.settings = Form(IOFQ, client, blanks=_ROW)
        self.tweak = Form(IOFQ, client)
        self.table = Table[FileRef](
            ['Sample', 'Run', 'Transmission', 'I(Q)'], self._selected
        )
        self.reduce_button = pn.widgets.Button(label='Reduce all', color='primary')
        self.reduce_button.on_click(lambda _: self.reduce())
        self.edit_button = pn.widgets.Button(label='Edit', disabled=True)
        self.edit_button.on_click(lambda _: self.edit())
        self.submit_button = pn.widgets.Button(label='Submit', color='primary')
        self.submit_button.on_click(lambda _: self.submit())
        self.message = pn.pane.Markdown('')
        self.log = pn.widgets.Checkbox(label='Logarithmic y axis', value=True)
        self.log.param.watch(lambda _: self._draw(), 'value')
        self.view = pn.Column(sizing_mode='stretch_width')
        self.refresh(self._listing)

    def refresh(self, listing: Listing) -> None:
        """
        Offer what ``listing`` has in the forms, list the rows with the status of
        their latest result, and draw the plot again if the results it shows
        changed.
        """
        self._listing = listing
        self.settings.refresh(listing)
        self.tweak.refresh(listing)
        self._rows = rows(self._client.files)
        self._results = latest_results(listing)
        self.table.set({row.sample: self._cells(row) for row in self._rows})
        self._draw()

    def _cells(self, row: Row) -> dict[str, str]:
        result = self._results.get(row.sample)
        transmission = row.transmission
        return {
            'Sample': row.name,
            'Run': self._listing.file_label(row.sample),
            'Transmission': (
                'none'
                if transmission is None
                else self._listing.file_label(transmission)
            ),
            'I(Q)': '' if result is None else str(self._listing.status[result.id]),
        }

    def _selected(self) -> None:
        self.edit_button.disabled = self.table.selected is None

    def _list_again(self) -> None:
        self.refresh(Listing(self._client, previous=self._listing))

    def reduce(self) -> dict[str, Record]:
        """
        Submit every row that has a transmission run through a stage of the
        settings; the records submitted, by the name of the row. A refusal, of
        the settings or of a row, stops the submissions and is shown in
        ``message``.
        """
        records: dict[str, Record] = {}
        try:
            stage = self._open(Template(IOFQ, params=self.settings.params, blanks=_ROW))
            for row in self._rows:
                if row.transmission is None:
                    continue
                try:
                    records[row.name] = self._client.submit(
                        stage,
                        params={
                            'sample': row.sample,
                            'sample_transmission': row.transmission,
                        },
                    )
                except SubmitError as error:
                    self._close()
                    raise SubmitError(
                        FieldError(row.name, e.loc, e.msg) for e in error.errors
                    ) from None
        except SubmitError as error:
            self.message.object = _refused(error.errors)
        else:
            self.message.object = f'Submitted {len(records)} runs.'
        self._list_again()
        return records

    def _open(self, template: Template) -> Stage:
        """The stage of ``template``: the open one if it is of the same, else new."""
        if self._stage is not None and self._stage[0] == template:
            return self._stage[1]
        stage = self._client.stage(template)
        self._close()
        self._stage = (template, stage)
        return stage

    def _close(self) -> None:
        if self._stage is not None:
            self._client.release(self._stage[1])
            self._stage = None

    def edit(self) -> None:
        """
        Show the selected row in ``tweak``: its latest result's params, else the
        settings and the row's files.
        """
        if (selected := self.table.selected) is None:
            return
        if (result := self._results.get(selected)) is not None:
            self.tweak.set(result.request.params)
            return
        row = next(r for r in self._rows if r.sample == selected)
        try:
            given = {**self.settings.params, 'sample': row.sample}
        except SubmitError as error:
            self.message.object = _refused(error.errors)
            return
        if row.transmission is not None:
            given['sample_transmission'] = row.transmission
        self.tweak.set(given)

    def submit(self) -> Record | None:
        """
        Submit ``tweak`` by itself; its record, pending, or ``None`` if it is
        refused, which ``message`` shows.
        """
        try:
            record = self._client.submit(IOFQ, params=self.tweak.params)
        except SubmitError as error:
            self.message.object = _refused(error.errors)
            return None
        self._list_again()
        self.message.object = f'Submitted {self._listing.labels[record.id]}.'
        return record

    def _draw(self) -> None:
        """Draw the readable I(Q) of the rows completed, if they or the axis changed."""
        completed = {
            row.name: result
            for row in self._rows
            if (result := self._results.get(row.sample)) is not None
            and self._listing.status[result.id] is Status.COMPLETED
            and 'iofq' in self._listing.readable[result.id]
        }
        key = (tuple((n, r.id) for n, r in completed.items()), self.log.value)
        if key == self._drawn:
            return
        self._drawn = key
        if not completed:
            self.view.objects = []
            return
        try:
            arrays = {
                name: self._client.output(result, 'iofq').intensity
                for name, result in completed.items()
            }
        except NotReadable as error:
            self.view.objects = [pn.pane.Markdown(f'Cannot read I(Q): {error}')]
            return
        self.view.objects = [figure_pane(overlay(arrays, log=self.log.value))]

    def panel(self) -> pn.Column:
        return pn.Column(
            pn.pane.Markdown('### Settings of all runs'),
            self.settings.panel(),
            pn.pane.Markdown('### Runs'),
            self.table.widget,
            pn.Row(self.reduce_button, self.edit_button),
            self.message,
            self.log,
            self.view,
            pn.pane.Markdown('### Tweak one run'),
            self.tweak.panel(),
            self.submit_button,
            sizing_mode='stretch_width',
        )


class LokiApp:
    """The app of ``ess.apps.panel`` with LoKI's plots and a ``Batch`` tab."""

    def __init__(self, client: Client) -> None:
        self.batch = Batch(client)
        self.app = App(client, plots=PLOTS, tabs={'Batch': self.batch})

    def panel(self) -> pn.Column:
        return self.app.panel()

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Several outputs on one plot: the user chooses 1-D arrays among the outputs of
completed records, and the plot draws a line for each.

The arrays offered are the 1-D array fields of the outputs of completed
records that the client may read (:class:`~ess.apps.plotting.Lines`). Once one
is chosen, only those along the same dimension are offered. The choice stays
through a refresh, as far as it is still offered.
"""

from __future__ import annotations

from typing import Any

import panel as pn

from ess.apps import Client, Listing
from ess.apps.plotting import Line, LineKey, Lines, overlay, same_dim

from .outputs import figure_pane


class Overlay:
    """
    A choice of 1-D arrays, a checkbox for a logarithmic y axis, and the
    figure that draws what is chosen.
    """

    def __init__(self, client: Client, listing: Listing) -> None:
        self._lines = Lines(client)
        self._offered: dict[LineKey, Line] = {}
        self._drawn: list[str] = []  # the labels of the lines in the figure
        self.choice = pn.widgets.MultiChoice(label='Outputs', options={}, value=[])
        self.log = pn.widgets.Checkbox(label='Logarithmic y axis')
        self.view = pn.Column(sizing_mode='stretch_width')
        self.choice.param.watch(lambda _: self._chosen(), 'value')
        self.log.param.watch(lambda _: self._draw(), 'value')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        """
        Offer the arrays of the outputs of ``listing``, and keep those chosen
        that are still offered. The plot is drawn again if fewer are, or if the
        label of one changed, which it does when another output is listed with
        the same.
        """
        self._offered = self._lines.of(listing)
        chosen = [c for c in self.choice.value if c in self._offered]
        self.choice.param.update(options=self._options(chosen), value=chosen)
        if self._labels(chosen) != self._drawn:
            self._draw()

    def _labels(self, chosen: list[LineKey]) -> list[str]:
        return [self._offered[c].label for c in chosen]

    def _chosen(self) -> None:
        self.choice.options = self._options(self.choice.value)
        self._draw()

    def _options(self, chosen: list[LineKey]) -> dict[str, LineKey]:
        offered = same_dim(self._offered, chosen)
        return {line.label: key for key, line in offered.items()}

    def _draw(self) -> None:
        self._drawn = self._labels(self.choice.value)
        if not self.choice.value:
            self.view.objects = []
            return
        try:
            arrays = {
                self._offered[c].label: self._lines.read(self._offered[c])
                for c in self.choice.value
            }
            self.view.objects = [figure_pane(overlay(arrays, log=self.log.value))]
        except Exception as error:  # not every choice can be drawn together
            self.view.objects = [pn.pane.Markdown(f'Cannot draw these: {error}')]

    def panel(self) -> Any:
        return pn.Column(self.choice, self.log, self.view, sizing_mode='stretch_width')

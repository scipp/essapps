# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A table of rows by key, of which one may be selected."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd
import panel as pn


class Table[K]:
    """
    A table with a row per key, of which one may be selected.

    ``show`` is called when the row selected changes: another key, none, or
    other values in the row of the same key. Setting the rows again keeps
    the key selected, if it has a row, so what ``show`` shows survives a
    refresh in which that row stays the same, though rows before it come
    and go.
    """

    def __init__(
        self, columns: list[str], show: Callable[[], None], **kwargs: Any
    ) -> None:
        self.widget = pn.widgets.Tabulator(
            pd.DataFrame(columns=columns, dtype=str),
            disabled=True,
            selectable=1,
            sortable=False,
            show_index=False,
            layout='fit_columns',
            sizing_mode='stretch_width',
            **kwargs,
        )
        self._columns = columns
        self._show = show
        self._keys: list[K] = []
        self._shown: tuple[K, tuple[str, ...]] | None = None
        self._setting = False
        self.widget.param.watch(lambda _: self._changed(), 'selection')

    @property
    def selected(self) -> K | None:
        """The key of the row selected, if one is."""
        selection = self.widget.selection
        return self._keys[selection[0]] if len(selection) == 1 else None

    def select(self, key: K | None) -> None:
        """Select the row of ``key``; none for ``None``."""
        self.widget.selection = [] if key is None else [self._keys.index(key)]

    def set(self, rows: Mapping[K, Mapping[str, str]]) -> None:
        """Set the rows, the values of each by column."""
        selected = self.selected
        self._setting = True
        try:
            self._keys = list(rows)
            self.widget.value = pd.DataFrame(
                [[row[c] for c in self._columns] for row in rows.values()],
                columns=self._columns,
                dtype=str,
            )
            self.select(selected if selected in rows else None)
        finally:
            self._setting = False
        self._changed()

    def _changed(self) -> None:
        if self._setting:
            return
        key = self.selected
        shown = None
        if key is not None:
            row = self.widget.value.iloc[self._keys.index(key)]
            shown = (key, tuple(row))
        if shown != self._shown:
            self._shown = shown
            self._show()

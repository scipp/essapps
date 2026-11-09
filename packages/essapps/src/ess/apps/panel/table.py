# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A table of rows by key, of which one may be selected."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pandas as pd
import panel as pn


def with_sidebar(sidebar: list[Any], main: list[Any]) -> pn.Row:
    """
    A column of fixed width beside one that takes the rest, each scrolling by
    itself, so that a table to select from stays in view of what is selected.
    """
    return pn.Row(
        pn.Column(*sidebar, width=440, scroll=True, sizing_mode='stretch_height'),
        pn.Column(*main, scroll=True, sizing_mode='stretch_both'),
        sizing_mode='stretch_both',
    )


class Table[K]:
    """
    A table with a row per key, of which one may be selected, or several with
    ``selectable=True``.

    ``show`` is called when the row selected changes: another key, none, or
    other values in the row of the same key. Setting the rows again keeps
    the key selected, if it has a row, so what ``show`` shows survives a
    refresh in which that row stays the same, though rows before it come
    and go.
    """

    def __init__(
        self, columns: list[str], show: Callable[[], None], **kwargs: Any
    ) -> None:
        kwargs.setdefault('selectable', 1)
        self.widget = pn.widgets.Tabulator(
            pd.DataFrame(columns=columns, dtype=str),
            disabled=True,
            sortable=False,
            show_index=False,
            layout='fit_columns',
            sizing_mode='stretch_width',
            **kwargs,
        )
        self._columns = columns
        self._show = show
        self._keys: list[K] = []
        self._shown: tuple[tuple[K, tuple[str, ...]], ...] = ()
        self._setting = False
        self.widget.param.watch(lambda _: self._changed(), 'selection')

    @property
    def selected(self) -> K | None:
        """The key of the row selected, if exactly one is."""
        keys = self.selected_keys
        return keys[0] if len(keys) == 1 else None

    @property
    def selected_keys(self) -> list[K]:
        """The keys of the rows selected."""
        return [self._keys[i] for i in self.widget.selection]

    def select(self, key: K | None) -> None:
        """Select the row of ``key``; none for ``None``."""
        self.select_keys([] if key is None else [key])

    def select_keys(self, keys: list[K]) -> None:
        """Select the rows of ``keys``."""
        self.widget.selection = [self._keys.index(key) for key in keys]

    def set(self, rows: Mapping[K, Mapping[str, str]]) -> None:
        """Set the rows, the values of each by column."""
        selected = self.selected_keys
        self._setting = True
        try:
            self._keys = list(rows)
            self.widget.value = pd.DataFrame(
                [[row[c] for c in self._columns] for row in rows.values()],
                columns=self._columns,
                dtype=str,
            )
            self.select_keys([key for key in selected if key in rows])
        finally:
            self._setting = False
        self._changed()

    def _changed(self) -> None:
        if self._setting:
            return
        shown = tuple(
            (self._keys[i], tuple(self.widget.value.iloc[i]))
            for i in self.widget.selection
        )
        if shown != self._shown:
            self._shown = shown
            self._show()

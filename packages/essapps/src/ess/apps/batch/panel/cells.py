# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the tables of the toolbox show, what their editors write, and their buttons.

An editor writes only the fields that differ from what it started with, so that
updating one cell leaves the others as they are. With several rows selected it
starts at the defaults, so a value equal to its default would not count as a
change: a person chooses that field in :func:`write_choice`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import panel as pn
from pydantic_core import to_json

from ess.apps import Listing
from ess.spec import FileRef, OutputRef
from ess.spec.refs import as_ref


def cell_text(value: Any, listing: Listing) -> str:
    """What a table shows for a cell: a file or an output by its label."""
    ref = as_ref(value)
    if isinstance(ref, OutputRef):
        return listing.output_label(ref)
    if isinstance(ref, FileRef):
        return listing.file_label(ref)
    if isinstance(value, list):
        return ', '.join(cell_text(item, listing) for item in value)
    if isinstance(value, dict):
        return to_json(value).decode()
    return 'none' if value is None else str(value)


def cell_texts(
    values: Mapping[str, Any], titles: Mapping[str, str], listing: Listing
) -> dict[str, str]:
    """The text of each field of ``titles`` by its title, empty if not in ``values``."""
    return {
        title: cell_text(values[name], listing) if name in values else ''
        for name, title in titles.items()
    }


def write_choice(titles: Mapping[str, str]) -> pn.widgets.Select:
    """A choice of one field to write whatever it holds, among ``titles`` by name."""
    options = {'The cells changed': '', **{t: name for name, t in titles.items()}}
    return pn.widgets.Select(label='Write', options=options)


def changed_cells(
    given: Mapping[str, Any], shown: Mapping[str, Any], chosen: str
) -> dict[str, Any]:
    """The fields of ``given`` that differ from ``shown``, and the one ``chosen``."""
    return {
        name: value
        for name, value in given.items()
        if name == chosen or name not in shown or value != shown[name]
    }


def button(
    label: str, action: Callable[[], Any], color: str = 'default'
) -> pn.widgets.Button:
    """A button that calls ``action``."""
    widget = pn.widgets.Button(label=label, color=color)
    widget.on_click(lambda _: action())
    return widget

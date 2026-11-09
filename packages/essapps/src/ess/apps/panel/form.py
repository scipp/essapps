# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A form for a pydantic model of params: the fields of :mod:`ess.apps.fields`, shown.

This module only places widgets and connects their events. What a field holds, and
what is missing, is in :mod:`ess.apps.fields`.

The form has a tab per parameter, with the tabs on the left, as
esslivedata's dashboard has (scipp/esslivedata#1426). A parameter is labelled by
the title of its tab. The same widgets serve a parameter, a field of a group at any
depth, and the element of a list. At a field of a group, a choice and a table show
in a card titled by the field, so that a nested group is a card of its fields.

A picker's option of nothing has the value ``UNSET``: Panel does not send a value
of ``None`` to the browser when it first shows a picker, which then shows no
option at all.

A table is a ``Tabulator``, so a notebook loads Panel with
``pn.extension('tabulator')``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

import pandas as pd
import panel as pn
from pydantic import BaseModel

from ess.apps import Listing
from ess.apps.fields import (
    ChoiceField,
    Field,
    Fields,
    FileField,
    GroupField,
    Input,
    NothingField,
    RowsField,
    ScalarField,
    ScalarsField,
)


def _bound(
    field: Field,
    widget: Any,
    show: Callable[[], None],
    written: Callable[[Any], None],
    name: str = 'value',
) -> None:
    """
    Run ``show`` now and whenever ``field`` changes, and give ``written`` what a
    person sets at ``name`` of ``widget``. What ``show`` does to the widget does not
    count, also where Panel moves a value that new options lack.
    """
    showing = False

    def shown() -> None:
        nonlocal showing
        showing = True
        try:
            show()
        finally:
            showing = False

    def changed(event: Any) -> None:
        if not showing:
            written(event.new)

    shown()
    field.subscribe(shown)
    widget.param.watch(changed, name)


def _input_widget(field: Input) -> Any:
    options: dict[str, Any] = {'label': field.label, 'sizing_mode': 'stretch_width'}
    if not isinstance(field, ScalarField):
        return pn.widgets.Select(**options)
    options['description'] = field.description
    if field.kind == 'float':
        return pn.widgets.FloatInput(**options)
    if field.kind == 'int':
        return pn.widgets.IntInput(**options)
    if field.kind == 'text':
        return pn.widgets.TextInput(**options)
    if field.kind == 'time':
        return pn.widgets.DatetimePicker(**options)
    return pn.widgets.Select(**options)


def _input(field: Input) -> Any:
    widget = _input_widget(field)

    def show() -> None:
        if isinstance(widget, pn.widgets.Select) and widget.options != field.options:
            widget.options = dict(field.options)
        widget.label = field.label
        widget.value = field.value

    def written(value: Any) -> None:
        field.value = value

    _bound(field, widget, show, written)
    return widget


def _scalars(field: ScalarsField) -> Any:
    column = pn.Column(sizing_mode='stretch_width')
    shown: dict[ScalarField, Any] = {}

    def show() -> None:
        nonlocal shown
        shown = {e: shown[e] if e in shown else _input(e) for e in field.elements}
        objects = list(shown.values())
        if column.objects != objects:
            column.objects = objects

    show()
    field.subscribe(show)
    add = pn.widgets.Button(label=f'Add to {field.label}')
    add.on_click(lambda _: field.add())
    remove = pn.widgets.Button(label=f'Remove from {field.label}')
    remove.on_click(lambda _: field.remove())
    return pn.Column(column, pn.Row(add, remove))


def _group(field: GroupField) -> Any:
    return pn.Column(
        *(
            _view(f)
            if isinstance(f, ScalarField | ScalarsField | FileField)
            else pn.Card(
                _view(f),
                title=field.titles[name],
                collapsible=False,
                sizing_mode='stretch_width',
            )
            for name, f in field.fields.items()
        ),
        sizing_mode='stretch_width',
    )


def _choice(field: ChoiceField) -> Any:
    selector = pn.widgets.RadioBoxGroup(options=field.offered, inline=True)
    ways = {label: _view(way) for label, way in field.ways.items()}

    def show() -> None:
        selector.value = field.chosen
        selector.visible = field.selector_visible
        for label, way in ways.items():
            way.visible = label == field.chosen

    def written(label: str | None) -> None:
        field.chosen = label

    _bound(field, selector, show, written)
    return pn.Column(selector, *ways.values(), sizing_mode='stretch_width')


def _rows(field: RowsField) -> Any:
    table = pn.widgets.Tabulator(
        disabled=True,
        selectable=True,
        sortable=False,
        show_index=False,
        layout='fit_columns',
        sizing_mode='stretch_width',
    )
    message = pn.pane.Markdown()
    add = pn.widgets.Button(label='Add as new row')
    add.on_click(lambda _: field.add_row())
    update = pn.widgets.Button(label='Update row', disabled=True)
    update.on_click(lambda _: field.update_row())
    remove = pn.widgets.Button(label='Remove')
    remove.on_click(lambda _: field.remove_rows())
    discard = pn.widgets.Button(label='Discard')
    discard.on_click(lambda _: field.discard())
    columns: dict[str, list[str]] | None = None

    def show() -> None:
        nonlocal columns
        if columns is not field.columns:
            columns = field.columns
            # Of text, since Tabulator formats a column by its type when first
            # shown, and a column without rows would be one of numbers.
            table.value = pd.DataFrame(columns, dtype=str)
        table.selection = field.selection
        update.disabled = not field.can_update
        message.object = field.message

    _bound(field, table, show, field.select, 'selection')
    return pn.Column(
        table,
        _view(field.editor),
        pn.Row(add, update, remove, discard),
        message,
        sizing_mode='stretch_width',
    )


def _view(field: Field) -> Any:
    """The widgets of ``field``, kept up to date with it."""
    if isinstance(field, Input):
        return _input(field)
    if isinstance(field, ScalarsField):
        return _scalars(field)
    if isinstance(field, GroupField):
        return _group(field)
    if isinstance(field, ChoiceField):
        return _choice(field)
    if isinstance(field, RowsField):
        return _rows(field)
    if isinstance(field, NothingField):
        return pn.Spacer(height=0)
    raise TypeError(f'no widget for {type(field).__name__}')


class Form(Fields):
    """
    The :class:`~ess.apps.fields.Fields` of a pydantic model, shown as tabs by
    :meth:`panel`.
    """

    def __init__(
        self,
        model: type[BaseModel],
        listing: Listing,
        *,
        fields: Collection[str] | None = None,
        title: str = '',
        description: str = '',
    ) -> None:
        super().__init__(model, listing, fields=fields, title=title)
        heading = f'**{title}**: {description}' if title else description
        self._panel = pn.Column(
            pn.pane.Markdown(heading),
            pn.Tabs(
                *(
                    pn.Column(
                        _view(field),
                        name=self.titles[name],
                        sizing_mode='stretch_width',
                    )
                    for name, field in self.fields.items()
                ),
                tabs_location='left',
                sizing_mode='stretch_width',
                max_width=800,
            ),
            sizing_mode='stretch_width',
        )

    def panel(self) -> Any:
        return self._panel

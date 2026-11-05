# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A form for a spec's params, generated from its pydantic models, on a client.

The form gives the params as a request does: at each field, at any depth,
the JSON form of a value, a file, a reference to an output, or ``None``. It
is filled from params as a record stores them, so that a record's request
can be edited and submitted again.

The form has a tab per parameter, with the tabs on the left, as
esslivedata's dashboard has (scipp/esslivedata#1426). The widget of a field
follows from its type, as the table in ``packages/essspec/docs/params.md``
gives (see :func:`_widget`), and is labelled by
:func:`ess.spec.field_types.title`, a parameter by the title of its tab. The
same widgets serve a parameter, a field of a group at any depth, and the
element of a list. At a field of a group, a choice and a table show in a card
titled by the field, so that a nested group is a card of its fields.

A record's output fits a field if its type, from the record's spec, fits the
field, and a request of the client may reference it now
(``client.readable``), so a pending record is offered too. A file fits a file
field if a :class:`~ess.apps.Selector` of the marker's metadata matches it, as
the backend checks when the request is submitted. A file that the form is set
to, such as one a record names, stays picked when the listing no longer has
it.

A table is a ``Tabulator``, so a notebook loads Panel with
``pn.extension('tabulator')``.

A field without a default starts unset, so that a value a correct reduction
needs is never filled in for the user; so does a list, of rows or of scalars,
until an element is added or removed, so that a table, which has no default, is
never given without rows by mistake. A field is left out of the request only if
it is a choice with no way chosen, which it is only without a default, so the
backend refuses it as required. A way chosen, or an input, with nothing given
is not left out, since the backend would fill in the default: the params name
it as missing (see :attr:`Form.params`). An empty text is the value ``''``
where the field has a default, and nothing given where it has none, since a
text input cannot tell an empty text from none; an element of a list of texts
is a text, ``''`` too, since it was added.

A time is picked to the second, in the zone of the value the picker was
set to, or of the default, which its label names: ``Start (UTC)``. A time
without a zone stays without one, and a time of ``AwareDatetime`` without a
value is in UTC. The fraction of a second of the value is kept.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from enum import Enum
from typing import Any, Union, get_args, get_origin

import pandas as pd
import panel as pn
from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_json, to_jsonable_python

from ess.apps import Client, FieldError, Listing, SubmitError
from ess.spec import (
    File,
    FileRef,
    OutputRef,
    WorkflowSpec,
    field_types,
)
from ess.spec.refs import Loc, as_ref


class _Unset:
    def __repr__(self) -> str:
        return 'UNSET'


UNSET: Any = _Unset()
"""
The value of a field that the request leaves out; given to ``set``, the
field as it starts, at its default or unset.
"""

type _Missing = list[FieldError]
"""The fields with nothing given, each with what the user is to do."""

_HOLDS_CHANGES = 'the editor holds changes: add or update a row, or discard them'
"""What a user who changed the editor of a table is to do before going on."""

type _Widget = (
    _Scalar | _Scalars | _File | _Group | _Record | _Nothing | _Choice | _Rows
)
"""
The widget of a value: ``value(loc, missing)`` gives it as a request writes
it, ``set`` shows a value, ``refresh`` lists the files and records anew.
"""


def _field(name: str, info: FieldInfo, listing: Listing) -> _Widget:
    """The widget of the field ``name`` with ``info``, of a group or of the params."""
    return _widget(
        field_types.title(name, info),
        info.annotation,
        field_types.field_metadata(info),
        UNSET if info.is_required() else info.get_default(call_default_factory=True),
        listing,
        info.description,
    )


def _widget(
    label: str,
    annotation: Any,
    metadata: tuple[Any, ...],
    default: Any,
    listing: Listing,
    description: str | None = None,
) -> _Widget:
    """
    The widget of a value of type ``annotation`` with ``metadata`` (see
    :func:`ess.spec.field_types.field_metadata`), at any depth.

    The type is read with :func:`ess.spec.field_types.split`, as the sources of its
    request type are (:func:`ess.spec.request_types.sources`). A scalar is an input
    of its type, and a file field a picker of the files that fit it. Anything with
    more than one way to give the value is a :class:`_Choice`: the alternatives of
    a union of groups, a group (one way, ``Given``, whose selector is hidden), a
    scalar or a file field with ``None`` or a ``Referenced[...]`` member
    (``Given``, ``A file``), ``From a record``, and ``None``. A list of scalars is
    :class:`_Scalars`, and any other list :class:`_Rows`.
    """
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if get_origin(tp) is list:
        (element,) = get_args(tp)
        if field_types.is_scalar(item := field_types.plain(element)):
            return _Scalars(label, item, default, description)
        return _Rows(element, default, listing)
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    union = field_types.alternatives(inline, *inline_metadata)
    marker = field_types.file_marker(inline, *inline_metadata)
    if not (none or referenced or union):
        if marker is not None:
            return _File(marker, listing, label)
        if not field_types.is_group(inline):
            return _Scalar(label, inline, default, description)
    ways: dict[str, _Widget] = {}
    if union is not None:
        tag, groups = union
        for value, group in groups.items():
            way = field_types.title(value, group.model_fields[tag])
            ways[way] = _Group(group, {tag: value}, listing)
    elif marker is not None:
        ways[_Choice.FILE] = _File(marker, listing)
    elif field_types.is_group(inline):
        ways[_Choice.GIVEN] = _Group(inline, {}, listing)
    elif inline is not None:
        ways[_Choice.GIVEN] = _Scalar(label, inline, UNSET, description)
    if referenced:
        ways[_Choice.RECORD] = _Record(Union[referenced], listing)  # noqa: UP007
    if none:
        ways[_Choice.NONE] = _Nothing()
    return _Choice(ways, None if union is None else union[0], default)


def _picked(widget: Any, options: Mapping[str, Any]) -> None:
    """Give a picker ``options``; its value stays if it is one of them."""
    value = widget.value
    widget.options = dict(options)
    _pick(widget, value)


def _pick(widget: Any, value: Any) -> None:
    """Pick ``value`` if the picker lists it, else nothing."""
    widget.value = value if value in widget.options.values() else UNSET


class _File:
    """
    A file field: a picker of the files that fit its marker, labelled
    ``label``. A
    file that the form is set to and the listing lacks stays picked,
    labelled by its reference as given, an identity or a name such as
    ``run:101``.

    A picker's option of nothing has the value ``UNSET``: Panel does not
    send a value of ``None`` to the browser when it first shows a picker,
    which then shows no option at all.
    """

    def __init__(self, marker: File, listing: Listing, label: str = '') -> None:
        self._marker = marker
        self._listed: dict[str, Any] = {}
        self.widget = pn.widgets.Select(label=label, sizing_mode='stretch_width')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        self._listed = {'(choose a file)': UNSET, **listing.files(self._marker)}
        self.set(self.widget.value)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self.widget.value is not UNSET:
            return self.widget.value
        missing.append(FieldError(None, loc, 'choose a file'))
        return UNSET

    def set(self, value: Any) -> None:
        # A reference, or its JSON form, as a request may give it.
        ref = None if value is UNSET else as_ref(value)
        picked = ref if isinstance(ref, FileRef) else UNSET
        options = dict(self._listed)
        if picked is not UNSET and picked not in options.values():
            options[str(picked)] = picked
        self.widget.options = options
        self.widget.value = picked

    def panel(self) -> Any:
        return self.widget


class _Scalar:
    """
    A scalar: an ``int``, ``float``, or ``str`` input, or a picker of a time,
    that may be unset, or for a ``bool``, an enum, or a ``Literal``, a picker
    of its values with an option for none, ``UNSET`` as at a file, so that it
    may start unset. It gives the JSON form of its value.
    """

    def __init__(
        self, label: str, tp: Any, default: Any, description: str | None = None
    ) -> None:
        self._label = label
        self._type = tp
        self._default = default
        self._zone: tzinfo | None = None
        self._time_set: datetime | None = None  # with the zone and fraction
        options: dict[str, Any] = {
            'label': label,
            'description': description,
            'sizing_mode': 'stretch_width',
        }
        self.widget: Any
        if tp is float:
            self.widget = pn.widgets.FloatInput(**options)
        elif tp is int:
            self.widget = pn.widgets.IntInput(**options)
        elif tp is str:
            self.widget = pn.widgets.TextInput(**options)
        elif tp in field_types.TIMES:
            self.widget = pn.widgets.DatetimePicker(**options)
        elif tp is bool:
            self.widget = self._picker({'True': True, 'False': False}, options)
        elif isinstance(tp, type) and issubclass(tp, Enum):
            self.widget = self._picker({m.name: m for m in tp}, options)
        else:  # a Literal
            self.widget = self._picker({str(v): v for v in get_args(tp)}, options)
        self.set(UNSET)

    @staticmethod
    def _picker(values: dict[str, Any], options: dict[str, Any]) -> Any:
        return pn.widgets.Select(options={'(choose)': UNSET, **values}, **options)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        value = self.widget.value
        if value is None or value is UNSET or (value == '' and self._default is UNSET):
            missing.append(FieldError(None, loc, 'give a value'))
            return UNSET
        if self._type in field_types.TIMES:
            # The picker gives a time without a zone, to the second: the time
            # set, unless the user changed it.
            value = value.replace(tzinfo=self._zone)
            if (set_ := self._time_set) is not None and value == set_.replace(
                microsecond=0
            ):
                value = set_
        return to_jsonable_python(value)

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        if value is UNSET:
            if not isinstance(self.widget, pn.widgets.Select):
                value = '' if self._type is str else None
        elif isinstance(self._type, type) and issubclass(self._type, Enum):
            value = self._type(value)  # a record holds an enum by its value
        if self._type in field_types.TIMES:
            value = self._time(value)
        self.widget.value = value

    def _time(self, value: datetime | str | None) -> datetime | None:
        """
        ``value``, a time, or its text, as a record holds it, as the picker
        shows it: without its zone, which is kept and named in the label, and
        without its fraction of a second, which is kept until the user changes
        the time.
        """
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value is None:
            self._zone = UTC if self._type is AwareDatetime else None
        else:
            self._zone = value.tzinfo
        self._time_set = value
        zone = 'no time zone' if self._zone is None else self._zone.tzname(value)
        self.widget.label = f'{self._label} ({zone})'
        return None if value is None else value.replace(tzinfo=None)

    def refresh(self, listing: Listing) -> None:
        pass

    def panel(self) -> Any:
        return self.widget


class _Scalars:
    """
    A list of scalars: an input per element, as :class:`_Scalar` has it, and
    buttons that add an element and remove the last. Unset, it has no
    element until one is added or removed: a removal gives no element, the
    empty list.
    """

    def __init__(
        self, label: str, tp: Any, default: Any, description: str | None = None
    ) -> None:
        self._label = label
        self._type = tp
        self._default = default
        self._description = description
        self._elements: list[_Scalar] | None = None  # unset
        self._column = pn.Column(sizing_mode='stretch_width')
        add = pn.widgets.Button(label=f'Add to {label}')
        add.on_click(lambda _: self._resize(len(self._elements or []) + 1))
        remove = pn.widgets.Button(label=f'Remove from {label}')
        remove.on_click(lambda _: self._resize(len(self._elements or []) - 1))
        self._panel = pn.Column(self._column, pn.Row(add, remove))
        self.set(UNSET)

    def _resize(self, n: int) -> None:
        """
        Keep the first ``n`` elements, none for ``-1``, or add elements with
        nothing given, but a text, which is ``''``.
        """
        elements = self._elements or []
        del elements[n:]
        default = '' if self._type is str else UNSET
        elements.extend(
            _Scalar(f'{self._label} [{i}]', self._type, default, self._description)
            for i in range(len(elements), n)
        )
        self._elements = elements
        self._column.objects = [e.panel() for e in elements]

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self._elements is None:
            missing.append(FieldError(None, loc, 'add an element, or remove for none'))
            return UNSET
        return [e.value((*loc, i), missing) for i, e in enumerate(self._elements)]

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        if value is UNSET:
            self._elements = None
            self._column.objects = []
            return
        self._resize(len(value))
        for element, item in zip(self._elements or [], value, strict=True):
            element.set(item)

    def refresh(self, listing: Listing) -> None:
        pass

    def panel(self) -> Any:
        return self._panel


class _Group:
    """
    The fields of a group, each the widget of its type; the tag of an
    alternative has none. A choice or a table shows in a card titled by its
    field.
    """

    def __init__(
        self, group: type[BaseModel], tag: dict[str, str], listing: Listing
    ) -> None:
        self.tag = tag
        self._fields = {
            name: _field(name, info, listing)
            for name, info in group.model_fields.items()
            if name not in tag
        }
        self._panel = pn.Column(
            *(
                w.panel()
                if isinstance(w, _Scalar | _Scalars | _File)
                else pn.Card(
                    w.panel(),
                    title=field_types.title(name, group.model_fields[name]),
                    collapsible=False,
                    sizing_mode='stretch_width',
                )
                for name, w in self._fields.items()
            ),
            sizing_mode='stretch_width',
        )

    def value(self, loc: Loc, missing: _Missing) -> dict[str, Any]:
        given = {n: w.value((*loc, n), missing) for n, w in self._fields.items()}
        return self.tag | {n: v for n, v in given.items() if v is not UNSET}

    def set(self, value: Any) -> None:
        given = {} if value is UNSET else dict(value)
        for name, field in self._fields.items():
            field.set(given.get(name, UNSET))

    def refresh(self, listing: Listing) -> None:
        for field in self._fields.values():
            field.refresh(listing)

    def panel(self) -> Any:
        return self._panel


class _Record:
    """
    An output of a record that fits ``tp``, a group or a union of groups: a
    picker of references, with ``UNSET`` for none, as at a file.
    """

    def __init__(self, tp: Any, listing: Listing) -> None:
        self._tp = tp
        self.widget = pn.widgets.Select(sizing_mode='stretch_width')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        _picked(self.widget, {'(choose a record)': UNSET, **listing.fitting(self._tp)})

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self.widget.value is UNSET:
            missing.append(FieldError(None, loc, 'choose a record'))
            return UNSET
        return self.widget.value

    def set(self, value: Any) -> None:
        _pick(self.widget, value)

    def panel(self) -> Any:
        return self.widget


class _Nothing:
    """``None``: no value."""

    def value(self, loc: Loc, missing: _Missing) -> None:
        return None

    def set(self, value: Any) -> None:
        pass

    def refresh(self, listing: Listing) -> None:
        pass

    def panel(self) -> Any:
        return pn.Spacer(height=0)


class _Choice:
    """
    The ways of giving a value, each a widget: the alternatives of a union
    of groups, whose tag field is ``tag``; a group, a file, or a scalar
    given; an output of a record; ``None``. A radio button per way that is
    offered, hidden if it offers one way only, and the widget of the chosen
    way.

    It starts at its default's way. Without a default, it starts unset,
    unless it has one way only. Set, each way not chosen is as it starts, so
    that it shows no value set before, such as a row shown before.
    """

    GIVEN = 'Given'
    RECORD = 'From a record'
    FILE = 'A file'
    NONE = 'None'

    def __init__(self, ways: dict[str, _Widget], tag: str | None, default: Any) -> None:
        self._ways = ways
        self._tag = tag
        self._default = default
        self._selector = pn.widgets.RadioBoxGroup(options=list(ways), inline=True)
        self._selector.param.watch(lambda _: self._show(), 'value')
        self._panel = pn.Column(
            self._selector,
            *(w.panel() for w in ways.values()),
            sizing_mode='stretch_width',
        )
        self.set(UNSET)

    def _show(self) -> None:
        for label, way in self._ways.items():
            way.panel().visible = label == self._selector.value

    def refresh(self, listing: Listing) -> None:
        for way in self._ways.values():
            way.refresh(listing)
        self._offer()

    def _offer(self) -> None:
        chosen = self._selector.value
        self._selector.options = list(self._ways)
        self._selector.value = chosen  # Panel picks the first option otherwise
        self._selector.visible = len(self._selector.options) > 1 or chosen is None
        self._show()

    @property
    def offered(self) -> list[str]:
        return list(self._selector.options)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        chosen = self._selector.value
        return UNSET if chosen is None else self._ways[chosen].value(loc, missing)

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        label = self._label(value)
        self._selector.value = label
        for way_label, way in self._ways.items():
            way.set(value if way_label == label else UNSET)
        self._offer()

    def _label(self, value: Any) -> str | None:
        """The label of the way that writes ``value``, or ``None`` if unset."""
        if value is UNSET:
            return next(iter(self._ways)) if len(self._ways) == 1 else None
        if value is None:
            return self.NONE
        ref = as_ref(value)
        if isinstance(ref, OutputRef):
            return self.RECORD
        if isinstance(ref, FileRef):
            return self.FILE
        if self._tag is None:
            return self.GIVEN
        tag = {self._tag: dict(value)[self._tag]}
        for label, way in self._ways.items():
            if isinstance(way, _Group) and way.tag == tag:
                return label
        raise ValueError(f'{tag[self._tag]}: not an alternative')

    def panel(self) -> Any:
        return self._panel


class _Rows:
    """
    A list of ``element``, not a scalar: a table with a row per element, and
    a column per field of the element, of the tag and the fields of every
    alternative of a union, and of the file and the output that a row may
    be given as, such as ``File`` and ``Record`` for ``list[Run |
    Referenced[X]]``; and below it the editor of a row, the widget of the
    element's type. A cell is empty where an alternative lacks the field,
    and shows a group or a list as its JSON text. The rows are held as a
    request writes them, so the widgets do not grow with the rows.

    The editor shows the row selected, and is empty if none or several are.
    ``Update row`` writes the editor back to the row selected, and ``Add as
    new row`` appends it, each only if nothing is missing in it, so that every
    row is complete. ``Remove`` removes the rows selected, with any changes to
    them in the editor. ``Discard`` shows the selection in the editor again.

    So that a change in the editor is not lost unseen, the selection stays
    while the editor differs from what it showed, and the params refuse it. A
    refresh is no change: a record gone from the listing leaves its row as it
    is, as a row the editor cannot show whole.

    Unset, it has no row until one is added or ``Remove`` is clicked, which
    gives the empty list. A row set that the editor cannot show whole, such
    as a reference to an output that the client may not read now, is kept
    as given.
    """

    def __init__(self, element: Any, default: Any, listing: Listing) -> None:
        self._default = default
        self._listing = listing
        self._rows: list[Any] | None = None  # unset
        # The selection that the editor shows, which the table's differs from
        # only while it is being restored.
        self._selection: list[int] = []
        # What the editor held as it showed the selection, which differs from
        # the row if the editor cannot show it whole.
        self._shown: tuple[Any, _Missing] = (UNSET, [])
        tp, _, metadata = field_types.unwrapped(element)
        inline, inline_metadata, referenced = field_types.split(tp, *metadata)
        self._marker = field_types.file_marker(inline, *inline_metadata)
        # The tag field of a union, and the title of each alternative by tag.
        self._tag: str | None = None
        self._titles: dict[str, str] = {}
        groups = [] if inline is None or self._marker is not None else [inline]
        if (union := field_types.alternatives(inline, *inline_metadata)) is not None:
            self._tag, alternatives = union
            self._titles = {
                value: field_types.title(value, group.model_fields[self._tag])
                for value, group in alternatives.items()
            }
            groups = list(alternatives.values())
        # The title of each field, of every alternative, and its marker if a
        # file field.
        self._fields: dict[str, tuple[str, File | None]] = {}
        for group in groups:
            for name, info in group.model_fields.items():
                if name != self._tag and name not in self._fields:
                    self._fields[name] = (
                        field_types.title(name, info),
                        field_types.file_of(info),
                    )
        # The columns of the file and of the output that a row may be given as.
        self._refs: dict[str, type[FileRef | OutputRef]] = {}
        if self._marker is not None:
            self._refs['File'] = FileRef
        if referenced:
            self._refs['Record'] = OutputRef
        self._editor = _widget('', element, (), UNSET, listing)
        self._table = pn.widgets.Tabulator(
            disabled=True,
            selectable=True,
            sortable=False,
            show_index=False,
            layout='fit_columns',
            sizing_mode='stretch_width',
        )
        self._table.param.watch(lambda _: self._select(), 'selection')
        self._message = pn.pane.Markdown()
        add = pn.widgets.Button(label='Add as new row')
        add.on_click(lambda _: self._write(None))
        self._update = pn.widgets.Button(label='Update row', disabled=True)
        self._update.on_click(lambda _: self._write(self._selected()))
        remove = pn.widgets.Button(label='Remove')
        remove.on_click(lambda _: self._remove())
        discard = pn.widgets.Button(label='Discard')
        discard.on_click(lambda _: self._load())
        self._panel = pn.Column(
            self._table,
            self._editor.panel(),
            pn.Row(add, self._update, remove, discard),
            self._message,
            sizing_mode='stretch_width',
        )
        self.set(UNSET)

    def _selected(self) -> int | None:
        """The row selected, if one is."""
        return self._selection[0] if len(self._selection) == 1 else None

    def _held(self) -> tuple[Any, _Missing]:
        """
        The editor's value and what is missing in it, which names any changes
        in the editor of a table within this editor.
        """
        missing: _Missing = []
        return self._editor.value((), missing), missing

    def _select(self) -> None:
        """Show the selection in the editor, or restore it if the editor changed."""
        if self._table.selection == self._selection:
            return
        if self._held() != self._shown:
            self._table.selection = self._selection
            self._message.object = _HOLDS_CHANGES
            return
        self._selection = self._table.selection
        self._load()

    def _load(self) -> None:
        """Show the row selected in the editor, or nothing if none is."""
        i = self._selected()
        self._editor.set(UNSET if i is None else (self._rows or [])[i])
        self._shown = self._held()
        self._update.disabled = i is None
        self._message.object = ''

    def _write(self, i: int | None) -> None:
        """Write the editor to the row ``i``, or a new row, if nothing is missing."""
        value, missing = self._held()
        if value is UNSET and not missing:  # a choice with no way chosen
            missing.append(FieldError(None, (), 'choose a way'))
        self._message.object = '\n'.join(f'- {e}' for e in missing)
        if missing:
            return
        self._shown = (value, missing)
        rows = self._rows or []
        if i is None:
            self._rows = [*rows, value]
            self._render()
            self._table.selection = [len(self._rows) - 1]
        else:
            rows[i] = value
            self._render()

    def _remove(self) -> None:
        selected = set(self._selection)
        self._rows = [r for i, r in enumerate(self._rows or []) if i not in selected]
        if selected:
            self._selection = []
            self._table.selection = []
            self._load()
        self._render()

    def _render(self) -> None:
        """Show the rows in the table, a file or an output by its label."""
        rows = self._rows or []
        listed: dict[File, dict[FileRef, str]] = {}

        def text(value: Any, marker: File | None) -> str:
            ref = as_ref(value)
            if isinstance(ref, OutputRef):
                return self._listing.output_label(ref)
            if isinstance(ref, FileRef) and marker is not None:
                if marker not in listed:
                    files = self._listing.files(marker)
                    listed[marker] = {r: label for label, r in files.items()}
                return listed[marker].get(ref, str(ref))
            if isinstance(value, dict | list):
                return to_json(value).decode()
            return '' if value is None else str(value)

        refs = [as_ref(r) for r in rows]
        columns: dict[str, list[str]] = {}
        if self._tag is not None:
            columns[field_types.title(self._tag, FieldInfo())] = [
                self._titles[r[self._tag]] if ref is None else ''
                for r, ref in zip(rows, refs, strict=True)
            ]
        for name, (title, marker) in self._fields.items():
            columns[title] = [
                text(r.get(name), marker) if ref is None else ''
                for r, ref in zip(rows, refs, strict=True)
            ]
        for column, kind in self._refs.items():
            columns[column] = [
                text(r, self._marker) if isinstance(ref, kind) else ''
                for r, ref in zip(rows, refs, strict=True)
            ]
        # Of text, since Tabulator formats a column by its type when first
        # shown, and a column without rows would be one of numbers.
        self._table.value = pd.DataFrame(columns, dtype=str)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self._rows is None:
            missing.append(FieldError(None, loc, 'add a row, or remove for none'))
            return UNSET
        if self._held() != self._shown:
            i = self._selected()
            at = loc if i is None else (*loc, i)
            missing.append(FieldError(None, at, _HOLDS_CHANGES))
        return list(self._rows)

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        if value is UNSET:
            self._rows = None
        else:
            self._rows = []
            for item in value:
                self._editor.set(item)
                given, missing = self._held()
                self._rows.append(item if missing else given)
        self._selection = []
        self._table.selection = []
        self._load()
        self._render()

    def refresh(self, listing: Listing) -> None:
        self._listing = listing
        changed = self._held() != self._shown
        self._editor.refresh(listing)
        if not changed:
            self._shown = self._held()
        self._render()

    def panel(self) -> Any:
        return self._panel


class Form:
    """
    The params of one spec: a widget per parameter, listing what ``client``
    may give at each field.
    """

    def __init__(self, spec: WorkflowSpec[Any, Any], client: Client) -> None:
        self.spec = spec
        self._client = client
        listing = Listing(client)
        fields = spec.params.model_fields
        self._params = {
            name: _field(name, info, listing) for name, info in fields.items()
        }
        self._panel = pn.Column(
            pn.pane.Markdown(f'**{spec.title}**: {spec.description}'),
            pn.Tabs(
                *(
                    pn.Column(
                        w.panel(),
                        name=field_types.title(name, fields[name]),
                        sizing_mode='stretch_width',
                    )
                    for name, w in self._params.items()
                ),
                tabs_location='left',
                sizing_mode='stretch_width',
                max_width=800,
            ),
            sizing_mode='stretch_width',
        )

    @property
    def params(self) -> dict[str, Any]:
        """
        The params as a request gives them; a field with no way chosen is
        left out. Raises :class:`~ess.apps.SubmitError` at each field
        where a way is chosen, or an input or a list is, with nothing given::

            beam_centre: choose a record
            reduction.bins: give a value
            sample: add a row, or remove for none
            sample[1]: the editor holds changes: add or update a row, or discard them
        """
        missing: _Missing = []
        given = {n: w.value((n,), missing) for n, w in self._params.items()}
        if missing:
            raise SubmitError(missing)
        return {n: v for n, v in given.items() if v is not UNSET}

    def set(self, params: Mapping[str, Any]) -> None:
        """
        Show ``params``, as a record or a request holds them, such as
        ``record.request.params``; a field left out is as it starts, at its
        default or unset. A reference that the client may not read now
        chooses the way of a record, with no record picked; a file that
        is not listed stays picked.
        """
        if unknown := set(params) - set(self._params):
            raise KeyError(f'{self.spec.title} has no parameters {sorted(unknown)}')
        self.refresh()
        for name, widget in self._params.items():
            widget.set(params.get(name, UNSET))

    def ways(self, name: str) -> list[str]:
        """
        The ways offered at the parameter ``name``, a choice, such as
        ``['Given', 'From a record']``. Raises ``TypeError`` for a
        parameter of another widget, such as a file or a list.
        """
        if not isinstance(widget := self._params[name], _Choice):
            raise TypeError(f'{name}: not a choice of ways')
        return widget.offered

    def refresh(self, listing: Listing | None = None) -> None:
        """
        List the files and the records that fit again, from ``listing``,
        or from one looked up now.
        """
        listing = Listing(self._client) if listing is None else listing
        for widget in self._params.values():
            widget.refresh(listing)

    def panel(self) -> Any:
        return self._panel

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A form for a spec's params, generated from its pydantic models, on a client.

The form gives the params as a request does: the JSON form of each place given
inline, and a reference at each other place. It is filled from params as a
record holds them, so that a record's request can be edited and submitted
again.

The form has a tab per parameter, with the tabs on the left, as
esslivedata's dashboard has. The widget follows from what a request writes,
as :func:`ess.spec.places.place` describes a parameter, and is labelled by
:func:`ess.spec.places.title`, as the params schema is, a parameter by the
title of its tab:

=========================  ================================================
Where                      Widget
=========================  ================================================
a parameter, :class:`Ways` a choice of ways: the fields of each group written
                           inline, an output of a record that fits
                           ``Ways.record``, or for a file model a file
                           that fits it, and ``None``. A record is offered
                           while an output fits.
a list parameter,          a row per element, labelled by its position:
:class:`Rows`              the choice of ways of the element, as at a
                           parameter of its type, side by side; and
                           buttons that add a row and remove the last
a field of a group         an input of its type, or a picker of its values;
                           for a list of scalars, a list of these; for a
                           file model, a picker of the files that fit
                           it, with ``None`` if it is typed ``X | None``
=========================  ================================================

A record's output fits if its type, from the record's spec, fits the place,
and a request of the client may reference it now (``client.readable``), so a
pending record is offered too. A file fits a file model if a
:class:`~ess.apps.Selector` of the model's metadata matches it, as the
backend checks when the request is submitted. A file that the form is set
to, such as one a record names, stays picked when the listing no longer has
it.

The widgets follow those of esslivedata's dashboard (scipp/esslivedata#1426),
in Panel. A place without a default starts unset, so that a value a correct
reduction needs is never filled in for the user; so does a list, of rows or
of scalars, until an element is added or removed, so that a table, which has
no default, is never given without rows by mistake. A place is left out of
the request only if it is a choice with no way chosen, which it is only
without a default, so the backend refuses it as required. A way chosen, or
an input, with nothing given is not left out, since the backend would fill
in the default: the params name it as missing (see :attr:`Form.params`). An
empty text is the value ``''`` where the place has a default, and nothing
given where it has none, since a text input cannot tell an empty text from
none; an element of a list of texts is a text, ``''`` too, since it was
added.

A time is picked to the second, in the zone of the value the picker was
set to, or of the default, which its label names: ``Start (UTC)``. A time
without a zone stays without one, and a time of ``AwareDatetime`` without a
value is in UTC. The fraction of a second of the value is kept.

Not covered: a list parameter of a union of groups, whose row would pick its
alternative, and a list of file models at a field of a group.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from enum import Enum
from typing import Any, get_args

import panel as pn
from pydantic import AwareDatetime, BaseModel, NaiveDatetime
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python

from ess.apps import Client, FieldError, Selector, SubmitError
from ess.spec import (
    FileModel,
    FileRef,
    OutputRef,
    WorkflowSpec,
    field_types,
    places,
)
from ess.spec.places import Ways
from ess.spec.refs import Loc, as_ref, iter_refs


class _Unset:
    def __repr__(self) -> str:
        return 'UNSET'


UNSET: Any = _Unset()
"""
The value of a place that the request leaves out; given to ``set``, the
place as it starts, at its default or unset.
"""

type _Missing = list[FieldError]
"""The places with nothing given, each with what the user is to do."""


class Listing:
    """
    What a request of ``client`` may name now, looked up once for a refresh
    of any number of forms: the files of each file model and the
    client's records, each by a label, and the outputs a request may
    reference.

    It stands in for two queries the client lacks (scipp/essapps#95). The
    client lists no records by the type of their outputs (gap 1), so
    :meth:`fitting` checks the type of each readable output. A record has no
    short name (gap 4), so its label is the start of its id, its spec, and
    the files and records its request names: ``2f9c01 Find centre, from
    run 101``.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._files: dict[str, dict[str, FileRef]] = {}

        def source(ref: Any) -> str:
            if not isinstance(ref, FileRef):
                return str(ref.record[:6])
            return _file_label(ref, self._known(ref))

        self.records = client.records()
        specs = {s: client.spec(s) for s in {r.spec for r in self.records}}
        self.labels: dict[str, str] = {}
        for record in self.records:
            refs = iter_refs(record.request.params)
            sources = ', '.join(source(ref) for _, ref in refs)
            label = f'{record.id[:6]} {specs[record.spec].title}'
            self.labels[record.id] = f'{label}, from {sources}' if sources else label
        readable = client.readable(self.records)
        self._outputs = [
            (
                f'{self.labels[record.id]}: {name}',
                record.ref(name),
                specs[record.spec].outputs.model_fields[name].annotation,
            )
            for record, names in reversed(
                list(zip(self.records, readable, strict=True))
            )
            for name in names
        ]

    def files(self, model: type[FileModel]) -> dict[str, FileRef]:
        """The files that fit ``model``, each by a label, in the order measured."""
        selector = Selector(**model.metadata)
        if (key := repr(selector)) not in self._files:
            listed = self._client.files.list(selector)
            self._files[key] = {}
            for ref in listed:
                fields = self._known(ref)
                role = f' ({fields["role"]})' if 'role' in fields else ''
                self._files[key][_file_label(ref, fields) + role] = ref
        return self._files[key]

    def _known(self, ref: FileRef) -> dict[str, Any]:
        """The metadata of a file, looked up once."""
        if ref not in self._metadata:
            self._metadata[ref] = self._client.files.metadata(ref)
        return self._metadata[ref]

    def fitting(self, tp: Any) -> dict[str, OutputRef]:
        """The outputs a request may reference that fit ``tp``, newest first."""
        return {
            label: ref for label, ref, out in self._outputs if field_types.fits(out, tp)
        }


def _file_label(ref: FileRef, metadata: Mapping[str, Any]) -> str:
    """
    ``run 101`` where the metadata has a run, else the file's identity:
    the contract of a file source names no field but ``proposal``.
    """
    run = metadata.get('run')
    return str(ref) if run is None else f'run {run}'


def _default(info: FieldInfo) -> Any:
    return UNSET if info.is_required() else info.get_default(call_default_factory=True)


def _parameter(model: type[BaseModel], name: str, listing: Listing) -> _Choice | _Rows:
    """The widget of the parameter ``name`` of ``model``, the params model."""
    info = model.model_fields[name]
    label = places.title(name, info)
    described = places.place(model, name)
    if isinstance(described, Ways):
        return _Choice(label, _ways(described, listing), described.tag, _default(info))
    if described.element.tag is not None:
        raise NotImplementedError(f'{name}: a list of a union of groups')
    return _Rows(label, described.element, _default(info), listing)


def _ways(described: Ways, listing: Listing, row: bool = False) -> dict[str, _Way]:
    """
    The widget of each way of a place that ``described`` describes, by label;
    in a ``row`` of a table, the fields of a group side by side.
    """
    ways: dict[str, _Way] = {}
    for value, group in described.inline.items():
        if value is None or described.tag is None:
            ways['Given'] = _Group(group, {}, listing, row)
        else:
            way = places.title(value, group.model_fields[described.tag])
            ways[way] = _Group(group, {described.tag: value}, listing, row)
    if field_types.is_file_model(described.record):
        ways[_Choice.FILE] = _File(described.record, listing)
    elif described.record is not None:
        ways[_Choice.RECORD] = _Record(described.record, listing)
    if described.none:
        ways[_Choice.NONE] = _Nothing()
    return ways


def _field(
    group: type[BaseModel], name: str, listing: Listing
) -> _Scalar | _Scalars | _File:
    """
    The input of the field ``name`` of ``group``, written inline, or the
    picker of a file model.
    """
    info = group.model_fields[name]
    if (model := field_types.file_model(info.annotation)) is not None:
        if field_types.is_list(group, name):
            raise NotImplementedError(f'{name}: a list of file models at a field')
        optional = field_types.optional(info.annotation)[1]
        return _File(model, listing, places.title(name, info), optional)
    widget = _Scalars if field_types.is_list(group, name) else _Scalar
    return widget(
        places.title(name, info),
        field_types.value_type(info.annotation),
        _default(info),
        info.description,
    )


def _picked(widget: Any, options: Mapping[str, Any]) -> None:
    """Give a picker ``options``; its value stays if it is one of them."""
    value = widget.value
    widget.options = dict(options)
    _pick(widget, value)


def _pick(widget: Any, value: Any) -> None:
    """Pick ``value`` if the picker lists it, else nothing."""
    widget.value = value if value in widget.options.values() else None


class _File:
    """
    A file model: a picker of the files that fit it, labelled ``name``
    at a field of a group, and with ``None`` if ``optional``. A file that
    the form is set to and the listing lacks stays picked, labelled by its
    reference as given, an identity or a name such as ``run:101``.
    """

    def __init__(
        self,
        model: type[FileModel],
        listing: Listing,
        name: str | None = None,
        optional: bool = False,
    ) -> None:
        self._model = model
        self._optional = optional
        self._listed: dict[str, Any] = {}
        self.widget = pn.widgets.Select(label=name or '', sizing_mode='stretch_width')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        nothing = '(none)' if self._optional else '(choose a file)'
        self._listed = {nothing: None, **listing.files(self._model)}
        self.set(self.widget.value)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self.widget.value is None and not self._optional:
            missing.append(FieldError(None, loc, 'choose a file'))
            return UNSET
        return self.widget.value

    def set(self, value: Any) -> None:
        # A reference, or its JSON form, as a request may give it.
        ref = None if value is UNSET else as_ref(value)
        picked = ref if isinstance(ref, FileRef) else None
        options = dict(self._listed)
        if picked is not None and picked not in options.values():
            options[str(picked)] = picked
        self.widget.options = options
        self.widget.value = picked

    def panel(self) -> Any:
        return self.widget


_TIMES = (datetime, AwareDatetime, NaiveDatetime)


class _Scalar:
    """
    A scalar: an ``int``, ``float``, or ``str`` input, or a picker of a time,
    that may be unset, or for a ``bool``, an enum, or a ``Literal``, a picker
    of its values with an option for none, so that it may start unset. It
    gives the JSON form of its value.
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
        elif tp in _TIMES:
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
        return pn.widgets.Select(options={'(choose)': None, **values}, **options)

    def value(self, loc: Loc, missing: _Missing) -> Any:
        value = self.widget.value
        if value is None or (value == '' and self._default is UNSET):
            missing.append(FieldError(None, loc, 'give a value'))
            return UNSET
        if self._type in _TIMES:
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
            value = '' if self._type is str else None
        elif isinstance(self._type, type) and issubclass(self._type, Enum):
            value = self._type(value)  # a record holds an enum by its value
        if self._type in _TIMES:
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

    def panel(self) -> Any:
        return self.widget


class _List:
    """
    A list: a widget per element, labelled by its position, ``Banks [0]``,
    and buttons that add an element and remove the last. Unset, it has no
    element until one is added or removed: a removal gives no element, the
    empty list. A subclass makes the widget of an element, and the panel.
    """

    _unset = 'add an element, or remove for none'

    def __init__(self, label: str, default: Any) -> None:
        self._label = label
        self._default = default
        self._elements: list[Any] | None = None  # unset
        self._column = pn.Column(sizing_mode='stretch_width')
        add = pn.widgets.Button(label=f'Add to {label}')
        add.on_click(lambda _: self._resize(len(self._elements or []) + 1))
        remove = pn.widgets.Button(label=f'Remove from {label}')
        remove.on_click(lambda _: self._resize(len(self._elements or []) - 1))
        self._buttons = pn.Row(add, remove)
        self.set(UNSET)

    def _element(self, label: str) -> Any:
        """The widget of an element labelled ``label``, with nothing given."""
        raise NotImplementedError

    def _resize(self, n: int) -> None:
        """Keep the first ``n`` elements, none for ``-1``, or add elements."""
        elements = self._elements or []
        del elements[n:]
        elements.extend(
            self._element(f'{self._label} [{i}]') for i in range(len(elements), n)
        )
        self._elements = elements
        self._column.objects = [e.panel() for e in elements]

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self._elements is None:
            missing.append(FieldError(None, loc, self._unset))
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


class _Scalars(_List):
    """
    A list of scalars: an input per element, as :class:`_Scalar` has it. An
    element added has nothing given, but a text, which is ``''``.
    """

    def __init__(
        self, label: str, tp: Any, default: Any, description: str | None = None
    ) -> None:
        self._type = tp
        self._description = description
        super().__init__(label, default)
        self._panel = pn.Column(self._column, self._buttons)

    def _element(self, label: str) -> _Scalar:
        default = '' if self._type is str else UNSET
        return _Scalar(label, self._type, default, self._description)

    def panel(self) -> Any:
        return self._panel


class _Group:
    """
    The fields of a group, each an input, one below the other, or side by
    side in a ``row`` of a table; the tag of an alternative has none.
    """

    def __init__(
        self,
        group: type[BaseModel],
        tag: dict[str, str],
        listing: Listing,
        row: bool = False,
    ) -> None:
        self.tag = tag
        self._fields = {
            name: _field(group, name, listing)
            for name in group.model_fields
            if name not in tag
        }
        layout = pn.Row if row else pn.Column
        self._panel = layout(
            *(w.panel() for w in self._fields.values()), sizing_mode='stretch_width'
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
            if isinstance(field, _File):
                field.refresh(listing)

    def panel(self) -> Any:
        return self._panel


class _Record:
    """An output of a record that fits the place: a picker of references."""

    def __init__(self, tp: type[BaseModel], listing: Listing) -> None:
        self._tp = tp
        self.widget = pn.widgets.Select(sizing_mode='stretch_width')
        self.refresh(listing)

    @property
    def offered(self) -> bool:
        return len(self.widget.options) > 1

    def refresh(self, listing: Listing) -> None:
        _picked(self.widget, {'(choose a record)': None, **listing.fitting(self._tp)})

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self.widget.value is None:
            missing.append(FieldError(None, loc, 'choose a record'))
            return UNSET
        return self.widget.value

    def set(self, value: Any) -> None:
        _pick(self.widget, value)

    def panel(self) -> Any:
        return self.widget


class _Nothing:
    """``None``: without the group."""

    def value(self, loc: Loc, missing: _Missing) -> None:
        return None

    def set(self, value: Any) -> None:
        pass

    def refresh(self, listing: Listing) -> None:
        pass

    def panel(self) -> Any:
        return pn.Spacer(height=0)


_Way = _Group | _Record | _File | _Nothing


class _Choice:
    """
    A group, or a union of groups, possibly typed ``X | None``: a radio
    button per way that is offered, and the widget of the chosen way, in the
    tab of a parameter titled ``label``; in a ``row`` of a table, these side
    by side, after the label.

    It starts at its default's way. Without a default, it starts unset,
    unless there is no ``None`` and one way only is written inline, or none
    is: then the choice of a record over the fields is not one of meaning.
    The way of a record is offered while an output fits, while it is chosen,
    or if it is the only way.
    """

    RECORD = 'From a record'
    FILE = 'A file'
    NONE = 'None'

    def __init__(
        self,
        label: str,
        ways: dict[str, _Way],
        tag: str | None,
        default: Any,
        row: bool = False,
    ) -> None:
        self._ways = ways
        self._tag = tag
        self._default = default
        self._selector = pn.widgets.RadioBoxGroup(options=list(ways), inline=True)
        self._selector.param.watch(lambda _: self._show(), 'value')
        widgets = (self._selector, *(w.panel() for w in ways.values()))
        self._panel = (
            pn.Row(pn.pane.Markdown(label, width=100), *widgets)
            if row
            else pn.Column(*widgets, name=label)
        )
        self._panel.sizing_mode = 'stretch_width'
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
        self._selector.options = [
            label
            for label, way in self._ways.items()
            if not isinstance(way, _Record)
            or way.offered
            or label == chosen
            or len(self._ways) == 1
        ]
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
        if label is not None:
            self._ways[label].set(value)
        self._offer()

    def _label(self, value: Any) -> str | None:
        """The label of the way that writes ``value``, or ``None`` if unset."""
        if value is UNSET:
            groups = [label for label, w in self._ways.items() if isinstance(w, _Group)]
            refs = [w for w in (self.RECORD, self.FILE) if w in self._ways]
            first = groups or refs
            return first[0] if len(first) == 1 and self.NONE not in self._ways else None
        if value is None:
            return self.NONE
        if isinstance(value, OutputRef):
            return self.RECORD
        if isinstance(value, FileRef):
            return self.FILE
        tag = {} if self._tag is None else {self._tag: dict(value)[self._tag]}
        for label, way in self._ways.items():
            if isinstance(way, _Group) and way.tag == tag:
                return label
        raise ValueError(f'{tag[str(self._tag)]}: not an alternative')

    def panel(self) -> Any:
        return self._panel


class _Rows(_List):
    """
    A list parameter: in its tab, titled by its name, a row per element,
    labelled by its position, ``Sample [0]``: the choice of ways of the
    element, as a parameter of its type has it, with the widgets side by
    side, as the columns of a table. A row added starts as such a parameter
    without a default does: at its one way, since the element is neither a
    union nor typed ``X | None``.
    """

    _unset = 'add a row, or remove for none'

    def __init__(
        self, label: str, element: Ways, default: Any, listing: Listing
    ) -> None:
        self._element_ways = element
        self._listing = listing
        super().__init__(label, default)
        self._panel = pn.Column(
            self._column, self._buttons, name=label, sizing_mode='stretch_width'
        )

    def _element(self, label: str) -> _Choice:
        ways = _ways(self._element_ways, self._listing, row=True)
        return _Choice(label, ways, self._element_ways.tag, UNSET, row=True)

    def refresh(self, listing: Listing) -> None:
        self._listing = listing
        for row in self._elements or []:
            row.refresh(listing)

    def panel(self) -> Any:
        return self._panel


class Form:
    """
    The params of one spec: a widget per parameter, listing what ``client``
    may give at each place.
    """

    def __init__(self, spec: WorkflowSpec[Any, Any], client: Client) -> None:
        self.spec = spec
        self._client = client
        listing = Listing(client)
        self._params = {
            name: _parameter(spec.params, name, listing)
            for name in spec.params.model_fields
        }
        self._panel = pn.Column(
            pn.pane.Markdown(f'**{spec.title}**: {spec.description}'),
            pn.Tabs(
                *(w.panel() for w in self._params.values()),
                tabs_location='left',
                sizing_mode='stretch_width',
            ),
            sizing_mode='stretch_width',
        )

    @property
    def params(self) -> dict[str, Any]:
        """
        The params as a request gives them; a place with no way chosen is
        left out. Raises :class:`~ess.apps.SubmitError` at each place
        where a way is chosen, or an input or a list is, with nothing given::

            beam_centre: choose a record
            reduction.bins: give a value
            sample: add a row, or remove for none
            sample[0]: choose a file
        """
        missing: _Missing = []
        given = {n: w.value((n,), missing) for n, w in self._params.items()}
        if missing:
            raise SubmitError(missing)
        return {n: v for n, v in given.items() if v is not UNSET}

    def set(self, params: Mapping[str, Any]) -> None:
        """
        Show ``params``, as a record or a request holds them, such as
        ``record.request.params``; a place left out is as it starts, at its
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
        The ways offered at the parameter ``name``, a group or a union, such
        as ``['Given', 'From a record']``; a list parameter has a choice of
        ways per row.
        """
        if not isinstance(widget := self._params[name], _Choice):
            raise TypeError(f'{name}: a list parameter has a choice of ways per row')
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

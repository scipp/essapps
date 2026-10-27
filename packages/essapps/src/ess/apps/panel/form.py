# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A form for a spec's params, generated from its pydantic models, on a client.

The form gives the params as a request does: the JSON form of each place given
inline, and a reference at each other place. It is filled from params as a
record holds them, so that a record's request can be edited and submitted
again.

The widget at a place follows from what a request writes there, as
:func:`ess.spec.places.place` describes it, and is labelled by
:func:`ess.spec.places.title`, as the params schema is:

=========================  ================================================
Place                      Widget
=========================  ================================================
:class:`File`              a picker of the client's datasets
:class:`Ways`              a choice of ways: the fields of each group written
                           inline, an output of a record that fits
                           ``Ways.record``, and ``None``. A record is offered
                           while an output fits.
:class:`Value`             an input for the scalar, or a picker of its
                           values
=========================  ================================================

A record's output fits if its type, from the record's spec, fits the place,
and a request of the client may reference it now (``client.readable``), so a
pending record is offered too.

The widgets follow those of esslivedata's dashboard (scipp/esslivedata#1426),
in Panel. A place without a default starts unset, so that a value a correct
reduction needs is never filled in for the user. A place is left out of the
request only if it is a choice with no way chosen, which it is only without
a default, so the backend refuses it as required. A way chosen, or an input,
with nothing given is not left out, since the backend would fill in the
default: the params name it as missing (see :attr:`Form.params`). An empty
text is the value ``''`` where the place has a default, and nothing given
where it has none, since a text input cannot tell an empty text from none.

Not covered: list parameters (tables, :class:`Rows`), scipp/essapps#101.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, get_args

import panel as pn
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ess.apps import Client, FieldError, Selector, SubmitError
from ess.spec import DatasetRef, OutputRef, WorkflowSpec, field_types, places
from ess.spec.places import File, Rows, Value
from ess.spec.refs import Loc, iter_refs


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
    of any number of forms: the datasets and the client's records, each by a
    label, and the outputs a request may reference.

    It stands in for two queries the client lacks (scipp/essapps#95). The
    client lists no records by the type of their outputs (gap 1), so
    :meth:`fitting` checks the type of each readable output. A record has no
    short name (gap 4), so its label is the start of its id, its spec, and
    the datasets and records its request names: ``2f9c01 Find centre, from
    run 101``.
    """

    def __init__(self, client: Client) -> None:
        metadata = {
            r: client.datasets.metadata(r) for r in client.datasets.list(Selector())
        }
        self.datasets: dict[str, DatasetRef] = {}
        for ref, fields in metadata.items():
            role = f' ({fields["role"]})' if 'role' in fields else ''
            self.datasets[_dataset_label(ref, fields) + role] = ref

        def source(ref: Any) -> str:
            if not isinstance(ref, DatasetRef):
                return str(ref.record[:6])
            known = metadata[ref] if ref in metadata else client.datasets.metadata(ref)
            return _dataset_label(ref, known)

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

    def fitting(self, tp: Any) -> dict[str, OutputRef]:
        """The outputs a request may reference that fit ``tp``, newest first."""
        return {
            label: ref for label, ref, out in self._outputs if field_types.fits(out, tp)
        }


def _dataset_label(ref: DatasetRef, metadata: Mapping[str, Any]) -> str:
    """
    ``run 101`` where the metadata has a run, else the dataset's identity:
    the contract of a dataset source names no field but ``proposal``.
    """
    run = metadata.get('run')
    return str(ref) if run is None else f'run {run}'


def _default(info: FieldInfo) -> Any:
    return UNSET if info.is_required() else info.get_default(call_default_factory=True)


def _place(
    model: type[BaseModel], name: str, listing: Listing
) -> _File | _Scalar | _Choice:
    """The widget for the value at the place of field ``name`` of ``model``."""
    info = model.model_fields[name]
    label = places.title(name, info)
    described = places.place(model, name)
    if isinstance(described, Rows):
        raise NotImplementedError(f'{name}: a list of places (a table)')
    if isinstance(described, File):
        return _File(label, listing)
    if isinstance(described, Value):
        return _Scalar(label, info, described.type)
    ways: dict[str, _Way] = {}
    for value, group in described.inline.items():
        if value is None or described.tag is None:
            ways['Given'] = _Group(group, listing, {})
        else:
            way = places.title(value, group.model_fields[described.tag])
            ways[way] = _Group(group, listing, {described.tag: value})
    if described.record is not None:
        ways[_Choice.RECORD] = _Record(described.record, listing)
    if described.none:
        ways[_Choice.NONE] = _Nothing()
    return _Choice(label, ways, described.tag, _default(info))


def _picked(widget: Any, options: Mapping[str, Any]) -> None:
    """Give a picker ``options``; its value stays if it is one of them."""
    value = widget.value
    widget.options = dict(options)
    _pick(widget, value)


def _pick(widget: Any, value: Any) -> None:
    """Pick ``value`` if the picker lists it, else nothing."""
    widget.value = value if value in widget.options.values() else None


class _File:
    """A file: a picker of datasets."""

    def __init__(self, label: str, listing: Listing) -> None:
        self.widget = pn.widgets.Select(label=label, sizing_mode='stretch_width')
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        _picked(self.widget, {'(choose a dataset)': None, **listing.datasets})

    def value(self, loc: Loc, missing: _Missing) -> Any:
        if self.widget.value is None:
            missing.append(FieldError(None, loc, 'choose a dataset'))
            return UNSET
        return self.widget.value

    def set(self, value: Any) -> None:
        _pick(self.widget, value)

    def panel(self) -> Any:
        return self.widget


class _Scalar:
    """
    A scalar, the value at any place of a valid spec that is not a file, a
    group, or a union: an ``int``, ``float``, or ``str`` input that may be
    unset, or for a ``bool``, an enum, or a ``Literal``, a picker of its
    values with an option for none, so that it may start unset.
    """

    def __init__(self, label: str, info: FieldInfo, tp: Any) -> None:
        self._type = tp
        self._default = _default(info)
        options: dict[str, Any] = {
            'label': label,
            'description': info.description,
            'sizing_mode': 'stretch_width',
        }
        self.widget: Any
        if tp is float:
            self.widget = pn.widgets.FloatInput(**options)
        elif tp is int:
            self.widget = pn.widgets.IntInput(**options)
        elif tp is str:
            self.widget = pn.widgets.TextInput(**options)
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
        return value.value if isinstance(value, Enum) else value

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        if value is UNSET:
            value = '' if self._type is str else None
        elif isinstance(self._type, type) and issubclass(self._type, Enum):
            value = self._type(value)  # a record holds an enum by its value
        self.widget.value = value

    def refresh(self, listing: Listing) -> None:
        pass

    def panel(self) -> Any:
        return self.widget


class _Group:
    """The fields of a group, each a place; the tag of an alternative has none."""

    def __init__(
        self, group: type[BaseModel], listing: Listing, tag: dict[str, str]
    ) -> None:
        self.tag = tag
        self._fields = {
            name: _place(group, name, listing)
            for name in group.model_fields
            if name not in tag
        }
        self._panel = pn.Column(
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
        for widget in self._fields.values():
            widget.refresh(listing)

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


_Way = _Group | _Record | _Nothing


class _Choice:
    """
    A group, or a union of groups, possibly typed ``X | None``: a box with a
    radio button per way that is offered, and the widget of the chosen way.

    It starts at its default's way. Without a default, it starts unset,
    unless there is no ``None`` and one way only is written inline, or none
    is: then the choice of a record over the fields is not one of meaning.
    The way of a record is offered while an output fits, while it is chosen,
    or if it is the only way.
    """

    RECORD = 'From a record'
    NONE = 'None'

    def __init__(
        self, label: str, ways: dict[str, _Way], tag: str | None, default: Any
    ) -> None:
        self._ways = ways
        self._tag = tag
        self._default = default
        self._selector = pn.widgets.RadioBoxGroup(options=list(ways), inline=True)
        self._selector.param.watch(lambda _: self._show(), 'value')
        self._panel = pn.Card(
            self._selector,
            *(w.panel() for w in ways.values()),
            title=label,
            collapsible=False,
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
            first = [label for label, w in self._ways.items() if isinstance(w, _Group)]
            first = first or [self.RECORD]
            return first[0] if len(first) == 1 and self.NONE not in self._ways else None
        if value is None:
            return self.NONE
        if isinstance(value, OutputRef):
            return self.RECORD
        tag = {} if self._tag is None else {self._tag: dict(value)[self._tag]}
        for label, way in self._ways.items():
            if isinstance(way, _Group) and way.tag == tag:
                return label
        raise ValueError(f'{tag[str(self._tag)]}: not an alternative')

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
            name: _place(spec.params, name, listing)
            for name in spec.params.model_fields
        }
        self._panel = pn.Column(
            pn.pane.Markdown(f'**{spec.title}**: {spec.description}'),
            *(w.panel() for w in self._params.values()),
            sizing_mode='stretch_width',
        )

    @property
    def params(self) -> dict[str, Any]:
        """
        The params as a request gives them; a place with no way chosen is
        left out. Raises :class:`~ess.apps.SubmitError` at each place
        where a way is chosen, or an input is, with nothing given::

            beam_centre: choose a record
            reduction.bins: give a value
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
        chooses the way of a record, with no record picked; a dataset that
        is not listed is not picked.
        """
        if unknown := set(params) - set(self._params):
            raise KeyError(f'{self.spec.title} has no parameters {sorted(unknown)}')
        self.refresh()
        for name, widget in self._params.items():
            widget.set(params.get(name, UNSET))

    def ways(self, name: str) -> list[str]:
        """
        The ways offered at the parameter ``name``, a group or a union, such
        as ``['Given', 'From a record']``.
        """
        widget = self._params[name]
        if not isinstance(widget, _Choice):
            raise TypeError(f'{name} is not a group or a union')
        return widget.offered

    def refresh(self, listing: Listing | None = None) -> None:
        """
        List the datasets and the records that fit again, from ``listing``,
        or from one looked up now.
        """
        listing = Listing(self._client) if listing is None else listing
        for widget in self._params.values():
            widget.refresh(listing)

    def panel(self) -> Any:
        return self._panel

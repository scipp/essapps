# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The fields of a form for a pydantic model of params, such as a spec's, without
widgets. :class:`ess.apps.panel.Form` shows them; another widget library would
show the same fields.

The fields give the params as a request does: at each field, at any depth,
the JSON form of a value, a file, a reference to an output, or ``None``. They
are filled from params as a record stores them, so that a record's request
can be edited and submitted again.

The field of a value follows from its type, as the table in
``packages/essspec/docs/params.md`` gives (see :func:`_make`), and is labelled by
:func:`ess.spec.field_types.title`.

A record's output fits a field if its type, from the record's spec, fits the
field, and a request of the client may reference it now
(``client.readable``), so a pending record is offered too. A file is offered for
a file field as :meth:`ess.apps.Listing.files` says, among those the workspace
accepts there. A file that the fields are set to, such as one a record names,
stays picked when the listing no longer has it.

A field without a default starts unset, so that a value a correct reduction
needs is never filled in for the user; so does a list, of rows or of scalars,
until an element is added or removed, so that a table, which has no default, is
never given without rows by mistake. A field is left out of the request only if
it is a choice with no way chosen, which it is only without a default, so the
workspace refuses it as required. A way chosen, or an input, with nothing given
is not left out, since the workspace would fill in the default: the params name
it as missing (see :attr:`Fields.params`). An empty text is the value ``''``
where the field has a default, and nothing given where it has none, since a
text input cannot tell an empty text from none; an element of a list of texts
is a text, ``''`` too, since it was added.

A time is picked to the second, in the zone of the value the picker was
set to, or of the default, which its label names: ``Start (UTC)``. A time
without a zone stays without one, and a time of ``AwareDatetime`` without a
value is in UTC. The fraction of a second of the value is kept.

A widget layer shows a field by reading its public attributes when the field
calls back (:meth:`Field.subscribe`), and gives it what a person does by setting
``value`` or calling its methods.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from enum import Enum
from numbers import Integral, Real
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_json, to_jsonable_python

from ess.spec import File, FileRef, OutputRef, field_types
from ess.spec.refs import Loc, as_ref

from .errors import FieldError, SubmitError
from .listing import Listing


class _Unset:
    def __repr__(self) -> str:
        return 'UNSET'


UNSET: Any = _Unset()
"""
The value of a field that the request leaves out; given to ``set``, the
field as it starts, at its default or unset.
"""

type Missing = list[FieldError]
"""The fields with nothing given, each with what the user is to do."""

type Changed = Callable[[], None]
"""Called when a field changed."""

_KINDS = {'float': Real, 'int': Integral, 'text': str, 'time': datetime}
"""What an input of each kind holds, besides ``None``."""

_HOLDS_CHANGES = 'the editor holds changes: add or update a row, or discard them'
"""What a user who changed the editor of a table is to do before going on."""


class Field:
    """
    A field of any type: ``collect(loc, missing)`` gives its value as a request
    writes it, ``set`` shows a value, ``refresh`` lists the files and records anew.
    """

    def __init__(self) -> None:
        self._subscribers: list[Changed] = []

    def subscribe(self, changed: Changed) -> None:
        """Call ``changed`` whenever this field or one inside it changes."""
        self._subscribers.append(changed)

    def _notify(self) -> None:
        for changed in self._subscribers:
            changed()

    def collect(self, loc: Loc, missing: Missing) -> Any:
        raise NotImplementedError

    def set(self, value: Any) -> None:
        raise NotImplementedError

    def refresh(self, listing: Listing) -> None:
        pass


class Input(Field):
    """A field a person gives through one input: ``value``, of ``options`` if any."""

    def __init__(self, label: str = '') -> None:
        super().__init__()
        self.label = label
        self.options: dict[str, Any] = {}
        self._value: Any = UNSET

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, value: Any) -> None:
        if value is not self._value and value != self._value:
            self._value = value
            self._notify()


def _make_field(name: str, info: FieldInfo, listing: Listing) -> Field:
    """The field ``name`` with ``info``, of a group or of the params."""
    return _make(
        field_types.title(name, info),
        info.annotation,
        field_types.field_metadata(info),
        UNSET if info.is_required() else info.get_default(call_default_factory=True),
        listing,
        info.description,
    )


def _make(
    label: str,
    annotation: Any,
    metadata: tuple[Any, ...],
    default: Any,
    listing: Listing,
    description: str | None = None,
) -> Field:
    """
    The field of a value of type ``annotation`` with ``metadata`` (see
    :func:`ess.spec.field_types.field_metadata`), at any depth.

    The type is read with :func:`ess.spec.field_types.split`, as the sources of its
    request type are (:func:`ess.spec.request_types.sources`). A scalar is an input
    of its type, and a file field a picker of the files to offer. Anything with
    more than one way to give the value is a :class:`ChoiceField`: the alternatives
    of a union of groups, a group (one way, ``Given``, whose selector is hidden), a
    scalar or a file field with ``None`` or a ``Referenced[...]`` member
    (``Given``, ``A file``), ``From a record``, and ``None``. A list of scalars is
    :class:`ScalarsField`, and any other list :class:`RowsField`.
    """
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if get_origin(tp) is list:
        (element,) = get_args(tp)
        if field_types.is_scalar(item := field_types.plain(element)):
            return ScalarsField(label, item, default, description)
        return RowsField(element, default, listing)
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    union = field_types.alternatives(inline, *inline_metadata)
    marker = field_types.file_marker(inline, *inline_metadata)
    if not (none or referenced or union):
        if marker is not None:
            return FileField(marker, listing, label)
        if not field_types.is_group(inline):
            return ScalarField(label, inline, default, description)
    ways: dict[str, Field] = {}
    if union is not None:
        tag, groups = union
        for value, group in groups.items():
            way = field_types.title(value, group.model_fields[tag])
            ways[way] = GroupField(group, {tag: value}, listing)
    elif marker is not None:
        ways[ChoiceField.FILE] = FileField(marker, listing)
    elif field_types.is_group(inline):
        ways[ChoiceField.GIVEN] = GroupField(inline, {}, listing)
    elif inline is not None:
        ways[ChoiceField.GIVEN] = ScalarField(label, inline, UNSET, description)
    if referenced:
        ways[ChoiceField.RECORD] = RecordField(Union[referenced], listing)  # noqa: UP007
    if none:
        ways[ChoiceField.NONE] = NothingField()
    return ChoiceField(ways, None if union is None else union[0], default)


class FileField(Input):
    """
    A file field: a picker of the files to offer for its marker
    (:meth:`~ess.apps.Listing.files`), labelled ``label``. A
    file that the field is set to and the listing lacks stays picked,
    labelled by its reference as given, an identity or a name such as
    ``run:101``.

    The option of nothing has the value :data:`UNSET`.
    """

    def __init__(self, marker: File, listing: Listing, label: str = '') -> None:
        super().__init__(label)
        self._marker = marker
        self._listed: dict[str, Any] = {}
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        self._listed = {'(choose a file)': UNSET, **listing.files(self._marker)}
        self.set(self.value)

    def collect(self, loc: Loc, missing: Missing) -> Any:
        if self.value is not UNSET:
            return self.value
        missing.append(FieldError(None, loc, 'choose a file'))
        return UNSET

    def set(self, value: Any) -> None:
        # A reference, or its JSON form, as a request may give it.
        ref = None if value is UNSET else as_ref(value)
        picked = ref if isinstance(ref, FileRef) else UNSET
        options = dict(self._listed)
        if picked is not UNSET and picked not in options.values():
            options[str(picked)] = picked
        self.options = options
        self._value = picked
        self._notify()


class ScalarField(Input):
    """
    A scalar: an ``int``, ``float``, or ``str`` input, or a picker of a time,
    that may be unset, or for a ``bool``, an enum, or a ``Literal``, a picker
    of its values with an option for none, :data:`UNSET` as at a file, so that it
    may start unset. It gives the JSON form of its value. ``set`` raises
    ``ValueError`` for a value of another type, and a value that a picker does
    not list leaves it unset.
    """

    def __init__(
        self, label: str, tp: Any, default: Any, description: str | None = None
    ) -> None:
        super().__init__(label)
        self._label = label
        self._type = tp
        self._default = default
        self.description = description
        self._zone: tzinfo | None = None
        self._time_set: datetime | None = None  # with the zone and fraction
        self.kind: Literal['float', 'int', 'text', 'time', 'pick'] = 'pick'
        if tp is float:
            self.kind = 'float'
        elif tp is int:
            self.kind = 'int'
        elif tp is str:
            self.kind = 'text'
        elif tp in field_types.TIMES:
            self.kind = 'time'
        elif tp is bool:
            self.options = {'(choose)': UNSET, 'True': True, 'False': False}
        elif isinstance(tp, type) and issubclass(tp, Enum):
            self.options = {'(choose)': UNSET, **{m.name: m for m in tp}}
        else:  # a Literal
            self.options = {'(choose)': UNSET, **{str(v): v for v in get_args(tp)}}
        self.set(UNSET)

    def collect(self, loc: Loc, missing: Missing) -> Any:
        value = self.value
        if value is None or value is UNSET or (value == '' and self._default is UNSET):
            missing.append(FieldError(None, loc, 'give a value'))
            return UNSET
        if self.kind == 'time':
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
            if self.kind != 'pick':
                value = '' if self.kind == 'text' else None
        elif isinstance(self._type, type) and issubclass(self._type, Enum):
            value = self._type(value)  # a record holds an enum by its value
        if self.kind == 'time':
            value = self._time(value)
        if self.kind == 'pick':
            value = value if value in self.options.values() else UNSET
        elif value is not None and not isinstance(value, _KINDS[self.kind]):
            raise ValueError(f'{self.label}: {value!r} is not of the kind {self.kind}')
        self._value = value
        self._notify()

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
        self.label = f'{self._label} ({zone})'
        return None if value is None else value.replace(tzinfo=None)


class ScalarsField(Field):
    """
    A list of scalars: a :class:`ScalarField` per element in ``elements``, with
    :meth:`add` and :meth:`remove` of the last. Unset, it has no
    element until one is added or removed: a removal gives no element, the
    empty list.
    """

    def __init__(
        self, label: str, tp: Any, default: Any, description: str | None = None
    ) -> None:
        super().__init__()
        self.label = label
        self._type = tp
        self._default = default
        self._description = description
        self._elements: list[ScalarField] | None = None  # unset
        self.set(UNSET)

    @property
    def elements(self) -> list[ScalarField]:
        return self._elements or []

    def add(self) -> None:
        self._resize(len(self.elements) + 1)
        self._notify()

    def remove(self) -> None:
        self._resize(len(self.elements) - 1)
        self._notify()

    def _resize(self, n: int) -> None:
        """
        Keep the first ``n`` elements, none for ``-1``, or add elements with
        nothing given, but a text, which is ``\'\'``.
        """
        elements = self._elements or []
        del elements[n:]
        default = '' if self._type is str else UNSET
        for i in range(len(elements), n):
            element = ScalarField(
                f'{self.label} [{i}]', self._type, default, self._description
            )
            element.subscribe(self._notify)
            elements.append(element)
        self._elements = elements

    def collect(self, loc: Loc, missing: Missing) -> Any:
        if self._elements is None:
            missing.append(FieldError(None, loc, 'add an element, or remove for none'))
            return UNSET
        return [e.collect((*loc, i), missing) for i, e in enumerate(self._elements)]

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        if value is UNSET:
            self._elements = None
        else:
            self._resize(len(value))
            for element, item in zip(self.elements, value, strict=True):
                element.set(item)
        self._notify()


class GroupField(Field):
    """The fields of a group, each by its type; the tag of an alternative has none."""

    def __init__(
        self, group: type[BaseModel], tag: dict[str, str], listing: Listing
    ) -> None:
        super().__init__()
        self.tag = tag
        self.fields = {
            name: _make_field(name, info, listing)
            for name, info in group.model_fields.items()
            if name not in tag
        }
        self.titles = {
            name: field_types.title(name, group.model_fields[name])
            for name in self.fields
        }
        for field in self.fields.values():
            field.subscribe(self._notify)

    def collect(self, loc: Loc, missing: Missing) -> dict[str, Any]:
        given = {n: f.collect((*loc, n), missing) for n, f in self.fields.items()}
        return self.tag | {n: v for n, v in given.items() if v is not UNSET}

    def set(self, value: Any) -> None:
        given = {} if value is UNSET else dict(value)
        for name, field in self.fields.items():
            field.set(given.get(name, UNSET))

    def refresh(self, listing: Listing) -> None:
        for field in self.fields.values():
            field.refresh(listing)


class RecordField(Input):
    """
    An output of a record that fits ``tp``, a group or a union of groups: a
    picker of references, with :data:`UNSET` for none, as at a file.
    """

    def __init__(self, tp: Any, listing: Listing) -> None:
        super().__init__()
        self._tp = tp
        self.refresh(listing)

    def refresh(self, listing: Listing) -> None:
        self.options = {'(choose a record)': UNSET, **listing.fitting(self._tp)}
        self.set(self.value)

    def collect(self, loc: Loc, missing: Missing) -> Any:
        if self.value is UNSET:
            missing.append(FieldError(None, loc, 'choose a record'))
            return UNSET
        return self.value

    def set(self, value: Any) -> None:
        self._value = value if value in self.options.values() else UNSET
        self._notify()


class NothingField(Field):
    """``None``: no value."""

    def collect(self, loc: Loc, missing: Missing) -> None:
        return None

    def set(self, value: Any) -> None:
        pass


class ChoiceField(Field):
    """
    The ways of giving a value, each a field: the alternatives of a union
    of groups, whose tag field is ``tag``; a group, a file, or a scalar
    given; an output of a record; ``None``. ``chosen`` is the label of the
    way, ``None`` for none. A selector of one way only is hidden.

    It starts at its default's way. Without a default, it starts unset,
    unless it has one way only. Set, each way not chosen is as it starts, so
    that it shows no value set before, such as a row shown before.
    """

    GIVEN = 'Given'
    RECORD = 'From a record'
    FILE = 'A file'
    NONE = 'None'

    def __init__(self, ways: dict[str, Field], tag: str | None, default: Any) -> None:
        super().__init__()
        self.ways = ways
        self._tag = tag
        self._default = default
        self._chosen: str | None = None
        for way in ways.values():
            way.subscribe(self._notify)
        self.set(UNSET)

    @property
    def chosen(self) -> str | None:
        return self._chosen

    @chosen.setter
    def chosen(self, label: str | None) -> None:
        if label != self._chosen:
            self._chosen = label
            self._notify()

    @property
    def offered(self) -> list[str]:
        return list(self.ways)

    @property
    def selector_visible(self) -> bool:
        return len(self.ways) > 1 or self._chosen is None

    def refresh(self, listing: Listing) -> None:
        for way in self.ways.values():
            way.refresh(listing)

    def collect(self, loc: Loc, missing: Missing) -> Any:
        chosen = self._chosen
        return UNSET if chosen is None else self.ways[chosen].collect(loc, missing)

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        label = self._label(value)
        self._chosen = label
        for way_label, way in self.ways.items():
            way.set(value if way_label == label else UNSET)
        self._notify()

    def _label(self, value: Any) -> str | None:
        """
        The label of the way that writes ``value``, or ``None`` if unset. Raises
        ``ValueError`` if no way writes it.
        """
        if value is UNSET:
            return next(iter(self.ways)) if len(self.ways) == 1 else None
        if (label := self._kind_of_way(value)) in self.ways:
            return label
        raise ValueError(f'{value!r}: not one of {self.offered}')

    def _kind_of_way(self, value: Any) -> str:
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
        for label, way in self.ways.items():
            if isinstance(way, GroupField) and way.tag == tag:
                return label
        raise ValueError(f'{tag[self._tag]}: not an alternative')


class RowsField(Field):
    """
    A list of ``element``, not a scalar: a table with a row per element, and
    a column per field of the element, of the tag and the fields of every
    alternative of a union, and of the file and the output that a row may
    be given as, such as ``File`` and ``Record`` for ``list[Run |
    Referenced[X]]``; and below it the editor of a row, the field of the
    element's type. A cell is empty where an alternative lacks the field,
    and shows a group or a list as its JSON text. The rows are held as a
    request writes them, so the fields do not grow with the rows.

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
        super().__init__()
        self._default = default
        self._listing = listing
        self._rows: list[Any] | None = None  # unset
        self.selection: list[int] = []
        self.message = ''
        self.can_update = False
        self.columns: dict[str, list[str]] = {}
        # What the editor held as it showed the selection, which differs from
        # the row if the editor cannot show it whole.
        self._shown: tuple[Any, Missing] = (UNSET, [])
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
        self.editor = _make('', element, (), UNSET, listing)
        self.editor.subscribe(self._editor_changed)
        self._loading = False
        self.set(UNSET)

    def _editor_changed(self) -> None:
        if not self._loading:
            self._notify()

    @contextmanager
    def _loads(self) -> Iterator[None]:
        """Load the editor without telling of each field on the way."""
        self._loading = True
        try:
            yield
        finally:
            self._loading = False

    def _selected(self) -> int | None:
        """The row selected, if one is."""
        return self.selection[0] if len(self.selection) == 1 else None

    def _held(self) -> tuple[Any, Missing]:
        """
        The editor's value and what is missing in it, which names any changes
        in the editor of a table within this editor.
        """
        missing: Missing = []
        return self.editor.collect((), missing), missing

    def select(self, selection: list[int]) -> None:
        """Show ``selection`` in the editor, or keep the last if the editor changed."""
        if selection == self.selection:
            return
        if self._held() != self._shown:
            self.message = _HOLDS_CHANGES
            self._notify()
            return
        self.selection = selection
        self._load()

    def discard(self) -> None:
        """Show the row selected in the editor, or nothing if none is."""
        self._load()

    def _load(self) -> None:
        i = self._selected()
        with self._loads():
            self.editor.set(UNSET if i is None else (self._rows or [])[i])
        self._shown = self._held()
        self.can_update = i is not None
        self.message = ''
        self._notify()

    def add_row(self) -> None:
        """Append the editor as a new row, if nothing is missing."""
        self._write(None)

    def update_row(self) -> None:
        """Write the editor to the row selected, if one is and nothing is missing."""
        if (i := self._selected()) is not None:
            self._write(i)

    def _write(self, i: int | None) -> None:
        value, missing = self._held()
        if value is UNSET and not missing:  # a choice with no way chosen
            missing.append(FieldError(None, (), 'choose a way'))
        self.message = '\n'.join(f'- {e}' for e in missing)
        if missing:
            self._notify()
            return
        self._shown = (value, missing)
        rows = self._rows or []
        if i is None:
            self._rows = [*rows, value]
            self._render()
            self.select([len(self._rows) - 1])
        else:
            rows[i] = value
            self._render()
        self._notify()

    def remove_rows(self) -> None:
        """Remove the rows selected, with any changes to them in the editor."""
        selected = set(self.selection)
        self._rows = [r for i, r in enumerate(self._rows or []) if i not in selected]
        if selected:
            self.selection = []
            self._load()
        self._render()
        self._notify()

    def _render(self) -> None:
        """The text of each column, a file or an output by its label."""
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
        self.columns = columns

    def collect(self, loc: Loc, missing: Missing) -> Any:
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
            with self._loads():
                for item in value:
                    self.editor.set(item)
                    given, missing = self._held()
                    self._rows.append(item if missing else given)
        self.selection = []
        self._load()
        self._render()
        self._notify()

    def refresh(self, listing: Listing) -> None:
        self._listing = listing
        changed = self._held() != self._shown
        with self._loads():
            self.editor.refresh(listing)
        if not changed:
            self._shown = self._held()
        self._render()
        self._notify()


class Fields:
    """
    The params of one pydantic model, such as a spec's ``params``: a field per
    parameter in ``fields``, listing what ``listing`` offers at each.

    ``fields`` limits them to these parameters. So a template's two parts
    are one form each, its values and its blanks::

        Fields(IOFQ.params, listing, fields=settings)
        Fields(IOFQ.params, listing, fields=('sample', 'sample_transmission'))

    Raises ``KeyError`` for a name that is not a parameter of the model.
    """

    def __init__(
        self,
        model: type[BaseModel],
        listing: Listing,
        *,
        fields: Collection[str] | None = None,
        title: str = '',
    ) -> None:
        self.title = title or model.__name__
        every = model.model_fields
        self._every = frozenset(every)
        if unknown := sorted(set(fields or ()) - self._every):
            raise KeyError(f'{self.title} has no parameters {unknown}')
        shown = {n: i for n, i in every.items() if fields is None or n in fields}
        self.fields = {
            name: _make_field(name, info, listing) for name, info in shown.items()
        }
        self.titles = {n: field_types.title(n, i) for n, i in shown.items()}
        self._on_change: Changed | None = None
        self._last: Any = None
        self._loading = False
        for field in self.fields.values():
            field.subscribe(self._changed)

    def watch(self, on_change: Changed) -> None:
        """
        Call ``on_change`` when a person changes what the fields hold or what
        is missing. Selecting a row does neither. :meth:`set` and :meth:`refresh`
        do not call it, whatever they change.
        """
        self._on_change = on_change
        self._last = self.held()

    def _changed(self) -> None:
        if self._on_change is None or self._loading:
            return
        if (held := self.held()) != self._last:
            self._last = held
            self._on_change()

    @contextmanager
    def _loads(self) -> Iterator[None]:
        self._loading = True
        try:
            yield
        finally:
            self._loading = False
            if self._on_change is not None:
                self._last = self.held()

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
        given, missing = self.held()
        if missing:
            raise SubmitError(missing)
        return given

    def held(self) -> tuple[dict[str, Any], list[FieldError]]:
        """
        What the fields hold: the params given in full, leaving out each with
        anything missing, and what is missing, as :attr:`params` names it.
        """
        missing: Missing = []
        given = {n: f.collect((n,), missing) for n, f in self.fields.items()}
        incomplete = {e.loc[0] for e in missing if e.loc}
        return {
            n: v for n, v in given.items() if v is not UNSET and n not in incomplete
        }, missing

    def set(self, params: Mapping[str, Any], listing: Listing) -> None:
        """
        Show ``params``, as a record or a request holds them, such as
        ``record.request.params``, with what ``listing`` offers; a field left
        out is as it starts, at its default or unset, and a parameter that is
        not shown is ignored. A reference that the client may not
        read now chooses the way of a record, with no record picked; a file
        that is not listed stays picked.
        """
        if unknown := set(params) - self._every:
            raise KeyError(f'{self.title} has no parameters {sorted(unknown)}')
        self.refresh(listing)
        with self._loads():
            for name, field in self.fields.items():
                field.set(params.get(name, UNSET))

    def ways(self, name: str) -> list[str]:
        """
        The ways offered at the parameter ``name``, a choice, such as
        ``['Given', 'From a record']``. Raises ``TypeError`` for a
        parameter of another type, such as a file or a list.
        """
        if not isinstance(field := self.fields[name], ChoiceField):
            raise TypeError(f'{name}: not a choice of ways')
        return field.offered

    def refresh(self, listing: Listing) -> None:
        """List the files and the records that fit again, from ``listing``."""
        with self._loads():
            for field in self.fields.values():
                field.refresh(listing)

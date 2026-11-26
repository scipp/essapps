# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The fields of a form for a pydantic model of params, without widgets.

:class:`ess.apps.panel.Form` shows them, and another widget library would show
the same fields. A widget layer reads a field's public attributes when the field
calls back (:meth:`Field.subscribe`). It passes on what a person does by setting
``value`` or by calling the field's methods.

The fields collect params as a request writes them, and are set from params as a
record holds them. So a record's request can be edited and submitted again. The
field of a value follows from its type, as the table in
``packages/essspec/docs/params.md`` shows. A field offers the outputs that the
client may reference now (``client.readable``), so a pending record is offered
too.

A field without a default starts unset, so that a value a correct reduction
needs is never filled in for the user. A list starts unset until an element is
added or removed, so that a table is never submitted without rows by mistake.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from enum import Enum
from numbers import Integral, Real
from typing import Any, Literal, Union, get_args

from pydantic import AwareDatetime, BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_json, to_jsonable_python

from ess.spec import File, FileRef, OutputRef, field_types
from ess.spec.field_types import Choice, FieldShape, FileKind, Group, List, Scalar
from ess.spec.refs import Loc, as_ref

from .errors import FieldError, SubmitError
from .listing import Listing


class _Unset:
    def __repr__(self) -> str:
        return 'UNSET'


UNSET: Any = _Unset()
"""
The value of a field that the request leaves out. Passed to ``set``, it resets
the field to its default, or to unset.
"""

type Missing = list[FieldError]
"""Each field with nothing given, with what the user must do."""

type Changed = Callable[[], None]
"""Called when a field changed."""

_KINDS = {'float': Real, 'int': Integral, 'text': str, 'time': datetime}
"""What an input of each kind holds, besides ``None``."""

_HOLDS_CHANGES = 'the editor holds changes: add or update a row, or discard them'


class Field:
    """
    A field of any type.

    ``collect(loc, missing)`` returns the value as a request writes it, and
    appends each field with nothing given to ``missing``. ``set`` shows a value.
    ``refresh`` lists the files and records again.
    """

    def __init__(self) -> None:
        self._subscribers: list[Changed] = []

    def subscribe(self, changed: Changed) -> None:
        """Calls ``changed`` whenever this field or one inside it changes."""
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
    """A field filled through one input: ``value``, one of ``options`` if any."""

    def __init__(self, label: str = '') -> None:
        super().__init__()
        self.label = label
        self.options = {}
        self._value: Any = UNSET

    @property
    def options(self) -> dict[str, Any]:
        """The options by label. Assign a new dict to change them."""
        return self._options

    @options.setter
    def options(self, options: dict[str, Any]) -> None:
        # Option values are hashable (references, enum members, plain scalars),
        # and ``==`` agrees with ``hash`` for them, so a lookup by value finds
        # what a search with ``==`` finds. The first of equal values wins.
        self._options = options
        self._labels: dict[Any, str] = {}
        for label, value in options.items():
            self._labels.setdefault(value, label)

    def label_of(self, value: Any) -> str | None:
        """The label of the first option equal to ``value``, ``None`` if none is."""
        try:
            return self._labels.get(value)
        except TypeError:  # unhashable, so equal to no option
            return None

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
        field_types.shape(info.annotation, field_types.field_metadata(info)),
        UNSET if info.is_required() else info.get_default(call_default_factory=True),
        listing,
        info.description,
    )


def _make(
    label: str,
    field: FieldShape,
    default: Any,
    listing: Listing,
    description: str | None = None,
) -> Field:
    """
    The field of a value of the shape ``field``, at any depth.

    Reads the shape as :func:`ess.spec.request_types.sources` reads the request
    type.
    """
    inline, referenced = field.inline, field.referenced
    match inline:
        case List(element) if field_types.is_scalar(item := field_types.plain(element)):
            return ScalarsField(label, item, default, description)
        case List(element):
            return RowsField(element, default, listing)
    if not (field.none or referenced):
        match inline:
            case FileKind(marker):
                return FileField(marker, listing, label)
            case Scalar(tp):
                return ScalarField(label, tp, default, description)
    alternatives: dict[str, Field] = {}
    match inline:
        case Choice(tag, groups):
            for value, group in groups.items():
                title = field_types.title(value, group.model_fields[tag])
                alternatives[title] = GroupField(group, {tag: value}, listing)
        case FileKind(marker):
            alternatives[ChoiceField.FILE] = FileField(marker, listing)
        case Group(group):
            alternatives[ChoiceField.GIVEN] = GroupField(group, {}, listing)
        case Scalar(tp):
            alternatives[ChoiceField.GIVEN] = ScalarField(label, tp, UNSET, description)
    if referenced:
        alternatives[ChoiceField.RECORD] = RecordField(Union[referenced], listing)  # noqa: UP007
    if field.none:
        alternatives[ChoiceField.NONE] = NothingField()
    tag = inline.tag if isinstance(inline, Choice) else None
    return ChoiceField(alternatives, tag, default)


class FileField(Input):
    """
    A picker of the files that :meth:`~ess.apps.Listing.files` lists for
    ``marker``.

    A file that the field is set to and the listing lacks stays picked. It is
    labelled by its reference as given, an identity or a name such as
    ``run:101``. The option of nothing has the value :data:`UNSET`.
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
    An input of an ``int``, ``float``, ``str`` or time, or a picker of the values
    of a ``bool``, an enum or a ``Literal``.

    ``collect`` returns the JSON form of the value. A picker has the option
    :data:`UNSET` for none, so that it may start unset. ``set`` raises
    ``ValueError`` for a value of another type. A value that a picker does not
    list leaves it unset.
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
        # A text input cannot tell an empty text from none, so ``''`` is a value
        # only where the field has a default.
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
        ``value``, a time or its text, as the picker shows it: without its zone,
        which the label names, and without its fraction of a second, which is
        kept until the user changes the time.
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
    A list of scalars, with a :class:`ScalarField` per element.

    :meth:`add` appends an element, and :meth:`remove` removes the last. Unset,
    the list has no elements until one is added or removed. A removal from an
    unset list makes it the empty list.
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
        Keeps the first ``n`` elements, or appends new ones up to ``n``. A new
        element has nothing given, except a text, which is ``''``.
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
        elif isinstance(value, str) or not isinstance(value, Sequence):
            raise ValueError(f'{self.label}: {value!r} is not a list')
        else:
            self._resize(len(value))
            for element, item in zip(self.elements, value, strict=True):
                element.set(item)
        self._notify()


class GroupField(Field):
    """The fields of a group, each by its type. The tag of an alternative has none."""

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
        if value is not UNSET and not isinstance(value, Mapping | BaseModel):
            raise ValueError(f'{value!r} is not a group of values')
        given = {} if value is UNSET else dict(value)
        for name, field in self.fields.items():
            field.set(given.get(name, UNSET))

    def refresh(self, listing: Listing) -> None:
        for field in self.fields.values():
            field.refresh(listing)


class RecordField(Input):
    """
    A picker of references to outputs that fit ``tp``, a group or a union of
    groups. The option of nothing has the value :data:`UNSET`.
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
    A field per alternative way to give a value, such as a group given inline,
    an output of a record, or ``None``.

    ``tag`` is the tag field of a union of groups. ``chosen`` is the label of the
    chosen alternative, or ``None``. The selector is hidden if there is only one
    alternative.

    The field starts at its default's alternative. Without a default, it starts
    unset, unless it has only one alternative. ``set`` resets each alternative
    not chosen, so that it shows no value set before, such as of a row shown
    before.
    """

    GIVEN = 'Given'
    RECORD = 'From a record'
    FILE = 'A file'
    NONE = 'None'

    def __init__(
        self, alternatives: dict[str, Field], tag: str | None, default: Any
    ) -> None:
        super().__init__()
        self.alternatives = alternatives
        self._tag = tag
        self._default = default
        self._chosen: str | None = None
        for alternative in alternatives.values():
            alternative.subscribe(self._notify)
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
        return list(self.alternatives)

    @property
    def selector_visible(self) -> bool:
        return len(self.alternatives) > 1 or self._chosen is None

    def refresh(self, listing: Listing) -> None:
        for alternative in self.alternatives.values():
            alternative.refresh(listing)

    def collect(self, loc: Loc, missing: Missing) -> Any:
        chosen = self._chosen
        return (
            UNSET if chosen is None else self.alternatives[chosen].collect(loc, missing)
        )

    def set(self, value: Any) -> None:
        if value is UNSET:
            value = self._default
        label = self._label(value)
        self._chosen = label
        for title, alternative in self.alternatives.items():
            alternative.set(value if title == label else UNSET)
        self._notify()

    def _label(self, value: Any) -> str | None:
        """
        The label of the alternative that writes ``value``, or ``None`` if unset.
        Raises ``ValueError`` if no alternative writes it.
        """
        if value is UNSET:
            return (
                next(iter(self.alternatives)) if len(self.alternatives) == 1 else None
            )
        if (label := self._alternative_label(value)) in self.alternatives:
            return label
        raise ValueError(f'{value!r}: not one of {self.offered}')

    def _alternative_label(self, value: Any) -> str:
        if value is None:
            return self.NONE
        ref = as_ref(value)
        if isinstance(ref, OutputRef):
            return self.RECORD
        if isinstance(ref, FileRef):
            return self.FILE
        if self._tag is None:
            return self.GIVEN
        given = dict(value) if isinstance(value, Mapping | BaseModel) else {}
        if self._tag not in given:
            raise ValueError(f'{value!r}: not one of {self.offered}')
        tag = {self._tag: given[self._tag]}
        for label, alternative in self.alternatives.items():
            if isinstance(alternative, GroupField) and alternative.tag == tag:
                return label
        raise ValueError(f'{tag[self._tag]}: not an alternative')


class RowsField(Field):
    """
    A list of anything except scalars: a table with a row per element, and an
    editor of one row.

    The table has a column per field of every alternative of the element, and a
    ``File`` or ``Record`` column for a row given by reference. The editor is
    the field of the element's type. The rows are held as a request writes
    them, so the number of fields does not grow with the rows.

    The editor shows the selected row, and is empty if no row or several are
    selected. :meth:`update_row` writes the editor back to the selected row, and
    :meth:`add_row` appends it, each only if nothing is missing in it.
    :meth:`remove_rows` removes the selected rows, with any changes to them in
    the editor. :meth:`discard` shows the selection in the editor again.

    While the editor differs from what it showed, the selection stays, and
    :attr:`Fields.params` reports the changes as missing. So a change in the
    editor is not lost unseen. A refresh is no change: a record gone from the
    listing leaves its row as it is.

    Unset, the field has no rows until one is added or :meth:`remove_rows` is
    called, which makes it the empty list. A row that the editor cannot show whole,
    such as a reference to an output that the client may not read now, stays
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
        row = field_types.shape(element)
        self._marker: File | None = None
        # The tag field of a union, and the title of each alternative by tag.
        self._tag: str | None = None
        self._titles: dict[str, str] = {}
        groups: list[type[BaseModel]] = []
        match row.inline:
            case FileKind(marker):
                self._marker = marker
            case Group(group):
                groups = [group]
            case Choice(tag, alternatives):
                self._tag = tag
                self._titles = {
                    value: field_types.title(value, group.model_fields[tag])
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
        if row.referenced:
            self._refs['Record'] = OutputRef
        self.editor = _make('', row, UNSET, listing)
        self.editor.subscribe(self._editor_changed)
        self._loading = False
        self.set(UNSET)

    def _editor_changed(self) -> None:
        if not self._loading:
            self._notify()

    @contextmanager
    def _loads(self) -> Iterator[None]:
        """Loads the editor without notifying the subscribers of each field."""
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
        The editor's value, and what is missing in it, including changes held
        in the editor of a table inside this editor.
        """
        missing: Missing = []
        return self.editor.collect((), missing), missing

    def select(self, selection: list[int]) -> None:
        """Shows ``selection`` in the editor, unless the editor holds changes."""
        if selection == self.selection:
            return
        if self._held() != self._shown:
            self.message = _HOLDS_CHANGES
            self._notify()
            return
        self.selection = selection
        self._load()

    def discard(self) -> None:
        """Shows the selected row in the editor, or nothing if none is selected."""
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
        """Appends the editor's value as a new row, if nothing is missing."""
        self._write(None)

    def update_row(self) -> None:
        """Writes the editor to the selected row, if one is and nothing is missing."""
        if (i := self._selected()) is not None:
            self._write(i)

    def _write(self, i: int | None) -> None:
        value, missing = self._held()
        if value is UNSET and not missing:  # a choice with no alternative chosen
            missing.append(FieldError(None, (), 'choose an alternative'))
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
        """Removes the selected rows, with any changes to them in the editor."""
        selected = set(self.selection)
        self._rows = [r for i, r in enumerate(self._rows or []) if i not in selected]
        if selected:
            self.selection = []
            self._load()
        self._render()
        self._notify()

    def _render(self) -> None:
        """Sets the text of each column, with a file or an output by its label."""
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
    A field per parameter of one pydantic model, such as a spec's ``params``.

    ``fields`` limits the form to these parameters, so that a template's values
    and its blanks are one form each::

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
        Calls ``on_change`` when a person changes what the fields hold or what is
        missing. Selecting a row changes neither. :meth:`set` and :meth:`refresh`
        never call it.
        """
        self._on_change = on_change
        self._last = self.params_and_missing()

    def _changed(self) -> None:
        if self._on_change is None or self._loading:
            return
        if (held := self.params_and_missing()) != self._last:
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
                self._last = self.params_and_missing()

    @property
    def params(self) -> dict[str, Any]:
        """
        The params as a request writes them.

        A field with no alternative chosen is left out. Raises
        :class:`~ess.apps.SubmitError` with a problem at each other field with
        nothing given::

            beam_centre: choose a record
            reduction.bins: give a value
            sample: add a row, or remove for none
            sample[1]: the editor holds changes: add or update a row, or discard them
        """
        given, missing = self.params_and_missing()
        if missing:
            raise SubmitError(missing)
        return given

    def params_and_missing(self) -> tuple[dict[str, Any], list[FieldError]]:
        """
        The params given in full, and what is missing, as :attr:`params` reports
        it. A parameter with anything missing is left out of the params.
        """
        missing: Missing = []
        given = {n: f.collect((n,), missing) for n, f in self.fields.items()}
        incomplete = {e.loc[0] for e in missing if e.loc}
        return {
            n: v for n, v in given.items() if v is not UNSET and n not in incomplete
        }, missing

    def set(self, params: Mapping[str, Any], listing: Listing) -> None:
        """
        Shows ``params`` as a record or a request holds them, such as
        ``record.request.params``, with what ``listing`` offers.

        A field left out of ``params`` is reset to its default, or to unset. A
        parameter that the form does not show is ignored. A reference that the
        client may not read now chooses the alternative of a record, with no
        record picked. A file that is not listed stays picked.

        Raises ``KeyError`` for a name that is not a parameter, and
        ``ValueError`` for a value that a field cannot show.
        """
        if unknown := set(params) - self._every:
            raise KeyError(f'{self.title} has no parameters {sorted(unknown)}')
        self.refresh(listing)
        with self._loads():
            for name, field in self.fields.items():
                field.set(params.get(name, UNSET))

    def set_fitting(self, params: Mapping[str, Any], listing: Listing) -> list[str]:
        """
        Shows each parameter of ``params`` that fits, as :meth:`set` does, and
        resets the others.

        Returns a line for each parameter that does not fit, such as one stored
        for another version of the model, with its name, its value, and why.
        """
        unfit = {
            name: 'not a setting here' for name in params if name not in self.fields
        }
        self.refresh(listing)
        with self._loads():
            for name, field in self.fields.items():
                try:
                    field.set(params.get(name, UNSET))
                except Exception as error:  # noqa: PERF203 - any value that does not fit
                    unfit[name] = str(error)
                    field.set(UNSET)
        return [
            f"{n} = {to_json(params[n], fallback=repr).decode()}: {why}"
            for n, why in unfit.items()
        ]

    def alternatives(self, name: str) -> list[str]:
        """
        The alternatives offered at the choice ``name``, such as
        ``['Given', 'From a record']``.

        Raises ``TypeError`` if the parameter is not a choice, such as a file or
        a list.
        """
        if not isinstance(field := self.fields[name], ChoiceField):
            raise TypeError(f'{name}: not a choice of alternatives')
        return field.offered

    def refresh(self, listing: Listing) -> None:
        """Lists the files and the records that fit again, from ``listing``."""
        with self._loads():
            for field in self.fields.values():
                field.refresh(listing)

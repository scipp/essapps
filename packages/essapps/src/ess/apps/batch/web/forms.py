# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The fields of :mod:`ess.apps.fields` as JSON for a page, and the edits a page
sends back.

A form is sent as a list of nodes, one per parameter, each with its children.
Every node has a ``kind``, a ``label``, a ``path`` that an edit names, and a
``problem``: what is missing there, or why a submission was refused there::

    {'kind': 'group', 'label': 'Wavelength', 'path': ['wavelength'], 'children': [
        {'kind': 'input', 'input': 'float', 'label': 'Start (Å)', 'value': 2.0,
         'path': ['wavelength', 'start'], 'problem': None, ...}]}

The kinds are those of the fields: ``input`` (``input`` is ``float``, ``int``,
``text``, ``time``, or ``pick``, with ``options`` by label), ``scalars`` (a
list of inputs), ``group``, ``choice`` (the ``ways``, the way ``chosen``, and
its node as ``child``), ``nothing``, and ``rows``, which is sent as its
columns of text, to show but not to edit. A page edits a form by sending
``{path, op, value}`` (see :func:`apply`), and gets the form again.

A picker's value travels as the label of its option, so a page never holds a
file reference: the server maps a label back to its value. A path has the name
of a field of a group, the label of a way of a choice, or the index of an
element of a list, while a ``loc`` of a :class:`~ess.apps.FieldError` has no
way, so the walk keeps both.

A cell of a batch's table is edited with the field of its column
(:func:`editor`, :func:`read`), so a cell takes what the form would.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ess.spec.refs import Loc

from ...errors import FieldError
from ...fields import (
    UNSET,
    ChoiceField,
    Field,
    Fields,
    GroupField,
    Input,
    NothingField,
    RowsField,
    ScalarField,
    ScalarsField,
)


class Edit(BaseModel):
    """
    What a person did to a form: ``set`` an input to ``value``, a label of an
    option for a picker; ``choose`` the way labelled ``value``; ``add`` or
    ``remove`` the last element of a list.
    """

    path: list[str | int]
    op: Literal['set', 'choose', 'add', 'remove']
    value: Any = None


type Node = dict[str, Any]


def tree(fields: Fields, refused: Iterable[FieldError] = ()) -> list[Node]:
    """The nodes of ``fields``, each with what is missing or ``refused`` there."""
    _, missing = fields.held()
    problems = {e.loc: e.msg for e in (*missing, *refused) if e.loc}
    return [
        _node(field, [name], (name,), fields.titles[name], problems)
        for name, field in fields.fields.items()
    ]


def _node(
    field: Field,
    path: list[str | int],
    loc: Loc,
    label: str,
    problems: Mapping[Loc, str],
) -> Node:
    node: Node = {'path': path, 'label': label, 'problem': problems.get(loc)}
    if isinstance(field, Input):
        return node | _input(field)
    if isinstance(field, ScalarsField):
        elements = [
            _node(e, [*path, i], (*loc, i), str(i + 1), problems)
            for i, e in enumerate(field.elements)
        ]
        return node | {'kind': 'scalars', 'elements': elements}
    if isinstance(field, GroupField):
        children = [
            _node(f, [*path, n], (*loc, n), field.titles[n], problems)
            for n, f in field.fields.items()
        ]
        return node | {'kind': 'group', 'children': children}
    if isinstance(field, ChoiceField):
        chosen = field.chosen
        child = (
            None
            if chosen is None
            else _node(field.ways[chosen], [*path, chosen], loc, label, problems)
        )
        return node | {
            'kind': 'choice',
            'ways': [{'value': w, 'label': _WAYS.get(w, w)} for w in field.offered],
            'chosen': chosen,
            'selector': field.selector_visible,
            'child': child,
        }
    if isinstance(field, NothingField):
        return node | {'kind': 'nothing'}
    if isinstance(field, RowsField):
        return node | {'kind': 'rows', 'columns': field.columns}
    raise TypeError(f'no node for {type(field).__name__}')


_WAYS = {
    ChoiceField.NONE: 'Not used',
    ChoiceField.FILE: 'Use a run',
    ChoiceField.RECORD: 'From a result',
}
"""The ways of a choice as a page labels them; ``None`` reads as no such step."""


def _input(field: Input) -> Node:
    kind = field.kind if isinstance(field, ScalarField) else 'pick'
    if kind == 'pick':
        value: Any = label_of(field)
    elif kind == 'time':
        value = None if field.value is None else field.value.isoformat()
    else:
        value = field.value
    return {
        'kind': 'input',
        'input': kind,
        'value': value,
        'options': [k for k, v in field.options.items() if v is not UNSET],
        'description': getattr(field, 'description', None),
    }


def label_of(field: Input) -> str | None:
    """The label of the option a picker holds, ``None`` if it holds none."""
    if field.value is UNSET:
        return None
    return next((k for k, v in field.options.items() if v == field.value), None)


def _at(fields: Fields, path: list[str | int]) -> Field:
    field = fields.fields[str(path[0])]
    for step in path[1:]:
        if isinstance(field, GroupField):
            field = field.fields[str(step)]
        elif isinstance(field, ChoiceField):
            field = field.ways[str(step)]
        elif isinstance(field, ScalarsField):
            field = field.elements[int(step)]
        else:
            raise KeyError(f'{path}: no field at {step!r}')
    return field


def apply(fields: Fields, edit: Edit) -> None:
    """
    Give ``fields`` what a person did. Raises ``KeyError`` for a path or a
    label that the form lacks, and ``ValueError`` for a value an input refuses.
    """
    field = _at(fields, edit.path)
    if edit.op == 'set' and isinstance(field, Input):
        field.value = _value(field, edit.value)
    elif edit.op == 'choose' and isinstance(field, ChoiceField):
        if edit.value not in (*field.offered, None):
            raise KeyError(f'{edit.value!r}: not one of {field.offered}')
        field.chosen = edit.value
    elif edit.op in ('add', 'remove') and isinstance(field, ScalarsField):
        getattr(field, edit.op)()
    else:
        raise KeyError(f'{edit.path}: cannot {edit.op} a {type(field).__name__}')


def _value(field: Input, sent: Any) -> Any:
    """What an input holds for ``sent``, as the page sends it."""
    kind = field.kind if isinstance(field, ScalarField) else 'pick'
    if kind == 'pick':
        if sent is None:
            return UNSET
        return field.options[sent]
    if kind == 'time' and sent:
        return datetime.fromisoformat(sent)
    if kind == 'float' and isinstance(sent, int):
        return float(sent)
    if kind in ('float', 'int') and not isinstance(sent, int | float | None):
        raise ValueError(f'{field.label}: {sent!r} is not a number')
    return sent


# Cells


def _inner(field: Field) -> tuple[Field, bool]:
    """
    The field that a cell edits, and whether the cell may be ``None``: a
    choice of one way that gives a value, and of none, is edited as that way.
    """
    if isinstance(field, ChoiceField):
        given = [w for w in field.offered if w != ChoiceField.NONE]
        if len(given) == 1:
            return field.ways[given[0]], ChoiceField.NONE in field.ways
    return field, False


def editor(field: Field) -> Node | None:
    """
    How a cell of the column of ``field`` is edited, or ``None`` if not in a
    cell: ``{'type': 'select', 'options': [...]}``, ``{'type': 'number'}``, or
    ``{'type': 'text'}``.
    """
    inner, _ = _inner(field)
    if not isinstance(inner, Input):
        return None
    node = _input(inner)
    if node['input'] == 'pick':
        return {'type': 'select', 'options': node['options']}
    if node['input'] in ('float', 'int'):
        return {'type': 'number', 'step': 1 if node['input'] == 'int' else 'any'}
    if node['input'] == 'text':
        return {'type': 'text'}
    return None


def cell(field: Field, value: Any) -> Any:
    """
    What the editor of a cell holding ``value`` starts at: the label of its
    option at a picker, the value at an input, ``None`` if empty.
    """
    inner, _ = _inner(field)
    if value is UNSET or value is None or not isinstance(inner, Input):
        return None
    if not inner.options:
        return value
    return next((k for k, v in inner.options.items() if v == value), None)


def read(field: Field, sent: Any) -> Any:
    """
    The value of a cell edited to ``sent``, as a request writes it; ``UNSET``
    for an empty cell, or ``None`` where the column takes ``None``. Raises
    ``ValueError`` for a value the field refuses, and ``KeyError`` for an
    option it lacks.
    """
    inner, nullable = _inner(field)
    if sent in (None, ''):
        return None if nullable else UNSET
    if not isinstance(inner, Input):
        raise ValueError('this column is not edited in a cell')
    if isinstance(inner, ScalarField):
        inner.set(_value(inner, sent))
        missing: list[FieldError] = []
        value = inner.collect((), missing)
        if missing:
            raise ValueError(missing[0].msg)
        return value
    return _value(inner, sent)


def jsonable(value: Any) -> Any:
    """``value`` in JSON form, as a record holds it."""
    return to_jsonable_python(value, by_alias=True)

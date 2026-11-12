# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The fields of :mod:`ess.apps.fields` as JSON for a page, and the edits a page
sends back.

A form is sent as a list of :class:`~.models.Node`, one per parameter, each
with its children and a ``problem``: what is missing there, or why a
submission was refused there::

    Node(kind='group', label='Wavelength', path=['wavelength'], children=[
        Node(kind='input', input='float', label='From (Å)', value=2.0,
             path=['wavelength', 'start'])])

A path is the ``loc`` of an error at that field: names of fields and indices of
elements, but no way of a choice. So the walk of a path goes into the way
chosen by itself, and a choice and the field of its way have the same path. A
page edits a form by sending an :class:`~.models.Edit` (see :func:`apply`), and
gets the form again.

A picker's value travels as the label of its option, so a page never holds a
file reference: the server maps a label back to its value.

A cell of a batch's table is edited with the field of its column
(:func:`editor`, :func:`read`), so a cell takes what the form would.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

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
from .models import Edit, Editor, Node, Way


def tree(fields: Fields, refused: Iterable[FieldError] = ()) -> list[Node]:
    """The nodes of ``fields``, each with what is missing or ``refused`` there."""
    _, missing = fields.held()
    said = {e.loc: message(e.msg) for e in (*missing, *refused) if e.loc}
    problems = {loc: m[:1].upper() + m[1:] for loc, m in said.items()}
    return [
        _node(field, (name,), fields.titles[name], problems)
        for name, field in fields.fields.items()
    ]


def _node(field: Field, loc: Loc, label: str, problems: Mapping[Loc, str]) -> Node:
    node = Node(kind='nothing', path=list(loc), label=label, problem=problems.get(loc))
    if isinstance(field, Input):
        return node.model_copy(update=_input(field))
    if isinstance(field, ScalarsField):
        elements = [
            _node(e, (*loc, i), str(i + 1), problems)
            for i, e in enumerate(field.elements)
        ]
        return node.model_copy(update={'kind': 'scalars', 'elements': elements})
    if isinstance(field, GroupField):
        children = [
            _node(f, (*loc, n), field.titles[n], problems)
            for n, f in field.fields.items()
        ]
        return node.model_copy(update={'kind': 'group', 'children': children})
    if isinstance(field, ChoiceField):
        chosen = field.chosen
        child = (
            None if chosen is None else _node(field.ways[chosen], loc, label, problems)
        )
        return node.model_copy(
            update={
                'kind': 'choice',
                'ways': [Way(value=w, label=_WAYS.get(w, w)) for w in field.offered],
                'chosen': chosen,
                'selector': field.selector_visible,
                'child': child,
                # The field of the way chosen shows a problem at this path.
                'problem': node.problem if child is None else None,
            }
        )
    if isinstance(field, NothingField):
        return node
    if isinstance(field, RowsField):
        return node.model_copy(update={'kind': 'rows', 'columns': field.columns})
    raise TypeError(f'no node for {type(field).__name__}')


_WAYS = {
    ChoiceField.NONE: 'Not used',
    ChoiceField.FILE: 'Use a run',
    ChoiceField.RECORD: 'From a result',
}
"""The ways of a choice as a page labels them; ``None`` reads as no such step."""

_MESSAGES = {'choose a file': 'choose a run', 'choose a record': 'choose a result'}


def message(text: str) -> str:
    """A message of a field in the words of :data:`_WAYS` and of a person."""
    text = _MESSAGES.get(text, text)
    return re.sub(r"'(.*)' is not of the kind (float|int)", r'\1 is not a number', text)


def _input(field: Input) -> dict[str, Any]:
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


def _chosen(field: Field) -> Field:
    """The field of the way chosen, through choices of choices."""
    while isinstance(field, ChoiceField):
        if field.chosen is None:
            raise KeyError('nothing is chosen there')
        field = field.ways[field.chosen]
    return field


def _at(fields: Fields, path: list[str | int], *, choice: bool = False) -> Field:
    """
    The field at ``path``: through the way chosen of each choice, but the choice
    itself at the end of the path if ``choice``.
    """
    if not path or str(path[0]) not in fields.fields:
        raise KeyError(f'no setting at {path}')
    field = fields.fields[str(path[0])]
    for step in path[1:]:
        field = _chosen(field)
        if isinstance(field, GroupField) and str(step) in field.fields:
            field = field.fields[str(step)]
        elif isinstance(field, ScalarsField) and 0 <= int(step) < len(field.elements):
            field = field.elements[int(step)]
        else:
            raise KeyError(f'no setting at {path}')
    return field if choice and isinstance(field, ChoiceField) else _chosen(field)


def apply(fields: Fields, edit: Edit) -> None:
    """
    Give ``fields`` what a person did. Raises ``KeyError`` for a path or a
    label that the form lacks, and ``ValueError`` for a value an input refuses.
    """
    field = _at(fields, edit.path, choice=edit.op == 'choose')
    if edit.op == 'set' and isinstance(field, Input):
        field.value = _value(field, edit.value)
    elif edit.op == 'choose' and isinstance(field, ChoiceField):
        if edit.value not in (*field.offered, None):
            raise KeyError(f'{edit.value} is not one of {", ".join(field.offered)}')
        field.chosen = edit.value
    elif edit.op in ('add', 'remove') and isinstance(field, ScalarsField):
        getattr(field, edit.op)()
    else:
        raise KeyError(f'cannot {edit.op} the setting at {edit.path}')


def _value(field: Input, sent: Any) -> Any:
    """What an input holds for ``sent``, as the page sends it."""
    kind = field.kind if isinstance(field, ScalarField) else 'pick'
    if kind == 'pick':
        if sent is None:
            return UNSET
        if sent not in field.options:
            raise KeyError(f'{sent} is not one of the options')
        return field.options[sent]
    if kind == 'time' and sent:
        return datetime.fromisoformat(sent)
    if kind == 'float' and isinstance(sent, int):
        return float(sent)
    if kind in ('float', 'int') and not isinstance(sent, int | float | None):
        raise ValueError(f'{sent} is not a number')
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


def editor(field: Field) -> Editor | None:
    """How a cell of the column of ``field`` is edited, ``None`` if not in a cell."""
    inner, _ = _inner(field)
    if not isinstance(inner, Input):
        return None
    node = _input(inner)
    if node['input'] == 'pick':
        return Editor(type='select', options=node['options'])
    if node['input'] in ('float', 'int'):
        return Editor(type='number')
    if node['input'] == 'text':
        return Editor(type='text')
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The fields of :mod:`ess.apps.fields` as the nodes of a page, and the edits that
a page sends back.

A form is a list of :class:`~.models.Node`, one per parameter, each with its
children and a ``problem``: what is missing there, or why a submission was
rejected there::

    Node(kind='group', label='Wavelength', path=['wavelength'], children=[
        Node(kind='input', input='float', label='From (Å)', value=2.0,
             path=['wavelength', 'start'])])

A path does not name the alternative of a choice, since an error's ``loc``
does not. So a walk along a path goes through the chosen alternative of each
choice. A page edits a form with an :class:`~.models.Edit` (:func:`apply`) and
receives the form again.

A picker's value travels as the label of its option, so a page never holds a
file reference. A cell of a batch table is edited with the field of its column
(:func:`editor`, :func:`read`), so a cell accepts what the form would.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any

from ess.apps.errors import FieldError
from ess.apps.fields import (
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
from ess.spec.refs import Loc

from .models import Alternative, Edit, Editor, Node


def params_and_missing(
    fields: Fields, parameter_columns: Collection[str] = ()
) -> tuple[dict[str, Any], list[FieldError]]:
    """
    :meth:`~ess.apps.Fields.params_and_missing` of a form of settings, except
    that a field of ``parameter_columns`` left unset as a whole is not missing.
    """
    given, missing = fields.params_and_missing()
    return given, [
        e for e in missing if not (len(e.loc) == 1 and e.loc[0] in parameter_columns)
    ]


def tree(
    fields: Fields,
    refused: Iterable[FieldError] = (),
    parameter_columns: Collection[str] = (),
) -> list[Node]:
    """
    The nodes of ``fields``, each with what is missing or ``refused`` at it,
    and those of ``parameter_columns`` marked as columns.
    """
    _, missing = params_and_missing(fields, parameter_columns)
    said = {e.loc: message(e.msg) for e in (*missing, *refused) if e.loc}
    problems = {loc: m[:1].upper() + m[1:] for loc, m in said.items()}
    return [
        _node(field, (name,), fields.titles[name], problems).model_copy(
            update={'column': name in parameter_columns}
        )
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
            None
            if chosen is None
            else _node(field.alternatives[chosen], loc, label, problems)
        )
        return node.model_copy(
            update={
                'kind': 'choice',
                'alternatives': [
                    Alternative(value=w, label=_ALTERNATIVES.get(w, w))
                    for w in field.offered
                ],
                'chosen': chosen,
                'selector': field.selector_visible,
                'child': child,
                # The field of the chosen alternative shows a problem at this path.
                'problem': node.problem if child is None else None,
            }
        )
    if isinstance(field, NothingField):
        return node
    if isinstance(field, RowsField):
        return node.model_copy(update={'kind': 'rows', 'columns': field.columns})
    raise TypeError(f'no node for {type(field).__name__}')


_ALTERNATIVES = {
    ChoiceField.NONE: 'Not used',
    ChoiceField.FILE: 'Use a run',
    ChoiceField.RECORD: 'From a result',
}
"""The labels of the alternatives of a choice."""

_MESSAGES = {'choose a file': 'choose a run', 'choose a record': 'choose a result'}


def message(text: str) -> str:
    """A message of a field, in the words of the page."""
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
    return field.label_of(field.value)


def _chosen(field: Field) -> Field:
    """The field of the alternative chosen, through choices of choices."""
    while isinstance(field, ChoiceField):
        if field.chosen is None:
            raise KeyError('nothing is chosen there')
        field = field.alternatives[field.chosen]
    return field


def _at(fields: Fields, path: list[str | int], *, choice: bool = False) -> Field:
    """
    The field at ``path``, through the chosen alternative of each choice; with
    ``choice``, a choice at the end of ``path`` itself.
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
    Applies ``edit`` to ``fields``. Raises ``KeyError`` for a path or a label
    that the form lacks, and ``ValueError`` for a value that an input rejects.
    """
    field = _at(fields, edit.path, choice=edit.op in ('choose', 'reset'))
    if edit.op == 'set' and isinstance(field, Input):
        field.value = _value(field, edit.value)
    elif edit.op == 'choose' and isinstance(field, ChoiceField):
        if edit.value not in (*field.offered, None):
            raise KeyError(f'{edit.value} is not one of {", ".join(field.offered)}')
        field.chosen = edit.value
    elif edit.op in ('add', 'remove') and isinstance(field, ScalarsField):
        getattr(field, edit.op)()
    elif edit.op == 'reset' and len(edit.path) == 1:
        fields.fields[str(edit.path[0])].set(UNSET)
    else:
        raise KeyError(f'cannot {edit.op} the setting at {edit.path}')


def _value(field: Input, sent: Any) -> Any:
    """The value that ``field`` holds for ``sent`` from the page."""
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
    The field that a cell edits, and whether the cell may be ``None``. A choice
    of one alternative besides ``NONE`` is edited as that alternative.
    """
    if isinstance(field, ChoiceField):
        given = [w for w in field.offered if w != ChoiceField.NONE]
        if len(given) == 1:
            return field.alternatives[given[0]], ChoiceField.NONE in field.alternatives
    return field, False


def editor(field: Field) -> Editor | None:
    """The editor of a cell of ``field``, ``None`` if no cell edits it."""
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


def edited(fields: Fields) -> list[str]:
    """The names of the fields that a cell edits."""
    return [n for n, f in fields.fields.items() if editor(f) is not None]


def cell(field: Field, value: Any) -> Any:
    """
    Where the editor of a cell holding ``value`` starts: the label of the
    option for a picker, ``None`` if empty.
    """
    inner, _ = _inner(field)
    if value is UNSET or value is None or not isinstance(inner, Input):
        return None
    if not inner.options:
        return value
    return inner.label_of(value)


def read(field: Field, sent: Any) -> Any:
    """
    The value of a cell edited to ``sent``: ``UNSET`` if empty, or ``None``
    where the column accepts ``None``. Raises ``ValueError`` for a value that
    the field rejects, and ``KeyError`` for an option that it lacks.
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

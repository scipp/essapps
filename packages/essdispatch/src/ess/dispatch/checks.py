# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Request-phase checks: what the backend checks of a request, a template, a
push, and the state of an accumulator before any data is read.

A request lives in two phases. In the *request phase*, when it is submitted,
the backend checks what it can without data, here. In the *call phase*, on a
worker, the backend reads the data each reference names and validates them
(see ``backend.py``). :class:`Checks` holds the checks of one spec, one
method for each thing checked::

    checks = Checks(IOFQ, datasets)
    request = checks.request(request, proposal)            # a plain request
    template, fixed = checks.template(template, proposal)  # a stage or an accumulator
    index, columns = checks.push(blanks, columns, proposal)
    refused = checks.state(plain, blanks)                  # at a pin or a read

The checks of a request; a template, a push, and a state check part of these:

- *Names.* Each name is a parameter of the spec. A required parameter must
  be given; one left out takes its default.
- *Forms.* Each value, and each row of a list parameter, is a reference or
  an inline value, or, for a group that holds a file, a mapping of its
  fields, each a reference or an inline value; a list parameter is given as
  a list of rows. A reference stands for a value, a row, or a field of a
  group given field by field, so a reference to a whole list, or one
  anywhere else inside a value, is refused (see :mod:`ess.spec.refs`). A
  reference to a dataset where a group that holds a file is expected stands
  for the group with its one required file, and its other fields at their
  defaults; the record holds the group field by field.
- *Inline values.* An inline value is allowed where the parameter's type has
  a JSON form (see :meth:`ess.spec.WorkflowSpec.takes_inline`), and ``None``
  where it is optional; so a file given as a string is refused: a file is
  given as ``dataset(path=...)``. Each inline value is validated alone, by a
  model of its field, or for a row of its element type, from its JSON form,
  and a field that a model lacks is refused at any depth, since validating
  would drop it silently. The log holds the JSON form of the validated value,
  and the call phase validates that, so it must read back as the value: a
  number that is not finite has none (see
  :func:`ess.spec.field_types.inline`). A list parameter that is not a column
  of a table meets its length constraints.
- *References.* A file is given by reference to a dataset, and only a file
  is (see :func:`ess.spec.field_types.is_file`): the dataset source checks
  that the proposal may read it, and the record holds its identity, to
  which its name is resolved, so it must be known. A reference to a record
  or an accumulator is checked by the backend, which knows what the client
  may read (see :func:`check_fit`).
- *Table rules.* The columns of a table are given with its index, as many
  rows as it; a column left out is ``None`` in each row if its element type
  is optional, and a column whose element type is not optional is given
  whenever the table has rows. The number of rows meets the length
  constraints of the table's columns, so a table with a required index has a
  row, and the index values of a table are distinct, compared as the record
  holds them: a dataset by its identity, a reference to an output by record,
  output, and key, and an inline value by its JSON form (see
  :func:`distinct`).

The params model's model validators never run: a stage and a driver never
get a whole params instance. A template's fixed values are checked each by
its own field, and a push by its columns' element types. The checks need
neither the backend's lock nor a client: what depends on history or on what a
client keeps is the backend's.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError, to_json

from ess.spec import AccumulatorRef, DatasetRef, OutputRef, Ref, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.field_types import (
    base,
    by_field,
    element,
    file_field,
    fits,
    has_json_form,
    inline,
    is_file,
    is_list,
    json_form,
    optional,
    type_name,
    value_type,
)
from ess.spec.refs import as_ref, places

from .datasets import DatasetSource, readable
from .records import Request, SpecId, SubmitError, Template, read_refs

type Spec = WorkflowSpec[Any, Any]

_LEFT_OUT = object()
"""A field of a group that a request leaves out."""


class Checks:
    """
    Request-phase checks of one spec: of a plain request, a template, a push,
    and the state of an accumulator.
    """

    def __init__(self, spec: Spec, datasets: DatasetSource) -> None:
        self.spec = spec
        self.id = SpecId.of(spec)
        self._datasets = datasets

    def request(self, request: Request, proposal: str) -> Request:
        """
        The request as the record holds it: names resolved, defaults and
        columns left out filled, inline values in JSON's types.
        """
        spec = self.spec
        values = _columns(spec, request.params, spec.tables)
        params = self._values(_fixed(spec, values, ()), proposal)
        _rows(spec, params, spec.tables)
        return Request(self.id, params=params)

    def template(
        self, template: Template, proposal: str
    ) -> tuple[Template, dict[str, Any]]:
        """
        The template as given, with dataset names resolved and the columns
        given for a blank table dropped, as the stage or the accumulator
        holds it, and its fixed values by name, defaults filled in.

        A blank is a parameter, or a table, named by its index; a template
        fixes a whole table or leaves it blank. A parameter in neither the
        values nor the blanks takes its default, so it must have one. Each
        fixed value is checked by its own field, and the table rules hold on
        the fixed tables.
        """
        spec, blanks = self.spec, template.blanks
        left_open = spec.blank_fields(blanks)
        given = {n: v for n, v in template.params.items() if n not in left_open}
        tables = [index for index in spec.tables if index not in blanks]
        filled = _columns(spec, given, tables)
        fixed = self._values(_fixed(spec, filled, blanks), proposal)
        _rows(spec, fixed, tables)
        stored = Template(self.id, params={n: fixed[n] for n in given}, blanks=blanks)
        return stored, fixed

    def push(
        self, blanks: Collection[str], columns: Mapping[str, Any], proposal: str
    ) -> tuple[str, dict[str, list[Any]]]:
        """
        The index of the table a push names, and its columns by name, as the
        log holds them.

        A push gives the columns of one of the ``blanks``, a table: its
        index, and the columns attached to it, as lists of equal length, one
        or more rows; a column left out is filled as a request's is. Each
        inline value is checked by its column's element type, and each
        reference as a request's is. Whether an index value repeats one the
        table holds is the backend's to check, which knows the table (see
        :func:`distinct`).
        """
        spec = self.spec
        tables = {n: spec.table_of(n) for n in columns}
        if unknown := sorted(n for n, index in tables.items() if index is None):
            raise SubmitError(f'{unknown}: not columns of a table of {self.id}')
        indexes = {index for index in tables.values() if index is not None}
        if len(indexes) != 1:
            raise SubmitError(
                f'a push gives the columns of one table, not of {sorted(indexes)}'
            )
        (index,) = indexes
        if index not in columns:
            raise SubmitError(f'{sorted(columns)}: columns pushed without {index}')
        if index not in blanks:
            raise SubmitError(
                f'a push names a table of the accumulator by its index, '
                f'{list(blanks)}, not {index!r}'
            )
        if not columns[index]:
            raise SubmitError(f'a push into {index} has no rows')
        filled = _columns(spec, read_refs(columns), [index])
        return index, self._values(filled, proposal)

    def state(self, plain: Request, blanks: Collection[str]) -> str | None:
        """
        Why the plain request of an accumulator's state would be refused, or
        ``None``.

        Each value was checked by its field when it was given, and the params
        model's model validators never run, so this checks the rest: the
        number of rows of each table among the ``blanks``, by the length
        constraints of its columns.
        """
        try:
            _lengths(self.spec, plain.params, blanks)
        except SubmitError as error:
            return str(error)
        return None

    def _values(self, values: Mapping[str, Any], proposal: str) -> dict[str, Any]:
        """
        ``values`` by name as the record holds them: each inline value
        validated by its field, a row by its element type, and each reference
        with dataset names resolved.
        """
        return {
            name: self._value(name, value, proposal) for name, value in values.items()
        }

    def _value(self, name: str, value: Any, proposal: str) -> Any:
        """
        The value of parameter ``name`` as the record holds it: an inline
        value or a reference, or for a list parameter a list of them, one per
        row.
        """
        if not is_list(self.spec.params, name):
            group = by_field(value_type(self.spec.params.model_fields[name].annotation))
            for where, field, item in places(name, value):
                at_field = field is not None and where == f'{name}.{field}'
                if where != name and not (group and at_field) and as_ref(item):
                    raise SubmitError(
                        f'{where}: {as_ref(item)} is a reference inside a value; a '
                        'reference stands for a whole value, a row of a list, or '
                        'a field of a group'
                    )
            return self._one(name, name, value, proposal)
        if isinstance(value, Ref):
            raise SubmitError(
                f'{name}: {value} is a reference to a whole list; a reference '
                'stands for one row of a list parameter'
            )
        if not isinstance(value, list):
            raise SubmitError(f'{name}: {value!r} is not a list')
        if self.spec.table_of(name) is None and (
            need := _length_problem(self.spec, name, len(value))
        ):
            raise SubmitError(f'{name}: {_rows_text(len(value))}; it needs {need}')
        return [
            self._one(f'{name}[{i}]', name, cell, proposal)
            for i, cell in enumerate(value)
        ]

    def _one(self, where: str, name: str, value: Any, proposal: str) -> Any:
        """
        The value, or the row, ``value`` at ``where`` of parameter ``name`` as
        the record holds it: a reference, a dataset's resolved, an inline
        value if the parameter takes one, a group field by field, or ``None``
        if its type is optional.
        """
        spec = self.spec
        tp = value_type(spec.params.model_fields[name].annotation)
        if by_field(tp):
            group = base(tp)
            if isinstance(value, DatasetRef) and (file := file_field(group)):
                value = {file: value}
            elif isinstance(value, Ref):
                raise SubmitError(
                    f'{where}: {value} names the whole of {type_name(tp)}, which '
                    'holds a file; it is given field by field, its file by '
                    'reference to a dataset, which stands for the whole if the '
                    'group has one required file'
                )
            if isinstance(value, Mapping):
                return self._group(where, group, value, proposal)
        elif isinstance(value, Ref):
            return self._ref(where, value, tp, proposal)
        elif spec.has_json_form(name):
            return self._inline(where, spec.params, name, value)
        if value is None and _nullable(spec, name):
            return None
        raise SubmitError(
            f'{where}: {value!r} is not a reference; {type_name(tp)} has no JSON '
            'form, so it is given by reference, a file as dataset(path=...)'
        )

    def _group(
        self,
        where: str,
        group: type[BaseModel],
        value: Mapping[str, Any],
        proposal: str,
    ) -> dict[str, Any]:
        """
        The group ``value`` at ``where``, given field by field, as the record
        holds it: each field a reference, a dataset's resolved, an inline
        value, if its type has a JSON form, or ``None``, if it is optional; a
        field left out holds its default. Like the params model, the group is
        never validated whole.
        """
        fields = group.model_fields
        if unknown := sorted(value.keys() - fields.keys()):
            raise SubmitError(f'{where}: {unknown}: not fields of {group.__name__}')
        held: dict[str, Any] = {}
        for name, info in fields.items():
            at = f'{where}.{name}'
            tp, nullable = optional(info.annotation)
            cell = value.get(name, _LEFT_OUT)
            if cell is _LEFT_OUT:
                if info.is_required():
                    raise SubmitError(f'{at}: a required field, not given')
                default = info.get_default(call_default_factory=True)
                held[name] = (
                    json_form(group, name, default, whole=True)
                    if has_json_form(group, name)
                    else default
                )
            elif isinstance(cell, Ref):
                if element(tp) is not None:
                    raise SubmitError(
                        f'{at}: {cell} names a whole list; a field of a group '
                        'that is a list is given inline'
                    )
                held[name] = self._ref(at, cell, tp, proposal)
            elif has_json_form(group, name):
                held[name] = self._inline(at, group, name, cell, whole=True)
            elif cell is None and nullable:
                held[name] = None
            else:
                raise SubmitError(
                    f'{at}: {cell!r} is not a reference; {type_name(tp)} has no '
                    'JSON form, so it is given by reference, a file as '
                    'dataset(path=...)'
                )
        return held

    def _inline(
        self,
        where: str,
        model: type[BaseModel],
        name: str,
        value: Any,
        whole: bool = False,
    ) -> Any:
        """
        The inline value ``value`` at ``where`` of the field ``name`` of
        ``model``, a parameter or a field of a group, as the record holds it,
        its JSON form (see :func:`ess.spec.field_types.inline`).
        """
        try:
            return inline(model, name, value, whole)
        except PydanticSerializationError as error:
            raise SubmitError(f'{self.id}: cannot be stored as JSON: {error}') from None
        except ValidationError as error:
            raise SubmitError(f'{self.id}: {_problems(error, where)}') from None
        except ValueError as error:
            raise SubmitError(f'{where}: {error}') from None

    def _ref(self, where: str, ref: Ref, tp: Any, proposal: str) -> Ref:
        """
        The reference ``ref`` at ``where``, a dataset's resolved. A file is
        given by reference to a dataset, one the proposal may read, and only a
        file is.
        """
        if not isinstance(ref, DatasetRef):
            if is_file(tp):
                raise SubmitError(
                    f'{where}: {ref} is not a dataset; a file, '
                    f'{type_name(tp)}, is given by reference to a dataset'
                )
            return ref
        if not is_file(tp):
            raise SubmitError(
                f'{where}: dataset {ref} does not fit {type_name(tp)}, '
                'which is not a file'
            )
        try:
            identity = self._datasets.resolve(ref)
        except KeyError:
            raise SubmitError(f'{where}: unknown dataset {ref}') from None
        metadata = self._datasets.metadata(identity)
        if not readable(metadata, proposal):
            raise SubmitError(
                f'{where}: dataset {ref} belongs to proposal {metadata["proposal"]}'
            )
        return identity


def _problems(error: ValidationError, where: str) -> str:
    """
    What a validation error finds wrong, each with its place, the first part
    of which, the parameter's name, is ``where``.
    """
    return '; '.join(
        '.'.join([where, *map(str, e['loc'][1:])]) + f": {e['msg']}"
        for e in error.errors()
    )


def _nullable(spec: Spec, name: str) -> bool:
    """Whether a value, or a row of a list parameter, ``name`` may be ``None``."""
    model = spec.params
    tp = model.model_fields[name].annotation
    return optional(element(tp) if is_list(model, name) else tp)[1]


def refs(spec: Spec, params: Mapping[str, Any]) -> list[tuple[str, Ref, Any]]:
    """
    Each reference in ``params`` by name, with its place and the type of the
    data it must fit: the parameter's type, its element type for a list
    parameter, or the type of the field of a group it stands for.
    """
    found = []
    for name, value in params.items():
        tp = value_type(spec.params.model_fields[name].annotation)
        for where, field, item in places(name, value):
            if (ref := as_ref(item)) is not None and (field is None or by_field(tp)):
                at = tp if field is None else base(tp).model_fields[field].annotation
                found.append((where, ref, value_type(at)))
    return found


def check_fit(spec: Spec, ref: OutputRef | AccumulatorRef, tp: Any, where: str) -> None:
    """
    Refuse a reference to an output that ``spec`` lacks, or whose data do not
    fit ``tp``. A reference with a key names one element of an output, of the
    output's element type, and the key of an element of a list is an integer
    in its canonical form, such as ``'0'`` or ``'12'``, not ``'012'``.
    """
    field = spec.outputs.model_fields.get(ref.output)
    if field is None:
        raise SubmitError(f'{where}: {SpecId.of(spec)} has no output {ref.output!r}')
    data, _ = optional(field.annotation)
    if isinstance(ref, OutputRef) and ref.key is not None:
        if (item := element(data)) is None:
            raise SubmitError(
                f'{where}: {ref} names an element of an output without elements'
            )
        if get_origin(data) is list and not re.fullmatch('0|[1-9][0-9]*', ref.key):
            raise SubmitError(
                f'{where}: {ref}: the key of an element of a list is an integer'
            )
        data = item
    if not fits(data, tp):
        raise SubmitError(
            f'{where}: {ref}, of type {type_name(data)}, does not fit {type_name(tp)}'
        )


def distinct(
    index: str, values: Sequence[Any], seen: Mapping[bytes, int]
) -> dict[bytes, int]:
    """
    The rows ``values`` of the table of ``index`` after the rows ``seen``,
    each by its JSON form, with its row. Refuses a value equal to one seen or
    to an earlier one: a reference by its fields, such as a dataset's
    identity, an inline value by its JSON form.
    """
    new: dict[bytes, int] = {}
    for i, value in enumerate(values, start=len(seen)):
        shown = to_json(value)
        first = seen[shown] if shown in seen else new.setdefault(shown, i)
        if first != i:
            raise SubmitError(
                f'{index}[{i}]: repeats {index}[{first}], {shown.decode()}'
            )
    return new


def _fixed(
    spec: Spec, values: Mapping[str, Any], blanks: Collection[str]
) -> dict[str, Any]:
    """
    The values of a request or a template by name, with the defaults of the
    parameters in neither ``values`` nor ``blanks`` (see :func:`fixed_values`).
    """
    try:
        return fixed_values(spec, values, blanks)
    except ValueError as error:
        raise SubmitError(str(error)) from None


def _columns(
    spec: Spec, values: Mapping[str, Any], indexes: Iterable[str]
) -> dict[str, Any]:
    """
    ``values`` by name with the columns of the tables of ``indexes`` checked
    and filled.

    A column is given with its index, as long as it. A column left out is
    ``None`` in each row if its element type is optional; a column whose
    element type is not optional is given whenever the table has rows. A
    table whose index is left out has no rows, and an empty list in each
    column, unless its index is required.
    """
    fields = spec.params.model_fields
    filled = dict(values)
    for index in indexes:
        attached = spec.tables[index]
        if index not in values:
            if given := [c for c in attached if c in values]:
                raise SubmitError(
                    f'{given}: columns of the table of {index}, given without it'
                )
            if fields[index].is_required():
                continue  # refused as a required parameter not given
            filled[index] = []
            rows = 0
        elif isinstance(values[index], list):
            rows = len(values[index])
        else:
            raise SubmitError(f'{index}: {values[index]!r} is not a list of rows')
        for column in attached:
            if column in values:
                cells = values[column]
                if isinstance(cells, list) and len(cells) != rows:
                    raise SubmitError(
                        f'{column}: {len(cells)} rows, but {index} has {rows}'
                    )
            elif rows == 0 or optional(element(fields[column].annotation))[1]:
                filled[column] = [None] * rows
            else:
                raise SubmitError(
                    f'{column}: a column whose element type is not optional, not given'
                )
    return filled


def _rows(spec: Spec, values: Mapping[str, Any], indexes: Collection[str]) -> None:
    """
    Refuse a table of ``indexes`` whose number of rows breaks a length
    constraint of its columns, or with an index value repeated; ``values`` by
    name, as the record holds them.
    """
    _lengths(spec, values, indexes)
    for index in indexes:
        distinct(index, values.get(index) or [], {})


def _lengths(spec: Spec, values: Mapping[str, Any], indexes: Iterable[str]) -> None:
    """
    Refuse a table of ``indexes`` whose number of rows breaks a length
    constraint of one of its columns, such as a required index without a row.
    """
    for index in indexes:
        n = len(values.get(index) or [])
        for name in spec.columns(index):
            if need := _length_problem(spec, name, n):
                raise SubmitError(
                    f'{index}: the table has {_rows_text(n)}; {name} needs {need}'
                )


def _length_problem(spec: Spec, name: str, n: int) -> str | None:
    """
    The length constraint of the list parameter ``name`` that ``n`` rows
    break, such as ``'at least 1'``, or ``None``.
    """
    for limit in spec.params.model_fields[name].metadata:
        if isinstance(limit, MinLen) and n < limit.min_length:
            return f'at least {limit.min_length}'
        if isinstance(limit, MaxLen) and n > limit.max_length:
            return f'at most {limit.max_length}'
    return None


def _rows_text(n: int) -> str:
    return {0: 'no row', 1: 'one row'}.get(n, f'{n} rows')

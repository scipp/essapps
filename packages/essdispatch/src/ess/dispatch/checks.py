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
- *Literals.* A group, or a list of groups, is given by value. Each literal
  is validated alone, by a model of its field, or for a column of a table of
  its element type (see :func:`model_alone`), from its JSON form, and a
  field that a model lacks is refused at any depth, since validating would
  drop it silently. The log holds the JSON form of the validated value, and
  the call phase validates that, so it must read back as the value: a number
  that is not finite has none. A reference where a literal is expected is
  refused.
- *Data values.* Any other parameter is given by reference, ``None`` if it is
  optional, or for a list parameter a list of them, so a file given as a
  string is refused: data are given by reference, a file as
  ``dataset(path=...)``. A dataset must be known, one the proposal may read,
  and fit its parameter, a file (see :func:`ess.spec.field_types.fits`); its
  name is resolved to the identity the record holds. A reference to a record
  or an accumulator is checked by the backend, which knows what the client
  may read (see :func:`check_fit`).
- *Table rules.* The columns of a table are given with its index, as many
  rows as it; a column left out is ``None`` in each row if its element type
  is optional, and a column whose element type is not optional is given
  whenever the table has rows. The number of rows meets the length
  constraints of the table's columns, so a table with a required index has a
  row, and the index values of a table are distinct, compared as the record
  holds them: a dataset by its identity, a reference to an output by record,
  output, and key, and a literal by its JSON form (see :func:`distinct`).

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
from pydantic_core import PydanticSerializationError, from_json, to_json

from ess.spec import AccumulatorRef, DatasetRef, OutputRef, Ref, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.field_types import (
    element,
    fits,
    is_file,
    model_of,
    optional,
    type_name,
    value_type,
)
from ess.spec.refs import as_ref, iter_refs

from .datasets import DatasetSource, readable
from .records import Request, SpecId, SubmitError, Template, read_refs

type Spec = WorkflowSpec[Any, Any]


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
        columns left out filled, literals in JSON's types.
        """
        spec = self.spec
        _check_names(spec, request.params)
        values = _columns(spec, request.params, spec.tables)
        values = _with_defaults(spec, values)
        params = self._values(values, proposal)
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
        _check_names(spec, template.params)
        left_open = spec.blank_fields(blanks)
        given = {n: v for n, v in template.params.items() if n not in left_open}
        tables = [index for index in spec.tables if index not in blanks]
        filled = _columns(spec, given, tables)
        try:
            defaults = fixed_values(spec, filled, blanks)
        except ValueError as error:
            raise SubmitError(str(error)) from None
        fixed = self._values(defaults, proposal)
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
        literal is checked by its column's element type, and each reference
        as a request's is. Whether an index value repeats one the table holds
        is the backend's to check, which knows the table (see
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
        ``values`` by name as the record holds them: each literal validated by
        its field, a column of a table by its element type (see
        :func:`model_alone`), and each data value a reference, with dataset
        names resolved.
        """
        return {
            name: self._literal(name, value)
            if self.spec.by_value(name)
            else self._data(name, value, proposal)
            for name, value in values.items()
        }

    def _literal(self, name: str, value: Any) -> Any:
        """
        The literal ``value`` of parameter ``name`` as the record holds it,
        its JSON form read back. It is validated from its JSON form, as the
        call phase validates the log's. A field that a model lacks is refused
        at any depth, and so is a value whose JSON form does not read back as
        the value, such as a number that is not finite, which JSON holds as
        ``null``.
        """
        for where, ref in iter_refs({name: value}):
            raise SubmitError(
                f'{where}: {ref} is a reference; {name} is given by value'
            )
        model = model_alone(self.spec, name)
        try:
            given = to_json({name: value}, inf_nan_mode='constants')
        except PydanticSerializationError as error:
            raise SubmitError(f'{self.id}: cannot be stored as JSON: {error}') from None
        try:
            made = model.model_validate_json(given, extra='forbid')
        except ValidationError as error:
            raise SubmitError(f'{self.id}: {_problems(error)}') from None
        line = to_json(made, by_alias=True)
        try:
            again = model.model_validate_json(line)
        except ValidationError:
            reads_back = False
        else:
            reads_back = getattr(again, name) == getattr(made, name)
        if not reads_back:
            raise SubmitError(f'{name}: has no JSON form that reads back as the value')
        return from_json(line)[name]

    def _data(self, name: str, value: Any, proposal: str) -> Any:
        """
        The data value ``value`` of parameter ``name`` as the record holds it:
        a reference, ``None`` if the parameter is optional, or for a list
        parameter a list of them; dataset names resolved.
        """
        tp, nullable = optional(self.spec.params.model_fields[name].annotation)
        if (item := element(tp)) is None:
            return self._ref(name, value, tp, nullable, proposal)
        if not isinstance(value, list):
            raise SubmitError(f'{name}: {value!r} is not a list of references')
        item, nullable = optional(item)
        return [
            self._ref(f'{name}[{i}]', cell, item, nullable, proposal)
            for i, cell in enumerate(value)
        ]

    def _ref(
        self, where: str, value: Any, tp: Any, nullable: bool, proposal: str
    ) -> Ref | None:
        """
        The reference ``value`` at ``where``, a dataset's resolved, or an
        allowed ``None``. A dataset must be one the proposal may read, and
        fit ``tp``, a file.
        """
        if value is None and nullable:
            return None
        try:
            ref = as_ref(value)
        except ValueError as error:
            raise SubmitError(f'{where}: {error}') from None
        if ref is None:
            raise SubmitError(
                f'{where}: {value!r} is not a reference; data are given by '
                'reference, a file as dataset(path=...)'
            )
        if not isinstance(ref, DatasetRef):
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


def _problems(error: ValidationError) -> str:
    """What a validation error finds wrong, each with its place."""
    return '; '.join(
        f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}"
        for e in error.errors()
    )


def model_alone(spec: Spec, name: str) -> type[BaseModel]:
    """
    The model that validates a value of the parameter ``name`` alone, in both
    phases (see :func:`model_of`): its field, or for a column of a table its
    element type, since the table rules check the number of rows of a table
    as a whole, whether given whole or pushed (see :func:`_lengths`).
    """
    return model_of(spec.params, name, rows=spec.table_of(name) is not None)


def validated(spec: Spec, name: str, value: Any) -> Any:
    """
    The value of the parameter ``name`` validated alone (see
    :func:`model_alone`), as the call phase gives it to the binding: a
    literal from its JSON form, which the log holds, so that both phases see
    the same value, and a data value from the object read.
    """
    model = model_alone(spec, name)
    if spec.by_value(name):
        return getattr(model.model_validate_json(to_json({name: value})), name)
    return getattr(model.model_validate({name: value}), name)


def refs(spec: Spec, params: Mapping[str, Any]) -> list[tuple[str, Ref, Any]]:
    """
    Each reference in ``params`` by name, with its place and the type of the
    data it must fit: the parameter's type, or its element type for a list
    parameter.
    """
    fields = spec.params.model_fields
    return [
        (where, ref, value_type(fields[name].annotation))
        for name, value in params.items()
        for where, ref in iter_refs({name: value})
    ]


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
    identity, a literal by its JSON form.
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


def _check_names(spec: Spec, params: Mapping[str, Any]) -> None:
    """Refuse a name the spec lacks."""
    if unknown := sorted(params.keys() - spec.params.model_fields.keys()):
        raise SubmitError(f'{unknown}: not parameters of {SpecId.of(spec)}')


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
    fields = spec.params.model_fields
    for index in indexes:
        n = len(values.get(index) or [])
        rows = {0: 'no row', 1: 'one row'}.get(n, f'{n} rows')
        for name in spec.columns(index):
            for limit in fields[name].metadata:
                if isinstance(limit, MinLen) and n < limit.min_length:
                    raise SubmitError(
                        f'{index}: the table has {rows}; {name} needs at least '
                        f'{limit.min_length}'
                    )
                if isinstance(limit, MaxLen) and n > limit.max_length:
                    raise SubmitError(
                        f'{index}: the table has {rows}; {name} needs at most '
                        f'{limit.max_length}'
                    )


def _with_defaults(spec: Spec, values: Mapping[str, Any]) -> dict[str, Any]:
    """
    Every parameter: as given in ``values``, or its default; refuses a
    required parameter not given.
    """
    fields = spec.params.model_fields
    if missing := [n for n, f in fields.items() if n not in values and f.is_required()]:
        raise SubmitError(f'{missing}: required parameters, not given')
    return {
        n: values[n]
        if n in values
        else f.get_default(call_default_factory=True, validated_data={})
        for n, f in fields.items()
    }

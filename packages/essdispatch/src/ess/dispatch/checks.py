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

- *Names and sides.* Each name is a parameter or an input of the spec, and
  given on its side: a parameter in ``params``, an input in ``inputs``.
- *Params.* The params model validates the JSON form of the params, its own
  model validators included, and refuses a field that a model lacks, at any
  depth, since validating would drop it silently. The log holds the JSON
  form of the validated params, and the call phase validates that, so it
  must read back as the value: a number that is not finite has none.
- *Inputs.* Each input is a reference, ``None`` for an optional field, or
  for a list field a list of them, so a file given as a string is refused:
  data are given by reference, a file as ``dataset(path=...)``. A required
  input must be given; one left out takes its default. A dataset must be
  known, one the proposal may read, and fit its field, a file (see
  :func:`ess.spec.field_types.fits`); its name is resolved to the identity
  the record holds. A reference to a record or an accumulator is checked by
  the backend, which knows what the client may read (see
  :func:`check_fit`).
- *Table rules.* The columns of a table are given with its index, as many
  rows as it; a column left out is ``None`` in each row if its element type
  is optional, and a column whose element type is not optional is given
  whenever the table has rows. A table with a required index has a row, and
  the index values of a table are distinct, compared as the record holds
  them: a dataset by its identity, a reference to an output by record,
  output, and key, and a literal by its JSON form (see :func:`distinct`).

A template's fixed values are checked each by its own field, without the
params model's own validators, which read whole requests: they run on each
request through a stage, and on the state of an accumulator when it is read.
A push is checked by its columns' types alone. The checks need neither the
backend's lock nor a client: what depends on history or on what a client
keeps is the backend's.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, get_origin

from pydantic import BaseModel, Field, ValidationError, create_model
from pydantic_core import PydanticSerializationError, from_json, to_json

from ess.spec import AccumulatorRef, DatasetRef, OutputRef, Ref, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.field_types import element, fits, is_file, optional, type_name
from ess.spec.refs import as_ref, map_refs

from .datasets import DatasetSource, readable
from .records import Request, SpecId, SubmitError, Template

type Spec = WorkflowSpec[Any, Any, Any]


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
        columns left out filled, params in JSON's types.
        """
        spec = self.spec
        _check_names(spec, request.params, request.inputs)
        values = _columns(spec, {**request.params, **request.inputs}, spec.tables)
        params, inputs = spec.split(values)
        inputs = self._resolve(inputs, proposal)
        inputs = _with_defaults(spec.inputs, inputs, spec.inputs.model_fields)
        params = self._params(params)
        _rows(spec, {**params, **inputs}, spec.tables)
        return Request(self.id, params=params, inputs=inputs)

    def template(
        self, template: Template, proposal: str
    ) -> tuple[Template, dict[str, Any]]:
        """
        The template as given, with dataset names resolved and the columns
        given for a blank table dropped, as the stage or the accumulator
        holds it, and its fixed values by name, defaults filled in.

        A blank is a parameter, an input, or a table, named by its index; a
        template fixes a whole table or leaves it blank. A parameter or an
        input in neither the values nor the blanks takes its default, so it
        must have one. Each fixed value is checked by its own field, and the
        table rules hold on the fixed tables.
        """
        spec, blanks = self.spec, template.blanks
        _check_names(spec, template.params, template.inputs)
        left_open = spec.blank_fields(blanks)
        given = {
            n: v
            for n, v in {**template.params, **template.inputs}.items()
            if n not in left_open
        }
        tables = [index for index in spec.tables if index not in blanks]
        filled = _columns(spec, given, tables)
        try:
            fixed_values(spec, filled, blanks)  # the rules on blanks and defaults
        except ValueError as error:
            raise SubmitError(str(error)) from None
        names = (spec.params.model_fields | spec.inputs.model_fields).keys() - left_open
        params, inputs = spec.split(filled)
        params = self._params(params, names)
        inputs = self._resolve(inputs, proposal)
        stored = Template(
            self.id,
            params={n: params[n] for n in given if n in params},
            inputs={n: inputs[n] for n in given if n in inputs},
            blanks=blanks,
        )
        inputs = _with_defaults(spec.inputs, inputs, names)
        _rows(spec, {**params, **inputs}, tables)
        return stored, {**params, **inputs}

    def push(
        self, blanks: Collection[str], columns: Mapping[str, Any], proposal: str
    ) -> tuple[str, dict[str, list[Any]]]:
        """
        The index of the table a push names, and its columns by name, as the
        log holds them.

        A push gives the columns of one of the ``blanks``, a table: its
        index, and the columns attached to it, as lists of equal length, one
        or more rows; a column left out is filled as a request's is. Each
        literal is checked by its column's type, and each dataset as a
        request's is. Whether an index value repeats one the table holds is
        the backend's to check, which knows the table (see :func:`distinct`).
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
        params, inputs = spec.split(_columns(spec, columns, [index]))
        inputs = self._resolve(inputs, proposal)
        return index, {**self._params(params, params.keys(), rows=True), **inputs}

    def state(self, plain: Request, blanks: Collection[str]) -> str | None:
        """
        Why the plain request of an accumulator's state would be refused, or
        ``None``.

        Each value was checked by its field when it was given, so this checks
        the rest: the params model, its own validators included, on the
        params' JSON form, as the call phase validates them, and a row in
        each table with a required index among the ``blanks``. It takes time
        in proportion to the rows of the params' tables.
        """
        try:
            _required_rows(self.spec, {**plain.params, **plain.inputs}, blanks)
            self.spec.params.model_validate_json(to_json(plain.params))
        except SubmitError as error:
            return str(error)
        except ValidationError as error:
            return f'{self.id}: {_problems(error)}'
        return None

    def _resolve(self, inputs: Mapping[str, Any], proposal: str) -> dict[str, Any]:
        """
        ``inputs``, each a reference or a list of references by its field,
        with dataset names resolved. A dataset must be one the proposal may
        read, and fit its field, a file.
        """
        resolved: dict[Ref, Ref] = {}
        for where, ref, tp in refs(self.spec.inputs, inputs):
            if not isinstance(ref, DatasetRef):
                continue
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
            resolved[ref] = identity
        return map_refs(inputs, lambda ref: resolved.get(ref, ref))

    def _params(
        self,
        params: Mapping[str, Any],
        names: Collection[str] | None = None,
        *,
        rows: bool = False,
    ) -> dict[str, Any]:
        """
        ``params`` as the record holds them, their JSON form read back,
        defaults filled in. They are validated from their JSON form, as the
        call phase validates the log's, by the spec's params model, or if
        ``names`` are given, each of these by its own field, or with ``rows``
        as the columns of rows pushed (see :func:`model_of`). A field that a
        model lacks is refused at any depth, and so is a value whose JSON
        form does not read back as the value, such as a number that is not
        finite, which JSON holds as ``null``.
        """
        model = self.spec.params
        if names is not None:
            model = model_of(model, names, rows=rows)
        try:
            given = to_json(params, inf_nan_mode='constants')
        except PydanticSerializationError as error:
            raise SubmitError(f'{self.id}: cannot be stored as JSON: {error}') from None
        try:
            made = model.model_validate_json(given, extra='forbid')
        except ValidationError as error:
            raise SubmitError(f'{self.id}: {_problems(error)}') from None
        line = to_json(made, by_alias=True)
        try:
            again = model.model_validate_json(line)
        except ValidationError as error:
            unread = sorted(
                {str(e['loc'][0]) if e['loc'] else 'params' for e in error.errors()}
            )
        else:
            unread = [
                n for n in model.model_fields if getattr(again, n) != getattr(made, n)
            ]
        if unread:
            raise SubmitError(
                f'{", ".join(unread)}: has no JSON form that reads back as the value'
            )
        return from_json(line)


def _problems(error: ValidationError) -> str:
    """What a validation error finds wrong, each with its place."""
    return '; '.join(
        f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}"
        for e in error.errors()
    )


def model_of(
    model: type[BaseModel], names: Collection[str], *, rows: bool = False
) -> type[BaseModel]:
    """
    A model of the fields ``names`` of ``model`` alone, with its config and
    without its validators, which read whole requests: it validates each value
    by its own field. A spec's params and inputs models have no decorators
    but model validators; a check of one value belongs to the value's type.

    With ``rows``, each field is a column of rows pushed into a table: it is
    validated by its type alone, without its constraints on the whole table,
    such as its length.
    """
    return _model_of(model, frozenset(names), rows)


@functools.cache
def _model_of(
    model: type[BaseModel], names: frozenset[str], rows: bool
) -> type[BaseModel]:
    fields: dict[str, Any] = {
        f: (info.annotation, Field() if rows else info)
        for f, info in model.model_fields.items()
        if f in names
    }
    return create_model(model.__name__, __config__=model.model_config, **fields)


def refs(
    model: type[BaseModel], inputs: Mapping[str, Any]
) -> list[tuple[str, Ref, Any]]:
    """
    Each reference in ``inputs``, fields of the inputs ``model``, with its
    place and the type of the data it must fit: the field's type, or its
    element's for a list field.

    Raises ``SubmitError`` for a value that is not a reference, ``None`` for
    an optional field, or for a list field a list of them.
    """
    found = []
    for name, value in inputs.items():
        tp, nullable = optional(model.model_fields[name].annotation)
        if (item := element(tp)) is None:
            found += _ref(name, value, tp, nullable)
        elif not isinstance(value, list):
            raise SubmitError(f'{name}: {value!r} is not a list of references')
        else:
            item, nullable = optional(item)
            for i, cell in enumerate(value):
                found += _ref(f'{name}[{i}]', cell, item, nullable)
    return found


def _ref(where: str, value: Any, tp: Any, nullable: bool) -> list[tuple[str, Ref, Any]]:
    """The reference ``value`` at ``where``, or none for an allowed ``None``."""
    if value is None and nullable:
        return []
    if (ref := as_ref(value)) is None:
        raise SubmitError(
            f'{where}: {value!r} is not a reference; data are given by reference, '
            'a file as dataset(path=...)'
        )
    return [(where, ref, tp)]


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


def _check_names(
    spec: Spec, params: Mapping[str, Any], inputs: Mapping[str, Any]
) -> None:
    """Refuse a name the spec lacks, or one given on the other side."""
    spec_id = SpecId.of(spec)
    fields = spec.params.model_fields.keys() | spec.inputs.model_fields.keys()
    if unknown := sorted((params.keys() | inputs.keys()) - fields):
        raise SubmitError(f'{unknown}: not parameters or inputs of {spec_id}')
    for side, values in (('params', params), ('inputs', inputs)):
        for name in values:
            if (actual := spec.side(name)) != side:
                what = 'a parameter' if actual == 'params' else 'an input'
                raise SubmitError(f'{name}: {what} of {spec_id}, given in {side}')


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
    filled = dict(values)
    for index in indexes:
        attached = spec.tables[index]
        if index not in values:
            if given := [c for c in attached if c in values]:
                raise SubmitError(
                    f'{given}: columns of the table of {index}, given without it'
                )
            if spec.field(index).is_required():
                continue  # refused by the table rules
            filled[index] = []
            rows = 0
        elif isinstance(values[index], list):
            rows = len(values[index])
        else:
            continue  # validation refuses it
        for column in attached:
            if column in values:
                cells = values[column]
                if isinstance(cells, list) and len(cells) != rows:
                    raise SubmitError(
                        f'{column}: {len(cells)} rows, but {index} has {rows}'
                    )
            elif rows == 0 or optional(element(spec.field(column).annotation))[1]:
                filled[column] = [None] * rows
            else:
                raise SubmitError(
                    f'{column}: a column whose element type is not optional, not given'
                )
    return filled


def _rows(spec: Spec, values: Mapping[str, Any], indexes: Collection[str]) -> None:
    """
    Refuse a table of ``indexes`` with a required index and no row, or with
    an index value repeated; ``values`` by name, as the record holds them.
    """
    _required_rows(spec, values, indexes)
    for index in indexes:
        distinct(index, values.get(index) or [], {})


def _required_rows(
    spec: Spec, values: Mapping[str, Any], indexes: Iterable[str]
) -> None:
    """Refuse a table of ``indexes`` with a required index and no row."""
    for index in indexes:
        if spec.field(index).is_required() and not values.get(index):
            raise SubmitError(f'{index}: the table has no row; its index is required')


def _with_defaults(
    model: type[BaseModel], values: Mapping[str, Any], names: Collection[str]
) -> dict[str, Any]:
    """
    The inputs ``names`` of ``model``: those given in ``values``, the default
    of the others; refuses a required input not given.
    """
    fields = {n: f for n, f in model.model_fields.items() if n in names}
    if missing := [n for n, f in fields.items() if n not in values and f.is_required()]:
        raise SubmitError(f'{missing}: required inputs, not given')
    return {
        n: values[n]
        if n in values
        else f.get_default(call_default_factory=True, validated_data={})
        for n, f in fields.items()
    }

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

    checks = Checks(IOFQ, files)
    request = checks.request(request, proposal)            # a plain request
    template, fixed = checks.template(template, proposal)  # a stage or an accumulator
    rows = checks.push(blanks, rows, proposal)
    refused = checks.state(plain, blanks)                  # at a pin or a read

The checks of a request; a template, a push, and a state check part of these:

- *Names.* Each name is a parameter of the spec. A required parameter must
  be given; one left out takes its default.
- *Places.* A request writes a value or a reference at each place: the
  value of a parameter, or an element of a list parameter (see
  :mod:`ess.spec.places`). A reference at a tag, for a whole list, at a
  union of groups, or inside a group is refused, but one to a file at a
  field typed a file model. A group is written as an object of its
  fields; a field left out takes its default.
- *Inline values.* An inline value is allowed where the type at the place
  is a group without a data value, or a union of groups, and ``None`` where
  it is optional; so a file model given inline, such as a path as a
  string, is refused: it is given as ``file(path=...)``. Each group, or
  row of a list, given inline is validated whole, so that its validators
  run, by its field, or for an element of a list by its element type, from
  its JSON form, an instance by its dump, and a field that a model lacks is
  refused, since validating would drop it silently. The log holds the JSON
  form of each validated value, and the call phase makes the value of that,
  so it must read back as the value: a number that is not finite has none,
  nor has a group whose validators change a value they validated already
  (see :func:`ess.spec.field_types.inline`). A list parameter that is not a
  table meets its length constraints.
- *References.* A reference to an output stands for a whole group. A union of groups is
  written inline only, with the tag of an alternative: no reference fits
  it, so that a producer need not output a tagged class to feed it. A
  file model is given by reference to a file, and only a file
  model is (see :class:`ess.spec.FileModel`): the file source checks
  that the proposal may read it, the file fits the model by its
  metadata, as a :class:`~.files.Selector` of the model's metadata
  matches it, and the record holds its identity, to which its name is
  resolved, so it must be known. A reference to a record or an accumulator
  is checked by the backend, which knows what the client may read (see
  :func:`check_fit`): its data fit the place. No output holds a file
  model, so a path comes only from a file.
- *Table rules.* The number of rows of a table meets its length
  constraints, and its rows are distinct, compared as the record holds them:
  a file by its identity, a reference to an output by record, output, and
  key, and an inline value by its JSON form, each at its place (see
  :func:`distinct`).

The params model's model validators never run: a stage and a driver never
get a whole params instance. A template's fixed values are checked each by
its own field, and a push by its table's element type. The checks need
neither the backend's lock nor a client: what depends on history or on what a
client keeps is the backend's.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, get_origin

from annotated_types import MaxLen, MinLen
from pydantic_core import to_json

from ess.spec import (
    AccumulatorRef,
    FileRef,
    OutputRef,
    Ref,
    WorkflowSpec,
    field_types,
    places,
)
from ess.spec.binding import fixed_values
from ess.spec.refs import Loc, ParamsError

from .errors import SubmitError
from .files import FileSource, Selector, readable
from .records import Request, SpecId, Template, read_refs

type Spec = WorkflowSpec[Any, Any]


class Checks:
    """
    Request-phase checks of one spec: of a plain request, a template, a push,
    and the state of an accumulator.
    """

    def __init__(self, spec: Spec, files: FileSource) -> None:
        self.spec = spec
        self.id = SpecId.of(spec)
        self._files = files

    def request(self, request: Request, proposal: str) -> Request:
        """
        The request as the record holds it: names resolved, defaults filled
        in, inline values in JSON's types.
        """
        spec = self.spec
        params = self._values(_fixed(spec, request.params, ()), proposal)
        _rows(spec, params, spec.tables)
        return Request(self.id, params=params)

    def template(
        self, template: Template, proposal: str
    ) -> tuple[Template, dict[str, Any]]:
        """
        The template as given, with file names resolved, as the stage or
        the accumulator holds it, and its fixed values by name, defaults
        filled in.

        A blank is a parameter, a table among them. A parameter in neither
        the values nor the blanks takes its default, so it must have one. Each
        fixed value is checked by its own field, and the table rules hold on
        the fixed tables.
        """
        spec, blanks = self.spec, template.blanks
        given = {n: v for n, v in template.params.items() if n not in blanks}
        fixed = self._values(_fixed(spec, given, blanks), proposal)
        _rows(spec, fixed, [t for t in spec.tables if t not in blanks])
        stored = Template(self.id, params={n: fixed[n] for n in given}, blanks=blanks)
        return stored, fixed

    def push(
        self, blanks: Collection[str], rows: Mapping[str, Any], proposal: str
    ) -> dict[str, list[Any]]:
        """
        The rows of a push by the name of their table, as the log holds them.

        A push gives one or more rows of each of one or more of the
        ``blanks``, tables. Each row is checked by its table's element type,
        as a request's is. Whether a row repeats one the table holds is the
        backend's to check, which knows the table (see :func:`distinct`).
        """
        if not rows:
            raise SubmitError.at((), 'a push gives rows of a blank table, not none')
        for table, given in rows.items():
            if table not in blanks:
                raise SubmitError.at(
                    (table,), f'not a blank table of the accumulator, {list(blanks)}'
                )
            if not given:
                raise SubmitError.at((table,), 'a push gives no rows')
        return self._values(read_refs(dict(rows)), proposal)

    def state(self, plain: Request, blanks: Collection[str]) -> str | None:
        """
        Why the plain request of an accumulator's state would be refused, or
        ``None``.

        Each value was checked by its field when it was given, and the params
        model's model validators never run, so this checks the rest: the
        number of rows of each table among the ``blanks``, by its length
        constraints.
        """
        try:
            _lengths(self.spec, plain.params, blanks)
        except SubmitError as error:
            return str(error)
        return None

    def _values(self, values: Mapping[str, Any], proposal: str) -> dict[str, Any]:
        """``values`` by name as the record holds them (see :meth:`_value`)."""
        return {
            name: self._value(name, value, proposal) for name, value in values.items()
        }

    def _value(self, name: str, value: Any, proposal: str) -> Any:
        """
        The value of parameter ``name`` as the record holds it: at each place,
        the JSON form of an inline value, a reference, a file's resolved,
        or ``None`` (see :func:`ess.spec.places.held`).
        """
        try:
            stored = places.held(
                self.spec.params,
                name,
                value,
                lambda loc, ref, tp: self._ref(loc, ref, tp, proposal),
            )
        except ParamsError as error:
            raise SubmitError.of(error) from None
        if (
            field_types.is_list(self.spec.params, name)
            and name not in self.spec.tables
            and (need := _length_problem(self.spec, name, len(stored)))
        ):
            raise SubmitError.at((name,), f'{_rows_text(len(stored))}; it needs {need}')
        return stored

    def _ref(self, loc: Loc, ref: Ref, tp: Any, proposal: str) -> Ref:
        """
        The reference ``ref`` at ``loc``, a file's resolved. A file
        model is given by reference to a file, one the proposal may read
        and that fits the model, and only a file model is.
        """
        name = field_types.type_name(tp)
        if not isinstance(ref, FileRef):
            if field_types.is_file_model(tp):
                raise SubmitError.at(
                    loc,
                    f'{ref} is not a file; {name} is given by reference to a file',
                )
            return ref
        if not field_types.is_file_model(tp):
            raise SubmitError.at(
                loc, f'file {ref} does not fit {name}, which is not a file model'
            )
        try:
            identity = self._files.resolve(ref)
        except KeyError:
            raise SubmitError.at(loc, f'unknown file {ref}') from None
        metadata = self._files.metadata(identity)
        if not readable(metadata, proposal):
            raise SubmitError.at(
                loc, f'file {ref} belongs to proposal {metadata["proposal"]}'
            )
        if not (selector := Selector(**tp.metadata)).matches(metadata):
            raise SubmitError.at(
                loc,
                f'file {ref} does not fit {name}, which takes files with '
                f'{selector.fields}',
            )
        return identity


def check_fit(spec: Spec, ref: OutputRef | AccumulatorRef, tp: Any, loc: Loc) -> None:
    """
    Refuse a reference to an output that ``spec`` lacks, or whose data do
    not fit ``tp``. A reference with a key names one element of an output, of
    the output's element type, and the key of an element of a list is an
    integer in its canonical form, such as ``'0'`` or ``'12'``, not
    ``'012'``.
    """
    field = spec.outputs.model_fields.get(ref.output)
    if field is None:
        raise SubmitError.at(loc, f'{SpecId.of(spec)} has no output {ref.output!r}')
    data, _ = field_types.optional(field.annotation)
    if isinstance(ref, OutputRef) and ref.key is not None:
        if (item := field_types.element(data)) is None:
            raise SubmitError.at(
                loc, f'{ref} names an element of an output without elements'
            )
        if get_origin(data) is list and not re.fullmatch('0|[1-9][0-9]*', ref.key):
            raise SubmitError.at(
                loc, f'{ref}: the key of an element of a list is an integer'
            )
        data = item
    if not field_types.fits(data, tp):
        raise SubmitError.at(
            loc,
            f'{ref}, of type {field_types.type_name(data)}, does not fit '
            f'{field_types.type_name(tp)}',
        )


def distinct(
    table: str, rows: Sequence[Any], seen: Mapping[bytes, int]
) -> dict[bytes, int]:
    """
    The ``rows`` of ``table`` after the rows ``seen``, each by its JSON form,
    with its index. Refuses a row equal to one seen or to an earlier one: a
    reference by its fields, such as a file's identity, and a value given
    inline by its JSON form.
    """
    new: dict[bytes, int] = {}
    for i, row in enumerate(rows, start=len(seen)):
        shown = to_json(row)
        first = seen[shown] if shown in seen else new.setdefault(shown, i)
        if first != i:
            raise SubmitError.at(
                (table, i), f'repeats {table}[{first}], {shown.decode()}'
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
    except ParamsError as error:
        raise SubmitError.of(error) from None


def _rows(spec: Spec, values: Mapping[str, Any], tables: Collection[str]) -> None:
    """
    Refuse a table of ``tables`` whose number of rows breaks its length
    constraints, or with a row repeated; ``values`` by name, as the record
    holds them.
    """
    _lengths(spec, values, tables)
    for table in tables:
        distinct(table, values.get(table) or [], {})


def _lengths(spec: Spec, values: Mapping[str, Any], tables: Iterable[str]) -> None:
    """
    Refuse a table of ``tables`` whose number of rows breaks its length
    constraints, such as a table that needs a row without one.
    """
    for table in tables:
        n = len(values.get(table) or [])
        if need := _length_problem(spec, table, n):
            raise SubmitError.at(
                (table,), f'the table has {_rows_text(n)}; it needs {need}'
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

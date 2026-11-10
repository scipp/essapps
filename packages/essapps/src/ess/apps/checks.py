# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Request-phase checks: what the workspace checks of a request, a template, a
push, and the state of an accumulator before any data is read.

The workspace checks a request in two phases (see ``workspace.py``): this module
is the part of the request phase that needs neither the workspace's lock nor
its history. What depends on history or on what the workspace keeps is the
workspace's.
:class:`Checks` holds the checks of one spec, one method for each thing
checked::

    checks = Checks(IOFQ, files)
    request, reads = checks.request(request, proposal)  # a plain request
    # a stage or an accumulator
    template, fixed, reads = checks.template(template, proposal)
    rows, reads = checks.push(blanks, rows, proposal)
    refused = checks.state(plain, blanks)                  # at a read or a freeze

The methods differ in what they hold each value against, since a template and
a push give only some of the params:

- ``request``: each parameter by its field, and the table rules on its tables.
- ``template``: each fixed value by its own field, and the table rules on the
  fixed tables.
- ``push``: each row by its table's element type. Whether a row repeats one the
  table holds is the workspace's to check, which knows the table.
- ``state``: the number of rows of each blank table, since each value was
  checked when it was given.

They share ``_values``, which validates a value by its request type (see
:func:`ess.spec.validation.validate_request_value`), resolves each file with
the file source, and returns what the record stores. Three choices in it follow
from the call phase:

- A parameter that holds no reference to an output is also validated whole by
  the params model, with the local path of each file, so that the validators of
  a group that holds a file run at submission.
- A field that a model lacks is refused, since validating would drop it
  silently.
- The JSON form of a value must read back as the value, since the call phase
  makes the value from it (see :func:`ess.spec.field_types.inline`).

Each check returns the references to outputs in the values, each with its
position and the type its data must fit (see
:data:`ess.spec.validation.OutputReads`). The workspace checks each against what
may be read (see :func:`check_fit`).

The params model has no validators of its own, which
:class:`~ess.spec.WorkflowSpec` refuses: a stage and a driver never get a whole
params instance. The terms (group, file field, reference, table) and the
rules of what a request may write are in ``packages/essspec/docs/params.md``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, get_origin

from annotated_types import MaxLen, MinLen
from pydantic_core import to_json

from ess.spec import (
    File,
    FileRef,
    OutputRef,
    WorkflowSpec,
    field_types,
    validation,
)
from ess.spec.binding import fixed_values
from ess.spec.refs import Loc, ParamsError
from ess.spec.validation import OutputReads

from .errors import SubmitError
from .files import Selector, readable
from .protocol import FileSource
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

    def request(self, request: Request, proposal: str) -> tuple[Request, OutputReads]:
        """
        The request as the record holds it: names resolved, defaults filled
        in, inline values in JSON's types; and the references to outputs in it.
        """
        spec = self.spec
        params, reads = self._values(_fixed(spec, request.params, ()), proposal)
        _rows(spec, params, spec.tables)
        return Request(self.id, params=params), reads

    def template(
        self, template: Template, proposal: str
    ) -> tuple[Template, dict[str, Any], OutputReads]:
        """
        The template as given, with file names resolved, as the stage or
        the accumulator holds it, its fixed values by name, defaults filled
        in, and the references to outputs in these.

        A blank is a parameter, a table among them. A parameter in neither
        the values nor the blanks takes its default, so it must have one. Each
        fixed value is checked by its own field, and the table rules hold on
        the fixed tables.
        """
        spec, blanks = self.spec, template.blanks
        given = {n: v for n, v in template.params.items() if n not in blanks}
        fixed, reads = self._values(_fixed(spec, given, blanks), proposal)
        _rows(spec, fixed, [t for t in spec.tables if t not in blanks])
        stored = Template(self.id, params={n: fixed[n] for n in given}, blanks=blanks)
        return stored, fixed, reads

    def push(
        self, blanks: Collection[str], rows: Mapping[str, Any], proposal: str
    ) -> tuple[dict[str, list[Any]], OutputReads]:
        """
        The rows of a push by the name of their table, as the log holds them,
        and the references to outputs in them.

        A push gives one or more rows of each of one or more of the
        ``blanks``, tables. Each row is checked by its table's element type,
        as a request's is. Whether a row repeats one the table holds is the
        workspace's to check, which knows the table (see :func:`distinct`).
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
        model has no validators of its own, so this checks the rest: the
        number of rows of each table among the ``blanks``, by its length
        constraints.
        """
        try:
            _lengths(self.spec, plain.params, blanks)
        except SubmitError as error:
            return str(error)
        return None

    def _values(
        self, values: Mapping[str, Any], proposal: str
    ) -> tuple[dict[str, Any], OutputReads]:
        """
        ``values`` by name as the record holds them, and the references to
        outputs in them (see :meth:`_value`).
        """
        stored, reads = {}, []
        for name, value in values.items():
            stored[name], found = self._value(name, value, proposal)
            reads.extend(found)
        return stored, reads

    def _value(self, name: str, value: Any, proposal: str) -> tuple[Any, OutputReads]:
        """
        The value of parameter ``name`` as the record holds it, the JSON form
        of an inline value with each file resolved, and the references to
        outputs in it (see :func:`ess.spec.validation.validate_request_value`).
        """
        try:
            return validation.validate_request_value(
                self.spec.params,
                name,
                value,
                lambda file, marker: self._file(file, marker, proposal),
                self._files.path,
                table=name in self.spec.tables,
            )
        except ParamsError as error:
            raise SubmitError.of(error) from None

    def _file(self, name: str, marker: File, proposal: str) -> str:
        """
        The identity of the file ``name``, which must be known, readable by
        ``proposal``, and fit ``marker``; raises ``ValueError`` otherwise.
        """
        ref = FileRef(file=name)
        try:
            identity = self._files.resolve(ref)
        except KeyError:
            raise ValueError(f'unknown file {ref}') from None
        metadata = self._files.metadata(identity)
        if not readable(metadata, proposal):
            raise ValueError(f'file {ref} belongs to proposal {metadata["proposal"]}')
        if not (selector := Selector.of(marker)).matches(metadata):
            raise ValueError(
                f'file {ref} does not fit the field, which takes files with '
                f'{selector.fields}'
            )
        return identity.file


def check_fit(spec: Spec, ref: OutputRef, tp: Any, loc: Loc) -> None:
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
    if ref.key is not None:
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

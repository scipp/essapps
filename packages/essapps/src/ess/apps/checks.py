# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The checks of the request phase that need neither the workspace's lock nor its
history.

:class:`Checks` holds the checks of one spec::

    checks = Checks(IOFQ, files)
    request, reads = checks.request(request, proposal)  # a plain request
    # a stage or an accumulator
    template, fixed, reads = checks.template(template, proposal)
    rows, reads = checks.push(blanks, rows, proposal)
    problem = checks.state_problem(plain, blanks)  # at a read or a freeze

Each check returns the references to outputs in the values, with the type that
their data must fit (:data:`ess.spec.validation.OutputReads`). The workspace
checks them against what may be read, and with :func:`check_fit`.
:func:`ess.spec.validation.validate_request_value` validates each value.

``docs/developer/accumulators.md``, "Validation", shows what each check holds a
value against. ``packages/essspec/docs/params.md`` defines groups, file fields,
references and tables.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
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
        The request as the record holds it, and the references to outputs in it.

        File names are resolved, defaults filled in, and inline values are in
        JSON's types.
        """
        spec = self.spec
        params, reads = self._values(_fixed(spec, request.params, ()), proposal)
        _rows(spec, params, spec.tables)
        return Request(self.id, params=params), reads

    def template(
        self, template: Template, proposal: str
    ) -> tuple[Template, dict[str, Any], OutputReads]:
        """
        The template as the stage or accumulator holds it, its fixed values by
        name, and the references to outputs in these.

        The template has file names resolved. The fixed values also have
        defaults filled in. A parameter in neither the values nor the blanks
        takes its default, so it must have one.
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
        The rows of a push by table, as the log holds them, and the references
        to outputs in them.

        Does not check whether a row repeats one that its table holds. The
        workspace checks this with :func:`distinct`, since only it knows the
        table.
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

    def state_problem(self, plain: Request, blanks: Collection[str]) -> str | None:
        """
        Why the plain request of an accumulator's state would be rejected, or
        ``None``.

        Checks only the number of rows of each table in ``blanks``. Each value
        was checked when it was given, and the params model has no validators
        of its own, which :class:`~ess.spec.WorkflowSpec` rejects.
        """
        try:
            _lengths(self.spec, plain.params, blanks)
        except SubmitError as error:
            return str(error)
        return None

    def _values(
        self, values: Mapping[str, Any], proposal: str
    ) -> tuple[dict[str, Any], OutputReads]:
        """``values`` as the record holds them, and the references in them."""
        stored, reads = {}, []
        for name, value in values.items():
            stored[name], found = self._value(name, value, proposal)
            reads.extend(found)
        return stored, reads

    def _value(self, name: str, value: Any, proposal: str) -> tuple[Any, OutputReads]:
        """The value of ``name`` as the record holds it, and the references in it."""
        try:
            return validation.validate_request_value(
                self.spec.params,
                name,
                value,
                lambda file, marker: self._file(file, marker, proposal),
                lambda ref: self._path(name, ref),
                table=name in self.spec.tables,
            )
        except ParamsError as error:
            raise SubmitError.of(error) from None

    def _path(self, name: str, ref: FileRef) -> Path:
        """
        The local path of ``ref``, a file of parameter ``name``.

        ``OSError`` from the file source rejects the file, as in :meth:`_file`.
        """
        try:
            return self._files.path(ref)
        except OSError as error:
            raise SubmitError.at(
                (name,), f'file {ref.file} cannot be read: {error}'
            ) from None

    def _file(self, name: str, marker: File, proposal: str) -> str:
        """
        The identity of the file ``name``.

        Raises ``ValueError`` unless the file is known, readable by
        ``proposal``, and fits ``marker``. ``OSError`` from the file source,
        such as when a mount is gone, rejects the file as unknown. Then the
        value cannot be checked, and only the request that names the file is
        rejected. Anything else the file source raises is a defect and
        propagates.
        """
        ref = FileRef(file=name)
        try:
            identity = self._files.resolve(ref)
            metadata = self._files.metadata(identity)
        except KeyError:
            raise ValueError(f'unknown file {ref}') from None
        except OSError as error:
            raise ValueError(f'file {ref} cannot be read: {error}') from None
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
    Raises ``SubmitError`` if ``spec`` lacks the referenced output, or if its
    data do not fit ``tp``.

    A reference with a key refers to one element of an output, of the output's
    element type. The key of an element of a list is an integer in canonical
    form, such as ``'12'`` but not ``'012'``.
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
    with its index.

    Raises ``SubmitError`` for a row equal to a seen or an earlier one. Rows
    are compared by JSON form, so a reference compares by its fields, such as
    a file's identity.
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
    """``values``, with the default of each parameter in neither it nor ``blanks``."""
    try:
        return fixed_values(spec, values, blanks)
    except ParamsError as error:
        raise SubmitError.of(error) from None


def _rows(spec: Spec, values: Mapping[str, Any], tables: Collection[str]) -> None:
    """Raises ``SubmitError`` for a table of a wrong length or with a repeated row."""
    _lengths(spec, values, tables)
    for table in tables:
        distinct(table, values.get(table) or [], {})


def _lengths(spec: Spec, values: Mapping[str, Any], tables: Iterable[str]) -> None:
    """Raises ``SubmitError`` for a table whose length breaks its constraints."""
    for table in tables:
        n = len(values.get(table) or [])
        if need := _length_problem(spec, table, n):
            raise SubmitError.at(
                (table,), f'the table has {_rows_text(n)}; it needs {need}'
            )


def _length_problem(spec: Spec, name: str, n: int) -> str | None:
    """The constraint that ``n`` rows of ``name`` break, such as ``'at least 1'``."""
    for limit in spec.params.model_fields[name].metadata:
        if isinstance(limit, MinLen) and n < limit.min_length:
            return f'at least {limit.min_length}'
        if isinstance(limit, MaxLen) and n > limit.max_length:
            return f'at most {limit.max_length}'
    return None


def _rows_text(n: int) -> str:
    return {0: 'no row', 1: 'one row'}.get(n, f'{n} rows')

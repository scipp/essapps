# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: what the backend calls to compute a spec's requests.

A binding is what the backend calls to compute a spec's requests. The
workflow author makes it from a pipeline, a driver, or a function, each for
one spec::

    single = PipelineBinding(IOFQ, pipeline, params=..., outputs=...)
    multi = DriverBinding(IOFQ_MULTI, make_driver)
    export = function_binding(EXPORT, export_csv)

``PipelineBinding`` is in :mod:`ess.spec.pipeline`, which needs sciline. Each
binding serves some of a client's actions:

=====================  =============  ==============  ==================
binding                plain request  stage           accumulator
=====================  =============  ==============  ==================
``PipelineBinding``    ``compute``    ``make_stage``  refused
``DriverBinding``      ``compute``    refused         ``make_accumulator``
``function_binding``   ``compute``    refused         refused
=====================  =============  ==============  ==================

The protocols name what the backend calls: :class:`Binding` for a plain
request, :class:`StageBinding` for a stage, and :class:`AccumulatorBinding`
for an accumulator. Values pass by parameter name, with data read: a group as
a dict of its fields, and a table as a list of such dicts, its rows. Outputs
come back by name.

A *driver* (:class:`Driver`) is the object behind an accumulator: rows are
pushed into it, one table at a time, and it gives the outputs over the rows so
far. The workflow package writes it; only a driver accumulates.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import TypeAdapter

from ._workflow_spec import WorkflowSpec
from .data import table_fields

StageCall = Callable[[Mapping[str, Any]], Mapping[str, Any]]
"""What ``make_stage`` returns: the outputs from the values of the blanks."""


@runtime_checkable
class Binding(Protocol):
    @property
    def spec(self) -> WorkflowSpec[Any]:
        """The spec whose requests the binding computes."""
        ...

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        The outputs of a plain request, by name.

        ``params`` holds each parameter's value by name, with data read. A
        parameter left out takes the spec's default. An output that the spec
        declares optional may be left out.

        It may be called from several threads at once, and does not modify
        ``params``.
        """
        ...


@runtime_checkable
class StageBinding(Binding, Protocol):
    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall:
        """
        A callable that computes the outputs from the values of ``blanks``,
        by name, with the values of ``fixed``.

        The callable gets the values of the blanks only, and gives what
        :meth:`compute` gives with the same values. It may compute what
        depends only on ``fixed`` once, and hold it. A parameter in neither
        ``fixed`` nor ``blanks`` takes the spec's default. An implementation
        raises ``ValueError`` for ``fixed`` and ``blanks`` that do not fit
        the spec.

        This method and the callable may be called from several threads at
        once, and neither modifies the values it is given. The callable may
        return the same object in several calls, so records made through one
        stage share what does not depend on the blanks.
        """
        ...


class Driver(Protocol):
    """
    The object behind an accumulator: rows are pushed into it, and it gives
    the outputs over the rows so far.

    The caller calls :meth:`push` and :meth:`outputs` from one thread at a
    time, and pushes only once it is done with the outputs of the state
    before. A driver promises:

    - :meth:`outputs` gives the outputs of every row pushed so far, however
      the rows were grouped into pushes, and whatever the order of pushes
      into different tables.
    - Within a table, the rows keep the order in which they were pushed.
    - :meth:`push` does not modify a row. A driver that adds in place copies
      the first contribution, so that adding never changes a value that
      another record returned.
    - :meth:`outputs` may share memory with what the driver holds; the
      caller reads them before the next push.
    - An output that the spec declares optional may be left out.
    - A driver may refuse a state, such as one with no can row yet, by
      raising in :meth:`outputs`; later pushes may make the state valid.

    A push that raises stops the accumulator, since the driver may hold part
    of its rows.
    """

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Add rows of the table named, in order; ``rows`` is never empty."""
        ...

    def outputs(self) -> Mapping[str, Any]:
        """The outputs over the rows pushed so far, by name."""
        ...


@runtime_checkable
class AccumulatorBinding(Binding, Protocol):
    def make_accumulator(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Driver:
        """
        A driver that takes pushes into the tables ``blanks``, made with the
        values of ``fixed``, with the rows of any table in ``fixed`` in it.

        A parameter in neither ``fixed`` nor ``blanks`` takes the spec's
        default. An implementation raises ``ValueError`` for ``fixed`` and
        ``blanks`` that do not fit the spec, and for a blank that is not a
        table.

        It may be called from several threads at once, and does not modify
        the values it is given.
        """
        ...


def fixed_values(
    spec: WorkflowSpec[Any], fixed: Mapping[str, Any], blanks: Collection[str]
) -> dict[str, Any]:
    """
    The values of ``fixed``, with the default of each parameter in neither
    ``fixed`` nor ``blanks``.

    A default comes as the backend passes values: a group as a dict of its
    fields. Raises ``ValueError`` if a parameter is both fixed and blank, if
    either names a parameter the spec lacks, or if a parameter without a
    default is in neither.
    """
    fields = spec.params.model_fields
    if both := fixed.keys() & set(blanks):
        raise ValueError(f'{sorted(both)}: both fixed and blank')
    if unknown := (fixed.keys() | set(blanks)) - fields.keys():
        raise ValueError(f'{sorted(unknown)}: not parameters of {spec.name}')
    left = {n: f for n, f in fields.items() if n not in fixed and n not in blanks}
    if missing := [n for n, f in left.items() if f.is_required()]:
        raise ValueError(f'{missing}: neither fixed nor blank, and without a default')
    defaults = {
        name: TypeAdapter(field.annotation).dump_python(
            field.get_default(call_default_factory=True, validated_data={})
        )
        for name, field in left.items()
    }
    return {**defaults, **fixed}


@dataclass(frozen=True)
class DriverBinding:
    """
    A driver behind a spec over tables: plain requests and accumulators.

    ``make_driver`` makes a driver from the fixed values that are not tables,
    by name. It may be called from several threads at once. An accumulator
    gets a driver of its own, with the rows of each table in its fixed values
    in one push; a table without rows, or ``None``, is not pushed. A plain
    request is an accumulator with no blanks, read once. ``fixed`` and
    ``blanks`` are checked by :func:`fixed_values`, and each blank must be a
    table.
    """

    spec: WorkflowSpec[Any]
    make_driver: Callable[[Mapping[str, Any]], Driver]

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.make_accumulator(params, ()).outputs()

    def make_accumulator(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Driver:
        tables = table_fields(self.spec.params)
        if not_tables := sorted(set(blanks) - tables.keys()):
            raise ValueError(
                f'{not_tables}: the blanks of an accumulator are tables of '
                f'{self.spec.name}, {sorted(tables)}'
            )
        values = fixed_values(self.spec, fixed, blanks)
        driver = self.make_driver({n: v for n, v in values.items() if n not in tables})
        for table in tables:
            if rows := values.get(table):
                driver.push(table, rows)
        return driver


@dataclass(frozen=True)
class _FunctionBinding:
    spec: WorkflowSpec[Any]
    function: Callable[..., Mapping[str, Any]]

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.function(**fixed_values(self.spec, params, ()))


def function_binding(
    spec: WorkflowSpec[Any], function: Callable[..., Mapping[str, Any]]
) -> Binding:
    """
    A function behind a spec: plain requests only.

    ``function`` takes each parameter by name, with data read and defaults
    filled in, and returns the outputs by name. It must be pure: the backend
    may call it from several threads at once, and it does not modify its
    arguments.
    """
    return _FunctionBinding(spec, function)

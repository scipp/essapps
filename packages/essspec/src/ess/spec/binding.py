# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: what the backend calls to compute a spec's requests.

``packages/essspec/docs/bindings.md`` is a guide for workflow authors.

The protocols name what the backend calls: :class:`Binding` for a plain
request, :class:`StageBinding` for a stage, and :class:`AccumulatorBinding`
for an accumulator, whose :class:`Driver` the workflow package writes. The
backend checks which protocol a binding implements when a stage or an
accumulator is made, and refuses one that the binding does not serve.

This module holds the bindings that need no sciline: :class:`DriverBinding`
and :func:`function_binding`. ``PipelineBinding`` is in
:mod:`ess.spec.pipeline`, the only module that imports sciline.
``DriverBinding`` computes a plain request as an accumulator with no blanks,
read once, and ``PipelineBinding`` as a stage with no blanks, called once, so
that a plain request and a stage or an accumulator go through the same code.
Both check ``fixed`` and ``blanks``, and fill in defaults, with
:func:`fixed_values`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ._workflow_spec import WorkflowSpec
from .refs import ParamsError

type StageCall[O] = Callable[[Mapping[str, Any]], O]
"""What ``make_stage`` returns: the outputs from the values of the blanks."""


@runtime_checkable
class Binding[P: BaseModel, O: BaseModel](Protocol):
    """
    What the backend calls to compute a plain request.

    Every binding is one, whatever else it serves. See
    ``packages/essspec/docs/bindings.md``.
    """

    @property
    def spec(self) -> WorkflowSpec[P, O]:
        """The spec whose requests the binding computes."""
        ...

    def compute(self, params: P) -> O:
        """The outputs of a plain request, called once per request."""
        ...


@runtime_checkable
class StageBinding[P: BaseModel, O: BaseModel](Binding[P, O], Protocol):
    """
    A binding that serves stages.

    The backend calls :meth:`make_stage` at a stage's first call, and the
    callable it returns once per call through the stage. See
    ``packages/essspec/docs/bindings.md``.
    """

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[O]:
        """
        A callable that computes the outputs from the values of ``blanks``,
        by name, with the values of ``fixed``.

        A parameter in neither ``fixed`` nor ``blanks`` takes the spec's
        default. The callable takes a value of each blank, a table as its
        rows, and gives what :meth:`compute` gives with the same values.

        Raises
        ------
        ValueError
            If ``fixed`` and ``blanks`` do not fit the spec.
        """
        ...


class Driver[O: BaseModel](Protocol):
    """
    The object behind an accumulator: tables are pushed into it, and it gives
    the outputs over the rows so far.

    The workflow package writes it. What a driver and its caller promise is in
    ``packages/essspec/docs/bindings.md``.
    """

    def push(self, table: str, rows: Sequence[Any]) -> None:
        """Add ``rows``, at least one, to the table ``table``, in order."""
        ...

    def outputs(self) -> O:
        """The outputs over the rows pushed so far."""
        ...


@runtime_checkable
class AccumulatorBinding[P: BaseModel, O: BaseModel](Binding[P, O], Protocol):
    """
    A binding that serves accumulators.

    The backend calls :meth:`make_accumulator` when an accumulator opens, then
    :meth:`Driver.push` once per table of a push, with its rows, and
    :meth:`Driver.outputs` once per state that is read. See
    ``packages/essspec/docs/bindings.md``.
    """

    def make_accumulator(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Driver[O]:
        """
        A driver that takes pushes into the tables ``blanks``, made with the
        values of ``fixed``, with the rows of any table in ``fixed`` in it.

        A parameter in neither ``fixed`` nor ``blanks`` takes the spec's
        default.

        Raises
        ------
        ValueError
            If ``fixed`` and ``blanks`` do not fit the spec, or a blank is not
            a table.
        """
        ...


def fixed_values(
    spec: WorkflowSpec[Any, Any],
    fixed: Mapping[str, Any],
    blanks: Collection[str],
) -> dict[str, Any]:
    """
    The values of ``fixed``, by name, with the default of each parameter in
    neither ``fixed`` nor ``blanks``.

    Raises :class:`~ess.spec.refs.ParamsError`, with a reason for each name,
    if a name is both fixed and blank, if either names no parameter of the
    spec, or if one without a default is in neither.
    """
    fields = spec.params.model_fields
    unknown = sorted((fixed.keys() | set(blanks)) - fields.keys())
    both = sorted(fixed.keys() & set(blanks))
    left = {n: f for n, f in fields.items() if n not in fixed and n not in blanks}
    missing = [n for n, f in left.items() if f.is_required()]
    if unknown or both or missing:
        raise ParamsError(
            [((n,), f'not a parameter of {spec.name}') for n in unknown]
            + [((n,), 'both fixed and blank') for n in both]
            + [((n,), 'a required parameter, not given') for n in missing]
        )
    defaults = {
        name: info.get_default(call_default_factory=True, validated_data={})
        for name, info in left.items()
    }
    return {**defaults, **fixed}


@dataclass(frozen=True)
class DriverBinding[P: BaseModel, O: BaseModel]:
    """
    A driver behind a spec over tables: plain requests and accumulators.

    An accumulator gets a driver of its own, into which the rows of each table
    in its fixed values are pushed first, one push per table. See
    ``packages/essspec/docs/bindings.md``.

    Parameters
    ----------
    spec:
        The spec whose requests the binding computes.
    make_driver:
        Makes a driver from the fixed values that are not tables, by name.
    """

    spec: WorkflowSpec[P, O]
    make_driver: Callable[[Mapping[str, Any]], Driver[O]]

    def compute(self, params: P) -> O:
        return self.make_accumulator(dict(params), ()).outputs()

    def make_accumulator(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Driver[O]:
        tables = self.spec.tables
        if not_tables := sorted(set(blanks) - set(tables)):
            raise ValueError(
                f'{not_tables}: the blanks of an accumulator are tables of '
                f'{self.spec.name}, {list(tables)}'
            )
        values = fixed_values(self.spec, fixed, blanks)
        driver = self.make_driver({n: v for n, v in values.items() if n not in tables})
        for table in tables:
            if values.get(table):
                driver.push(table, values[table])
        return driver


@dataclass(frozen=True)
class _FunctionBinding[P: BaseModel, O: BaseModel]:
    spec: WorkflowSpec[P, O]
    function: Callable[[P], O]

    def compute(self, params: P) -> O:
        return self.function(params)


def function_binding[P: BaseModel, O: BaseModel](
    spec: WorkflowSpec[P, O], function: Callable[[P], O]
) -> Binding[P, O]:
    """
    A function behind a spec: plain requests only.

    See ``packages/essspec/docs/bindings.md``.

    Parameters
    ----------
    spec:
        The spec whose requests the binding computes.
    function:
        Takes an instance of the spec's params model and returns an instance
        of its outputs model.
    """
    return _FunctionBinding(spec, function)

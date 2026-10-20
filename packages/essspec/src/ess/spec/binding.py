# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: what the backend calls to compute a spec's requests.

The workflow author makes a binding from a pipeline, a driver, or a function,
each for one spec::

    single = PipelineBinding(IOFQ, make_pipeline)
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
for an accumulator. A plain request passes an instance of the params model,
``compute(params) -> outputs``. A stage or an accumulator is made from some
values only, so these pass by name, each as its type, whether given inline
or by reference: a group as the model, a file as a local path, any other value
as the object, and a table as the list of its rows::

    {'reduction': Reduction(bins=2), 'beam_centre': centre,
     'sample': [Sample(run=path_611), Sample(run=path_612, transmission=t)]}

Blanks name parameters, a table among them. An output that the spec declares
optional is left out by being ``None``.

A *driver* (:class:`Driver`) is the object behind an accumulator: tables are
pushed into it, and it gives the outputs over the rows so far. The workflow
package writes it; only a driver accumulates.
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
    @property
    def spec(self) -> WorkflowSpec[P, O]:
        """The spec whose requests the binding computes."""
        ...

    def compute(self, params: P) -> O:
        """
        The outputs of a plain request.

        It may be called from several threads at once, and does not modify
        ``params``. ``params`` is made from values each validated by its own
        field, and checked by the params model's model validators.
        """
        ...


@runtime_checkable
class StageBinding[P: BaseModel, O: BaseModel](Binding[P, O], Protocol):
    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[O]:
        """
        A callable that computes the outputs from the values of ``blanks``,
        by name, with the values of ``fixed``.

        The callable takes a value of each blank, a table as its rows.

        The callable gets the values of the blanks only, and gives what
        :meth:`compute` gives with the same values. It may compute what
        depends only on ``fixed`` once, and hold it. A parameter in neither
        ``fixed`` nor ``blanks`` takes the spec's default. An
        implementation raises ``ValueError`` for ``fixed`` and ``blanks``
        that do not fit the spec.

        This method and the callable may be called from several threads at
        once, and neither modifies the values it is given. The callable may
        return the same object in several calls, so records made through one
        stage share what does not depend on the blanks.
        """
        ...


class PushRefused(ValueError):
    """
    Raised by :meth:`Driver.push` for rows that the driver refuses before it
    adds any of them: the driver is as it was before the push.
    """


class Driver[O: BaseModel](Protocol):
    """
    The object behind an accumulator: tables are pushed into it, and it gives
    the outputs over the rows so far.

    The caller calls :meth:`push` and :meth:`outputs` from one thread at a
    time, and pushes only once it is done with the outputs of the state
    before. A driver promises:

    - :meth:`outputs` gives the outputs of every row pushed so far, however
      the rows were grouped into pushes, and whatever the order of pushes
      into different tables.
    - Within a table, the rows keep the order in which they were pushed.
    - :meth:`push` does not modify a value it is given. A driver that adds in
      place copies the first contribution, so that adding never changes a
      value that another record returned.
    - :meth:`outputs` may share memory with what the driver holds; the
      caller reads them before the next push.
    - An output that the spec declares optional may be ``None``.
    - A driver may refuse a state, such as one whose runs it cannot
      combine yet, by raising in :meth:`outputs`; later pushes may make the
      state valid.
    - A check across rows is the driver's: the params model's model
      validators run when the accumulator opens, without the blank tables.
    - A push that raises :class:`PushRefused` changed nothing: the driver
      gives the outputs it gave before the push, and later pushes add as if
      it had not been made. A driver keeps this by computing what every row
      contributes first, and adding it only once all of it is computed.

    A push that raises :class:`PushRefused` fails alone: the accumulator
    takes more pushes. A push that raises anything else stops the
    accumulator, since the driver may hold part of its rows. A client's push
    into several tables is one call per table, so a refusal at a later table
    stops the accumulator too: the driver holds the rows of those before.
    """

    def push(self, table: str, rows: Sequence[Any]) -> None:
        """Add ``rows``, never none, to the table ``table``, in order."""
        ...

    def outputs(self) -> O:
        """The outputs over the rows pushed so far."""
        ...


@runtime_checkable
class AccumulatorBinding[P: BaseModel, O: BaseModel](Binding[P, O], Protocol):
    def make_accumulator(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Driver[O]:
        """
        A driver that takes pushes into the tables ``blanks``, made with the
        values of ``fixed``, with the rows of any table in ``fixed`` in it.

        A parameter in neither ``fixed`` nor ``blanks`` takes the spec's
        default. An implementation raises ``ValueError`` for ``fixed``
        and ``blanks`` that do not fit the spec, and for a blank that is not
        a table.

        It may be called from several threads at once, and does not modify
        the values it is given.
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

    ``make_driver`` makes a driver from the fixed values that are not tables,
    by name. It may be called from several threads at once. An accumulator
    gets a driver of its own, with each table in its fixed values in one
    push; a table without rows is not pushed. A plain request is an
    accumulator with no blanks, read once. ``fixed`` and ``blanks`` are
    checked by :func:`fixed_values`, and each blank must be a table.
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

    ``function`` takes an instance of the spec's params model and returns an
    instance of its outputs model. It must be pure: the backend may call it
    from several threads at once, and it does not modify its argument.
    """
    return _FunctionBinding(spec, function)

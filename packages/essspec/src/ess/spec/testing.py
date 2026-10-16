# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Checks of the symmetries that a workflow author promises, for a package's tests.

Each check computes the same outputs in two ways and compares them, so it
needs no known result. With a single-run binding ``single`` (a
``PipelineBinding``) of ``IOFQ``, a multi-run binding ``multi`` (a
``DriverBinding``) of ``IOFQ_MULTI = IOFQ.tabulate(...)``, and the function
``make_driver`` that ``multi`` makes its drivers with::

    params = IofQParams(reduction=Reduction(bins=2), sample=run_101, can=run_110)
    check_one_row(single, multi, params)
    check_caching(single, {'sample': run_101}, [{'reduction': ...}, ...])
    check_arrival_and_order(
        make_driver,
        {'reduction': params.reduction},
        {'sample': {'sample': [run_101, run_102]}, 'can': {'can': [run_110]}},
    )

Values are those the binding receives: data read. Outputs are compared up to
``rtol``, since adding in another order rounds differently, with
:func:`assert_close`. :func:`check_one_row` and :func:`check_caching` also
check that no output shares memory with a value the workflow was given. A
failed check raises ``AssertionError`` naming the symmetry and the call. The
symmetries are those of docs/developer/README.md (Symmetries).
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from ._workflow_spec import WorkflowSpec
from .binding import Binding, Driver, StageBinding, fixed_values
from .field_types import model_of

Columns = Mapping[str, Sequence[Any]]
"""A table's columns by name, the index among them, as a push gives them."""


def check_one_row(
    single: Binding[Any, Any],
    multi: Binding[Any, Any],
    params: BaseModel,
    *,
    rtol: float = 1e-12,
) -> None:
    """
    One-row: the binding of a multi-run spec, with one row in each table
    whose index is given and none where it is left out, gives the outputs of
    the binding of the single-run spec that ``tabulate`` made it from.

    ``params`` is a request of ``single``, an instance of its spec's params
    model; the request of ``multi`` is made from it.
    """
    multi_params = one_row(multi.spec, params)
    expected = single.compute(params)
    actual = multi.compute(multi_params)
    _check_no_shared_memory(expected, params, where='one-row, single-run')
    _check_no_shared_memory(actual, multi_params, where='one-row, multi-run')
    assert_close(actual, expected, rtol=rtol, where='one-row')


def one_row(spec: WorkflowSpec[Any, Any], params: BaseModel) -> BaseModel:
    """
    The request of the multi-run ``spec`` with one row in each table whose
    index is given in the request ``params`` of the single-run spec, and none
    where it is ``None``.
    """
    values = dict(params)
    for index in spec.tables:
        rows = 0 if values[index] is None else 1
        for name in spec.columns(index):
            values[name] = [values[name]] * rows
    return instance(spec, values)


def instance(spec: WorkflowSpec[Any, Any], values: Mapping[str, Any]) -> BaseModel:
    """
    An instance of the spec's params model from ``values`` by name, as a
    stage or a driver takes them, made as the backend makes one: each value
    validated by its own field, without the model validators.
    """
    model: type[BaseModel] = spec.params
    return model.model_construct(
        **{
            n: getattr(model_of(model, n).model_validate({n: v}), n)
            for n, v in values.items()
        }
    )


def check_caching(
    binding: StageBinding[Any, Any],
    fixed: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Caching: each call through one stage gives the outputs of the plain
    request with the same values.

    The stage is made with ``fixed`` and the blanks the names of the first
    call leave open, a table's columns by its index, and called in the order
    given. Values are by name, as ``make_stage`` takes them. Repeat a call
    after others, as in bins 50, 100, and 50 again, to find a stage whose
    calls change what it holds.
    """
    spec = binding.spec
    blanks = tuple(n for n in calls[0] if spec.table_of(n) in (None, n))
    call = binding.make_stage(fixed, blanks)
    for n, values in enumerate(calls, start=1):
        given = fixed_values(spec, {**fixed, **values}, ())
        expected = binding.compute(instance(spec, given))
        actual = call(values)
        where = f'caching, call {n} {values}'
        _check_no_shared_memory(expected, given, where=f'{where}, plain')
        _check_no_shared_memory(actual, given, where=f'{where}, staged')
        assert_close(actual, expected, rtol=rtol, where=where)


def check_arrival_and_order(
    make_driver: Callable[[Mapping[str, Any]], Driver[Any]],
    fixed: Mapping[str, Any],
    tables: Mapping[str, Columns],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Arrival and order: after each push, a driver gives the outputs that one
    push of each table's rows so far gives.

    ``make_driver`` makes a driver from ``fixed``, as for a
    ``DriverBinding``, so a package tests its driver without a spec.
    ``fixed`` holds every value that is not a column of a table, defaults
    included: this function does not fill them in. ``tables`` holds the
    columns of each table by its index. The rows are pushed into a new
    driver for each of several schedules, each table's rows in their order:
    one push per table, the tables reversed, one row per push table by
    table, and one row per push alternating between the tables. Every
    schedule ends with the same rows, so arrival in all of them is order too.

    The expected outputs come from a new driver with one push per table that
    has rows so far. A state whose expected outputs raise is not read, since
    a driver may refuse a state; the state with every row is always read.
    The check fails if a push modifies the values it was given.

    The check compares the driver with itself under other push schedules, so
    it does not prove that the outputs are right. Only a comparison with an
    independent reference does, such as :func:`check_one_row` against the
    single-run binding, if the package writes one.
    """
    pristine = copy.deepcopy(tables)
    for name, schedule in _schedules(tables):
        driver = make_driver(fixed)
        pushed: dict[str, int] = dict.fromkeys(tables, 0)
        for n, (table, start, stop) in enumerate(schedule, start=1):
            driver.push(table, _rows(tables[table], start, stop))
            pushed[table] = stop
            try:
                expected = _one_push_per_table(
                    make_driver,
                    fixed,
                    {t: _rows(pristine[t], 0, upto) for t, upto in pushed.items()},
                )
            except Exception:
                if n == len(schedule):
                    raise
                continue
            where = f'arrival, {name}, state {n} of pushes {schedule[:n]}'
            assert_close(driver.outputs(), expected, rtol=rtol, where=where)
        assert_close(
            tables, pristine, rtol=0.0, where=f'values after pushing them, {name}'
        )


def _rows(columns: Columns, start: int, stop: int) -> dict[str, list[Any]]:
    """Rows ``start`` to ``stop`` of a table, as its columns."""
    return {name: list(column[start:stop]) for name, column in columns.items()}


def _length(columns: Columns) -> int:
    return len(next(iter(columns.values())))


def _one_push_per_table(
    make_driver: Callable[[Mapping[str, Any]], Driver[Any]],
    fixed: Mapping[str, Any],
    tables: Mapping[str, Columns],
) -> Any:
    driver = make_driver(fixed)
    for table, columns in tables.items():
        if _length(columns):
            driver.push(table, copy.deepcopy(dict(columns)))
    return driver.outputs()


def _schedules(
    tables: Mapping[str, Columns],
) -> list[tuple[str, list[tuple[str, int, int]]]]:
    """
    Named push schedules over the rows of ``tables``, each push as its table
    and the slice of its rows; schedules that push the same are listed once.
    """
    lengths = {t: _length(columns) for t, columns in tables.items()}
    names = [t for t in tables if lengths[t]]
    longest = max(lengths.values(), default=0)

    def whole(order: Sequence[str]) -> list[tuple[str, int, int]]:
        return [(t, 0, lengths[t]) for t in order]

    candidates = {
        'one push per table': whole(names),
        'one push per table, reversed': whole(names[::-1]),
        'one row per push, table by table': [
            (t, i, i + 1) for t in names for i in range(lengths[t])
        ],
        'one row per push, alternating tables': [
            (t, i, i + 1) for i in range(longest) for t in names if i < lengths[t]
        ],
    }
    unique: dict[str, tuple[str, list[tuple[str, int, int]]]] = {}
    for name, schedule in candidates.items():
        unique.setdefault(repr(schedule), (name, schedule))
    return list(unique.values())


def _check_no_shared_memory(outputs: Any, values: Any, *, where: str) -> None:
    """
    Raise ``AssertionError`` if an output shares memory with one of ``values``.

    A workflow returns no output that shares memory with a value it reads,
    such as a slice that is a view of it: the framework passes the values it
    keeps, so changing one would change the other. Only arrays that numpy
    holds are compared: numpy arrays, and the data, variances, coords, and
    masks of scipp objects.
    """
    given = [array for _, array in _arrays(values, '')]
    if not given:
        return
    import numpy as np

    for at, array in _arrays(outputs, ''):
        if any(np.shares_memory(array, g) for g in given):
            raise AssertionError(f'{where}: output {at} shares memory with a value')


def _arrays(value: Any, at: str) -> Iterator[tuple[str, Any]]:
    """The numpy arrays that hold ``value``, recursively, each with its place."""
    if isinstance(value, BaseModel):
        yield from _arrays(dict(value), at)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _arrays(item, f'{at}[{key!r}]')
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            yield from _arrays(item, f'{at}[{i}]')
    elif type(value).__module__.partition('.')[0] == 'scipp':
        yield from _scipp_arrays(value, at)
    elif type(value).__module__ == 'numpy':
        import numpy as np

        if isinstance(value, np.ndarray):
            yield at, value


def _scipp_arrays(value: Any, at: str) -> Iterator[tuple[str, Any]]:
    import scipp as sc

    if isinstance(value, sc.Variable):
        if value.bins is not None:
            yield from _arrays(value.bins.constituents['data'], f'{at}.bins')
            return
        yield from _arrays(value.values, f'{at}.values')
        yield from _arrays(value.variances, f'{at}.variances')
    elif isinstance(value, sc.DataArray):
        yield from _arrays(value.data, f'{at}.data')
        yield from _arrays(dict(value.coords), f'{at}.coords')
        yield from _arrays(dict(value.masks), f'{at}.masks')
    elif isinstance(value, sc.Dataset):
        yield from _arrays(dict(value.items()), at)


def assert_close(
    actual: Any, expected: Any, *, rtol: float = 1e-12, where: str = ''
) -> None:
    """
    Raise ``AssertionError`` unless the values agree to ``rtol``, recursively.

    Mappings, lists, and tuples are compared element by element, scipp objects
    and arrays up to ``rtol``, floats too, and other values by equality. The
    message starts with ``where``, followed by the keys and indices of the
    value that differs. Models are compared field by field, and must be of
    one type.
    """
    if isinstance(expected, BaseModel):
        if type(actual) is not type(expected):
            raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
        assert_close(dict(actual), dict(expected), rtol=rtol, where=where)
    elif isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or actual.keys() != expected.keys():
            got = sorted(actual) if isinstance(actual, Mapping) else actual
            raise AssertionError(f'{where}: {got!r}, expected {sorted(expected)!r}')
        for key in expected:
            assert_close(
                actual[key], expected[key], rtol=rtol, where=f'{where}, {key!r}'
            )
    elif isinstance(expected, list | tuple):
        if not isinstance(actual, list | tuple) or len(actual) != len(expected):
            raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            assert_close(a, e, rtol=rtol, where=f'{where}[{i}]')
    elif type(expected).__module__.partition('.')[0] == 'scipp':
        import scipp as sc
        import scipp.testing

        try:
            scipp.testing.assert_allclose(actual, expected, rtol=sc.scalar(rtol))
        except Exception as error:  # a value of another type raises other errors
            raise AssertionError(f'{where}: {error}') from None
    elif hasattr(expected, '__array__'):
        import numpy as np

        try:
            np.testing.assert_allclose(actual, expected, rtol=rtol)
        except Exception as error:
            raise AssertionError(f'{where}: {error}') from None
    elif isinstance(expected, float):
        if not (
            isinstance(actual, int | float)
            and math.isclose(actual, expected, rel_tol=rtol)
        ):
            raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
    elif actual != expected:
        raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')

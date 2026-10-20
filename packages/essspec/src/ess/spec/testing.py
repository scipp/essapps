# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Checks of the symmetries that a workflow author promises, for a package's tests.

Each check computes the same outputs in two ways and compares them, so it
needs no known result. With a single-run binding ``single`` (a
``PipelineBinding``) of ``IOFQ``, a multi-run binding ``multi`` (a
``DriverBinding``) of ``IOFQ_MULTI``, whose tables ``sample`` and ``can`` are
lists of the types of those parameters of ``IOFQ``, and the function
``make_driver`` that ``multi`` makes its drivers with::

    params = IofQParams(reduction=Reduction(bins=2), sample=run_101, can=run_110)
    check_one_row(single, multi, params)
    check_caching(single, {'sample': run_101}, [{'reduction': ...}, ...])
    check_arrival_and_order(
        make_driver,
        {'reduction': params.reduction},
        {'sample': [run_101, run_102], 'can': [run_110]},
    )
    check_refused_push(
        make_driver,
        {'reduction': params.reduction},
        {'sample': [run_101, run_102], 'can': [run_110]},
        {'sample': [run_103], 'can': [broken_run]},
    )

:func:`check_refused_push` checks a promise rather than a symmetry: a push
that raises ``PushRefused`` changes nothing.

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
from .binding import Binding, Driver, PushRefused, StageBinding, fixed_values
from .field_types import check_model, validated

Rows = Sequence[Any]
"""A table's rows, as a push gives them."""


def check_one_row(
    single: Binding[Any, Any],
    multi: Binding[Any, Any],
    params: BaseModel,
    *,
    rtol: float = 1e-12,
) -> None:
    """
    One-row: the binding of a multi-run spec, with one row in each table,
    gives the outputs of the binding of a single-run spec. The two specs
    agree on the parameters that are not tables, and each table is a list of
    the type of the parameter of the same name of the single-run spec.

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
    The request of the multi-run ``spec`` with one row in each table: the
    value of the request ``params`` of the single-run spec.
    """
    values = dict(params)
    for table in spec.tables:
        values[table] = [values[table]]
    return instance(spec, values)


def instance(spec: WorkflowSpec[Any, Any], values: Mapping[str, Any]) -> BaseModel:
    """
    An instance of the spec's params model from ``values`` by name, as a
    stage or a driver takes them, made as the backend makes one from data it
    read: each value, or each row of a list, validated from the object by its
    own field, without the length constraints of a list, which the backend
    checks when a request is submitted, and then checked by the model
    validators (see :func:`ess.spec.field_types.check_model`).
    """
    model: type[BaseModel] = spec.params
    made = model.model_construct(
        **{n: validated(model, n, v) for n, v in values.items()}
    )
    check_model(model, made)
    return made


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

    The stage is made with ``fixed`` and the names of the first call as its
    blanks, and called in the order given. Values are by name, as
    ``make_stage`` takes them. Repeat a call after others, as in bins 50,
    100, and 50 again, to find a stage whose calls change what it holds.
    """
    spec = binding.spec
    blanks = tuple(calls[0])
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
    tables: Mapping[str, Rows],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Arrival and order: after each push, a driver gives the outputs that one
    push of each table's rows so far gives.

    ``make_driver`` makes a driver from ``fixed``, as for a
    ``DriverBinding``, so a package tests its driver without a spec.
    ``fixed`` holds every value that is not a table, defaults
    included: this function does not fill them in. ``tables`` holds the rows
    of each table by its name. The rows are pushed into a new
    driver for each of several schedules, each table's rows in their order:
    one push per table, the tables reversed, one row per push table by
    table, one row per push alternating between the tables, and one row of
    each table per push. Every schedule ends with the same rows, so arrival
    in all of them is order too.

    The expected outputs come from a new driver with one push of the rows of
    every table so far. A state whose expected outputs raise is not read,
    since a driver may refuse a state; the state with every row is always
    read. The check fails if a push modifies the values it was given.

    The check compares the driver with itself under other push schedules, so
    it does not prove that the outputs are right. Only a comparison with an
    independent reference does, such as :func:`check_one_row` against the
    single-run binding, if the package writes one.
    """
    expected = _Expected(make_driver, fixed, tables)
    for name, schedule in _schedules(tables):
        driver = make_driver(fixed)
        pushed: dict[str, int] = dict.fromkeys(tables, 0)
        for n, push in enumerate(schedule, start=1):
            driver.push(_slices(tables, push))
            pushed.update({t: stop for t, (_, stop) in push.items()})
            outputs = expected(pushed, last=n == len(schedule))
            if outputs is not None:
                where = f'arrival, {name}, state {n} of pushes {schedule[:n]}'
                assert_close(driver.outputs(), outputs, rtol=rtol, where=where)
        expected.check_unmodified(f'values after pushing them, {name}')


def check_refused_push(
    make_driver: Callable[[Mapping[str, Any]], Driver[Any]],
    fixed: Mapping[str, Any],
    tables: Mapping[str, Rows],
    refused: Mapping[str, Rows],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Refusal: a push that raises ``PushRefused`` changes nothing.

    ``make_driver``, ``fixed``, and ``tables`` are as for
    :func:`check_arrival_and_order`. ``refused`` is one push that the driver
    refuses, its rows by table, such as a row it takes followed by one it
    refuses, so that a driver that adds the first before it refuses the
    second fails the check; it gives rows of at least one table. The rows of
    ``tables`` are pushed into one driver one row per push, table by table.
    Before each of these pushes, and after the last, ``refused`` is pushed,
    and must raise ``PushRefused``. The driver then gives the outputs that
    one push of the rows of every table so far gives, and so it does after
    the next push too. A state whose expected outputs raise is not read, as
    in :func:`check_arrival_and_order`; the state with every row is always
    read. The check fails if a push modifies the values it was given.
    """
    if not any(refused.values()):
        raise ValueError(
            'refused: give the rows of one push that the driver refuses, not none'
        )
    pristine_refused = copy.deepcopy(refused)
    expected = _Expected(make_driver, fixed, tables)
    schedule = [{t: (i, i + 1)} for t in tables for i in range(len(tables[t]))]
    driver = make_driver(fixed)
    pushed: dict[str, int] = dict.fromkeys(tables, 0)
    for n in range(len(schedule) + 1):
        where = f'refusal, {_shown(refused)} after {n} pushes'
        try:
            driver.push({t: list(rows) for t, rows in refused.items() if rows})
        except PushRefused:
            pass
        except Exception as error:
            raise AssertionError(
                f'{where}: raised {error!r}, not PushRefused'
            ) from error
        else:
            raise AssertionError(f'{where}: the push was not refused')
        outputs = expected(pushed, last=n == len(schedule))
        if outputs is not None:
            assert_close(driver.outputs(), outputs, rtol=rtol, where=where)
        if n < len(schedule):
            driver.push(_slices(tables, schedule[n]))
            pushed.update({t: stop for t, (_, stop) in schedule[n].items()})
    expected.check_unmodified('values after pushing them')
    assert_close(
        refused, pristine_refused, rtol=0.0, where='values after refusing them'
    )


_Push = dict[str, tuple[int, int]]
"""A push of a schedule: the slice of the rows of each table it gives, by name."""


def _slices(tables: Mapping[str, Rows], push: _Push) -> dict[str, list[Any]]:
    """The rows that ``push`` gives of ``tables``."""
    return {t: list(tables[t][start:stop]) for t, (start, stop) in push.items()}


def _shown(rows: Mapping[str, Rows]) -> dict[str, list[Any]]:
    """Rows by table, as a failed check shows them."""
    return {t: list(r) for t, r in rows.items()}


class _Expected:
    """
    The outputs of a new driver given one push of the first rows of each of
    ``tables``, by their number; computed once for each number of rows, from
    a copy of the rows as given.
    """

    def __init__(
        self,
        make_driver: Callable[[Mapping[str, Any]], Driver[Any]],
        fixed: Mapping[str, Any],
        tables: Mapping[str, Rows],
    ) -> None:
        self._make_driver = make_driver
        self._fixed = fixed
        self._tables = tables
        self._pristine = copy.deepcopy(tables)
        self._computed: dict[tuple[tuple[str, int], ...], Any] = {}

    def __call__(self, pushed: Mapping[str, int], *, last: bool) -> Any:
        """
        The outputs with the first ``pushed`` rows of each table, or ``None``
        if they raise, since a driver may refuse a state; they raise if
        ``last``, the state with every row, which is always read.
        """
        key = tuple(sorted(pushed.items()))
        if key not in self._computed:
            rows = {t: self._pristine[t][:n] for t, n in pushed.items() if n}
            driver = self._make_driver(self._fixed)
            try:
                if rows:
                    driver.push(copy.deepcopy(rows))
                self._computed[key] = driver.outputs()
            except Exception as error:
                self._computed[key] = error
        computed = self._computed[key]
        if isinstance(computed, Exception):
            if last:
                raise computed
            return None
        return computed

    def check_unmodified(self, where: str) -> None:
        """Fail if a push modified the rows of ``tables`` it was given."""
        assert_close(self._tables, self._pristine, rtol=0.0, where=where)


def _schedules(tables: Mapping[str, Rows]) -> list[tuple[str, list[_Push]]]:
    """
    Named push schedules over the rows of ``tables``; schedules that push the
    same are listed once.
    """
    lengths = {t: len(rows) for t, rows in tables.items()}
    names = [t for t in tables if lengths[t]]
    longest = max(lengths.values(), default=0)

    def whole(order: Sequence[str]) -> list[_Push]:
        return [{t: (0, lengths[t])} for t in order]

    candidates = {
        'one push per table': whole(names),
        'one push per table, reversed': whole(names[::-1]),
        'one row per push, table by table': [
            {t: (i, i + 1)} for t in names for i in range(lengths[t])
        ],
        'one row per push, alternating tables': [
            {t: (i, i + 1)} for i in range(longest) for t in names if i < lengths[t]
        ],
        'one row of each table per push': [
            {t: (i, i + 1) for t in names if i < lengths[t]} for i in range(longest)
        ],
    }
    unique: dict[str, tuple[str, list[_Push]]] = {}
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

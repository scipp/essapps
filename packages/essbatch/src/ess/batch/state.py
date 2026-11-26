# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Where each row of a batch stands, worked out from plain data.

Nothing here calls a client, so every rule of what is ready and what is left
to do is tested on values. :class:`~.reduction.Batch` reads
the records and gives them to these functions.

A row's *result* is the newest record under the batch's label with the row's
key as its member, of any spec (:func:`results`). The row's request now differs
from its result's at the parameters that :attr:`RowState.changed` names, found
with :func:`differs` on both as a record holds them. A result is *outdated*
(:attr:`RowState.outdated`) if it is of another spec than the one bound
(:attr:`RowState.spec_changed`), or if the request changed at any parameter
(:func:`row_states`).

The cells of a row are given here as its request takes them, each empty cell
given the setting, else its default, or ``None`` if its parameter takes
``None`` (:meth:`~.reduction.Batch.cells`). A column left out of them is empty, and a
problem of the row. A value refused when resolved is a problem of the row, not
a change: the row cannot run with it, and a change would offer it to
:func:`left_to_reduce`.

A change of settings applies to the rows not reduced yet. For one row, a
parameter counts as a setting if it is no column or the row's cell there is
empty, since the row then takes the setting of the batch. A completed result
whose request differs from the row's only at settings so counted is reduced
with *earlier settings* (:attr:`RowState.earlier`): it reads ``earlier``, and
:func:`left_to_reduce` leaves it until a person reduces it again. A row whose
own cell changed reads ``outdated``. Which cells are empty is read from the
batch table now, so a cell emptied after the row was reduced counts as a setting.

A step of Follow reduces the rows of :func:`not_reduced`: ready, and
without a result. A row with a result is left to a person, whether it is
outdated, failed or cancelled.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from ess.apps.errors import FieldError
from ess.apps.records import Record, Status
from ess.spec import OutputRef
from ess.spec.refs import iter_refs

_RERUN = (Status.FAILED, Status.CANCELLED)


@dataclass(frozen=True)
class RowState:
    """
    Where one row stands.

    ``record`` is the row's result, if it has one, and ``status`` its status.
    ``problems`` say why the row cannot be reduced now. ``changed`` names the
    parameters at which the row's request now differs from the request of its
    result, both as a record holds them, and is ``None`` if they were not
    compared, as in what ``Batch.state(compare=False)`` gives.
    ``spec_changed`` is true if the result is of another spec than the one
    bound. ``outdated`` is true if the result is of another spec, or if the
    request changed; ``None`` if that is not known, since the requests were not
    compared. ``earlier`` is true if the row is ready and its completed result
    differs from its request only at settings: not at a column where the
    row's cell holds a value, and of the same spec. ``own`` names the
    parameter columns at which the row's cell holds a value: those where the
    row gives itself its value, not the setting or the default.
    """

    key: str
    record: Record | None = None
    status: Status | None = None
    problems: tuple[FieldError, ...] = ()
    changed: frozenset[str] | None = frozenset()
    spec_changed: bool = False
    outdated: bool | None = False
    earlier: bool = False
    own: frozenset[str] = frozenset()

    @property
    def text(self) -> str:
        """
        The status of the row, as the page, a table file written and the command
        say it: ``running``, ``incomplete``, ``ready`` (no result),
        ``failed``, ``cancelled``, ``done``, ``earlier`` or ``outdated``. A
        failed or cancelled result reads as its status whether or not the
        request changed since, since the row needs reducing either way. A row
        of earlier settings (:attr:`earlier`) reads ``earlier``: its result is
        completed, as one that reads ``done``. A completed result not compared
        with the settings, as while they are refused, reads ``outdated``: it
        cannot be told to match them.
        """
        if self.status is Status.PENDING:
            return 'running'
        if self.problems:
            return 'incomplete'
        if self.status is None:
            return 'ready'
        if self.status in _RERUN:
            return str(self.status)
        if self.earlier:
            return 'earlier'
        return 'done' if self.outdated is False else 'outdated'


def problems(
    key: str,
    cells: Mapping[str, Any],
    parameter_columns: Collection[str],
    status: Mapping[str, Status],
) -> tuple[FieldError, ...]:
    """
    Why the row of ``key`` cannot be reduced: one of ``parameter_columns``
    that ``cells`` leave out, which is empty and has no value, or a reference
    to a record that failed or was cancelled, per ``status`` by record ID.
    """
    found = [
        FieldError(key, (c,), 'empty') for c in parameter_columns if c not in cells
    ]
    found += [
        FieldError(key, loc, f'the record it reads is {status[ref.record]}')
        for loc, ref in iter_refs(cells)
        if isinstance(ref, OutputRef) and status.get(ref.record) in _RERUN
    ]
    return tuple(found)


def results(records: Iterable[Record]) -> dict[str, Record]:
    """
    The result of each row by its key: the newest of ``records``, oldest
    first, with that member, of any spec and whoever submitted it. So a rerun
    of a row is its result, and one folder or one service holds one history.
    """
    return {r.member: r for r in records if r.member is not None}


def references(cells: Iterable[Mapping[str, Any]]) -> set[str]:
    """The IDs of the records that ``cells``, those of some rows, reference."""
    return {
        ref.record
        for row in cells
        for _, ref in iter_refs(row)
        if isinstance(ref, OutputRef)
    }


def differs(
    now: Mapping[str, Any], was: Mapping[str, Any], names: Iterable[str]
) -> list[str]:
    """
    The ``names`` at which the params ``now`` differ from ``was``. A name that
    either lacks differs: a value left out takes its default, and a spec of
    another version may not have the parameter.
    """
    return [n for n in names if n not in now or n not in was or now[n] != was[n]]


def row_states(
    cells: Mapping[str, Mapping[str, Any]],
    parameter_columns: Collection[str],
    found: Mapping[str, Record],
    status: Mapping[str, Status],
    changed: Mapping[str, frozenset[str]] | None = MappingProxyType({}),
    refused: Mapping[str, tuple[FieldError, ...]] | None = None,
    spec_changed: AbstractSet[str] = frozenset(),
    *,
    own: Mapping[str, AbstractSet[str]],
) -> dict[str, RowState]:
    """
    The state of each row of ``cells``, which hold its cells by key.
    ``found`` holds their results by key, ``status`` the status of those and
    of the records their cells reference, by record ID, ``changed`` the names
    at which a row differs from its result, ``None`` if the rows were not
    compared with their results, ``refused`` the reasons a row's cells were
    refused when resolved, ``spec_changed`` the keys whose result is of
    another spec, and ``own`` the columns at which each row's cell holds a
    value, by key.
    """
    refused = refused or {}
    states = {}
    for key, row in cells.items():
        record = found.get(key)
        names = None if changed is None else changed.get(key, frozenset())
        if key in spec_changed:
            outdated: bool | None = True
        else:
            outdated = None if names is None else bool(names)
        found_problems = problems(key, row, parameter_columns, status)
        found_problems += refused.get(key, ())
        row_status = None if record is None else status[record.id]
        states[key] = RowState(
            key=key,
            record=record,
            status=row_status,
            problems=found_problems,
            changed=names,
            spec_changed=key in spec_changed,
            outdated=outdated,
            earlier=not found_problems
            and row_status is Status.COMPLETED
            and bool(names)
            and names.isdisjoint(own[key])
            and key not in spec_changed,
            own=frozenset(own[key]),
        )
    return states


def ready(states: Mapping[str, RowState]) -> list[str]:
    """The keys of the rows that can be reduced now: complete and not running."""
    return [
        k
        for k, s in states.items()
        if not s.problems and s.status is not Status.PENDING
    ]


def earlier_settings(states: Mapping[str, RowState]) -> list[str]:
    """
    The rows reduced with earlier settings (:attr:`RowState.earlier`). A page
    that applies a change of settings to new rows only leaves these rows as
    they are, until a person reduces them again. None in states not
    compared.
    """
    return [k for k, s in states.items() if s.earlier]


def left_to_reduce(states: Mapping[str, RowState]) -> list[str]:
    """
    The ready rows with no result, one that failed or was cancelled, or one
    that is outdated, but those of :func:`earlier_settings`: a change of
    settings applies to rows not reduced yet, and a change of a cell, or of the
    spec, to its row. A row not compared counts as not outdated.
    """
    return [
        k
        for k in ready(states)
        if not states[k].earlier
        and (
            states[k].status is None or states[k].status in _RERUN or states[k].outdated
        )
    ]


type Which = Literal['remaining', 'earlier', 'all']
"""Which rows to reduce: those left, those of earlier settings, or all ready."""


def to_reduce(states: Mapping[str, RowState], which: Which) -> list[str]:
    """
    The rows of ``which``: :func:`left_to_reduce`, :func:`earlier_settings`,
    or :func:`ready`.
    """
    if which == 'remaining':
        return left_to_reduce(states)
    if which == 'earlier':
        return earlier_settings(states)
    return ready(states)


def not_reduced(states: Mapping[str, RowState]) -> list[str]:
    """
    The ready rows with no result: the rule of Follow. A row with
    a result is left to a person, also one that is outdated, failed or was
    cancelled. ``changed`` is not read, so ``states`` may be compared or not.
    """
    return [k for k in ready(states) if states[k].record is None]


def publishable(states: Mapping[str, RowState]) -> list[str]:
    """
    The rows whose result may be published: those that read ``done`` or
    ``earlier``, completed and not outdated.
    """
    return [
        k
        for k, s in states.items()
        if s.text in ('done', 'earlier') and s.record is not None
    ]

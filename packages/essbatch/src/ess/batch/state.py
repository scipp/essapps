# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The state of each row of a batch, computed from plain data.

Nothing here calls a client, so the rules are tested on values.
:class:`~.reduction.Batch` reads the records and passes them in.

A cell that the workspace rejects is a problem of its row, not a change: a
change would offer the row to :func:`left_to_reduce`, though it cannot run.

The statuses and the rules: guide ``packages/essbatch/docs/batch.md``, "The
state of a row and the rules over it".
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, NamedTuple

from ess.apps.errors import FieldError
from ess.apps.records import Record, Status
from ess.spec import OutputRef
from ess.spec.refs import iter_refs

_RERUN = (Status.FAILED, Status.CANCELLED)

type RowStatus = Literal[
    'running',
    'incomplete',
    'ready',
    'failed',
    'cancelled',
    'cleared',
    'done',
    'settings changed',
    'outdated',
]
"""The status of a row, as the page, a written table file and the command show it."""


@dataclass(frozen=True)
class RowState:
    """
    The state of one row.

    Attributes
    ----------
    record:
        The result of the row, if any.
    cleared:
        Whether the outputs of the result are cleared.
    problems:
        Why the row cannot be reduced now.
    changed:
        The parameters at which the request of the row differs from the request
        of its result, both as a record holds them. ``None`` if not compared.
    spec_changed:
        Whether the result is of another spec than the batch, or of an unbound
        spec.
    outdated:
        Whether ``spec_changed`` or ``changed`` is not empty. ``None`` if not
        compared and the spec did not change.
    settings_changed:
        Whether the row is ready, its result is completed and of the same spec,
        and ``changed`` holds only settings: parameters outside ``overrides``.
    overrides:
        The parameter columns at which the cell of the row holds a value.
    """

    key: str
    record: Record | None = None
    record_status: Status | None = None
    cleared: bool = False
    problems: tuple[FieldError, ...] = ()
    changed: frozenset[str] | None = frozenset()
    spec_changed: bool = False
    outdated: bool | None = False
    settings_changed: bool = False
    overrides: frozenset[str] = frozenset()

    @property
    def status(self) -> RowStatus:
        """
        The status of the row.

        A failed or cancelled result shows its status even if the request
        changed since: the row needs reducing either way, and so does a
        cleared one. A completed result
        that was not compared, as while the settings are rejected, shows
        ``outdated``: it cannot be told to match them.
        """
        if self.record_status is Status.PENDING:
            return 'running'
        if self.problems:
            return 'incomplete'
        if self.record_status is None:
            return 'ready'
        if self.record_status is Status.FAILED:
            return 'failed'
        if self.record_status is Status.CANCELLED:
            return 'cancelled'
        if self.cleared:
            return 'cleared'
        if self.settings_changed:
            return 'settings changed'
        return 'done' if self.outdated is False else 'outdated'


class Cleared(NamedTuple):
    """
    A record whose outputs are cleared, as a reference to it is reported:
    the label of its batch, and whether the batch holds a newer result of
    its member, whose outputs a reference can read instead.
    """

    label: str | None
    newer: bool = False


def problems(
    key: str | None,
    cells: Mapping[str, Any],
    parameter_columns: Collection[str],
    status: Mapping[str, Status],
    cleared: Mapping[str, Cleared] = MappingProxyType({}),
) -> tuple[FieldError, ...]:
    """
    Why the row ``key`` cannot be reduced: a column of ``parameter_columns``
    missing from ``cells``, or a reference to a record that failed or was
    cancelled, per ``status`` by record ID, or whose outputs are cleared, per
    ``cleared`` by record ID. ``key`` is ``None`` for the settings.
    """
    found = [
        FieldError(key, (c,), 'empty') for c in parameter_columns if c not in cells
    ]
    for loc, ref in iter_refs(cells):
        if not isinstance(ref, OutputRef):
            continue
        if (record_status := status.get(ref.record)) in _RERUN:
            found.append(
                FieldError(key, loc, f'the record it reads is {record_status}')
            )
        elif ref.record in cleared:
            found.append(FieldError(key, loc, _cleared(cleared[ref.record], loc)))
    return tuple(found)


def _cleared(record: Cleared, loc: tuple[str | int, ...]) -> str:
    """
    The problem at ``loc`` of a reference to a record whose outputs are
    cleared, which says how to point it to a result to read::

        its outputs are cleared; the batch find has a newer result, set offset to it
        its outputs are cleared; reduce the batch find again, then set offset
        to the new result
    """
    if record.label is None:
        return 'its outputs are cleared'
    column = loc[0]
    if record.newer:
        return (
            f'its outputs are cleared; the batch {record.label} has a newer '
            f'result, set {column} to it'
        )
    return (
        f'its outputs are cleared; reduce the batch {record.label} again, '
        f'then set {column} to the new result'
    )


def results(records: Iterable[Record]) -> dict[str, Record]:
    """
    The result of each row by key: the newest of ``records``, given oldest
    first, with that member. Of any spec and any submitter, so a rerun of a row
    is its result.
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


def changed_names(
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
    cleared: Mapping[str, Cleared] = MappingProxyType({}),
    *,
    overrides: Mapping[str, AbstractSet[str]],
) -> dict[str, RowState]:
    """
    The state of each row of ``cells``, by key.

    A reference to a cleared record is a problem only of a row that would be
    reduced: without a result, or with one that failed, was cancelled, is
    cleared, or is outdated or not compared. A ``done`` row stays ``done``.

    Parameters
    ----------
    cells:
        The values of the parameter columns of each row, by key.
    found:
        The result of each row that has one, by key.
    status:
        The status of the results and of the records that the cells reference,
        by record ID.
    changed:
        :attr:`RowState.changed` of each row, by key; ``None`` if not compared.
    refused:
        The problems of the cells that the workspace rejected, by key.
    spec_changed:
        The keys of :attr:`RowState.spec_changed`.
    cleared:
        Each cleared record by ID, of the results and of the records that the
        cells reference.
    overrides:
        :attr:`RowState.overrides` of each row, by key.
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
        row_status = None if record is None else status[record.id]
        row_cleared = record is not None and record.id in cleared
        reduced = (
            row_status is None
            or row_status in _RERUN
            or row_cleared
            or outdated is not False  # not compared counts as outdated here
        )
        found_problems = problems(
            key, row, parameter_columns, status, cleared if reduced else {}
        )
        found_problems += refused.get(key, ())
        states[key] = RowState(
            key=key,
            record=record,
            record_status=row_status,
            cleared=row_cleared,
            problems=found_problems,
            changed=names,
            spec_changed=key in spec_changed,
            outdated=outdated,
            settings_changed=not found_problems
            and row_status is Status.COMPLETED
            and not row_cleared
            and bool(names)
            and names.isdisjoint(overrides[key])
            and key not in spec_changed,
            overrides=frozenset(overrides[key]),
        )
    return states


def ready(states: Mapping[str, RowState]) -> list[str]:
    """The keys of the rows that can be reduced now: complete and not running."""
    return [
        k
        for k, s in states.items()
        if not s.problems and s.record_status is not Status.PENDING
    ]


def with_changed_settings(states: Mapping[str, RowState]) -> list[str]:
    """
    The rows of :attr:`RowState.settings_changed`; none in states not compared.
    """
    return [k for k, s in states.items() if s.settings_changed]


def left_to_reduce(states: Mapping[str, RowState]) -> list[str]:
    """
    The ready rows without a result, or whose result failed, was cancelled, is
    cleared or is outdated, except those of :func:`with_changed_settings`.

    A row not compared counts as not outdated.
    """
    return [
        k
        for k in ready(states)
        if not states[k].settings_changed
        and (
            states[k].record_status is None
            or states[k].record_status in _RERUN
            or states[k].cleared
            or states[k].outdated
        )
    ]


type Which = Literal['remaining', 'settings changed', 'all']
"""Which rows to reduce, as :func:`to_reduce` accepts it."""


def to_reduce(states: Mapping[str, RowState], which: Which) -> list[str]:
    """
    The rows of ``which``: :func:`left_to_reduce`, :func:`with_changed_settings`,
    or :func:`ready`.
    """
    if which == 'remaining':
        return left_to_reduce(states)
    if which == 'settings changed':
        return with_changed_settings(states)
    return ready(states)


def not_reduced(states: Mapping[str, RowState]) -> list[str]:
    """
    The ready rows without a result: the rows that a step of Follow reduces.

    ``changed`` is not read, so ``states`` may be compared or not.
    """
    return [k for k in ready(states) if states[k].record is None]


def publishable(states: Mapping[str, RowState]) -> list[str]:
    """The rows whose result may be published: ``done`` or ``settings changed``."""
    return [
        k
        for k, s in states.items()
        if s.status in ('done', 'settings changed') and s.record is not None
    ]

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
(:attr:`RowState.spec_changed`), or if the request changed: at any parameter
for a result reduced with the settings, at a column for a tweak, whose other
parameters are its own (:func:`row_states`)::

    changed == {'binning'}               # outdated, but a tweak is not
    changed == {'binning', 'thickness'}  # outdated, a tweak too

A value refused when resolved is a problem of the row, not a change: the row
cannot run with it, and a change would offer it to :func:`remaining`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel

from ess.spec import OutputRef
from ess.spec.refs import iter_refs

from ..errors import FieldError
from ..records import Record, Status
from .table import Row

_RERUN = (Status.FAILED, Status.CANCELLED)


@dataclass(frozen=True)
class RowState:
    """
    Where one row stands.

    ``record`` is the row's result, if it has one, and ``status`` its status.
    ``problems`` say why the row cannot be reduced now. ``changed`` names the
    parameters at which the row's request now differs from the request of its
    result, both as a record holds them, and is ``None`` if they were not
    compared, as in what :meth:`~.reduction.Batch.results` gives.
    ``spec_changed`` is true if the result is of another spec than the one
    bound. ``tweaked`` is true if the result came from
    :meth:`~.reduction.Batch.tweak` of this batch, with parameters of its own,
    so that it does not follow the settings. ``outdated`` is true if the result
    is of another spec, or if the request changed, at a column only for a
    tweak; ``None`` if that is not known, since the requests were not compared.
    """

    key: str
    record: Record | None = None
    status: Status | None = None
    problems: tuple[FieldError, ...] = ()
    changed: frozenset[str] | None = frozenset()
    spec_changed: bool = False
    tweaked: bool = False
    outdated: bool | None = False

    @property
    def text(self) -> str:
        """
        What a table shows. A failed or cancelled result reads as its status
        whether or not the request changed since, since the row needs reducing
        either way. A completed result not compared with the settings, as while
        they are refused, reads ``outdated``: it cannot be told to match them. A
        tweak does not follow the settings, so it reads as its status.
        """
        if self.status is Status.PENDING:
            return 'pending'
        if self.problems:
            return 'incomplete'
        if self.status is None:
            return 'not reduced'
        if self.status in _RERUN:
            return str(self.status)
        if self.outdated:
            return 'outdated'
        if self.outdated is None and self.status is Status.COMPLETED:
            return str(self.status) if self.tweaked else 'outdated'
        return str(self.status)


def problems(
    row: Row,
    model: type[BaseModel],
    columns: Collection[str],
    status: Mapping[str, Status],
) -> tuple[FieldError, ...]:
    """
    Why ``row`` cannot be reduced: a value at a name that is not a column, an
    empty cell of a column with no default, or a reference to a record that
    failed or was cancelled, per ``status`` by record ID.
    """
    found = [
        FieldError(row.key, (name,), 'not a column')
        for name in row.values
        if name not in columns
    ]
    found += [
        FieldError(row.key, (column,), 'empty')
        for column in columns
        if column not in row.values and model.model_fields[column].is_required()
    ]
    found += [
        FieldError(row.key, loc, f'the record it reads is {status[ref.record]}')
        for loc, ref in iter_refs(row.values)
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


def references(rows: Iterable[Row]) -> set[str]:
    """The IDs of the records that the cells of ``rows`` reference."""
    return {
        ref.record
        for row in rows
        for _, ref in iter_refs(row.values)
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
    rows: Iterable[Row],
    model: type[BaseModel],
    columns: Collection[str],
    found: Mapping[str, Record],
    status: Mapping[str, Status],
    changed: Mapping[str, frozenset[str]] | None = MappingProxyType({}),
    refused: Mapping[str, tuple[FieldError, ...]] | None = None,
    spec_changed: AbstractSet[str] = frozenset(),
    tweaked: AbstractSet[str] = frozenset(),
) -> dict[str, RowState]:
    """
    The state of each row. ``found`` holds their results by key, ``status`` the
    status of those and of the records their cells reference, by record ID,
    ``changed`` the names at which a row differs from its result, ``None`` if
    the rows were not compared with their results, ``refused``
    the reasons a row's cells were refused when resolved, ``spec_changed`` the
    keys whose result is of another spec, and ``tweaked`` the keys whose result
    is a tweak.
    """
    refused = refused or {}
    states = {}
    for row in rows:
        record = found.get(row.key)
        names = None if changed is None else changed.get(row.key, frozenset())
        tweak = row.key in tweaked
        if row.key in spec_changed:
            outdated: bool | None = True
        elif names is None:
            outdated = None
        else:
            outdated = bool(names & set(columns) if tweak else names)
        states[row.key] = RowState(
            key=row.key,
            record=record,
            status=None if record is None else status[record.id],
            problems=problems(row, model, columns, status) + refused.get(row.key, ()),
            changed=names,
            spec_changed=row.key in spec_changed,
            tweaked=tweak,
            outdated=outdated,
        )
    return states


def compared(states: Mapping[str, RowState]) -> bool:
    """
    Whether each row was compared with its result, as in what
    :meth:`~.reduction.Batch.state` gives, but not
    :meth:`~.reduction.Batch.results`.
    """
    return all(s.changed is not None for s in states.values())


def _compared(states: Mapping[str, RowState]) -> dict[str, frozenset[str]]:
    """
    What changed in each row; raises ``ValueError`` if a row was not compared
    with its result.
    """
    changed = {k: s.changed for k, s in states.items() if s.changed is not None}
    if not_compared := sorted(states.keys() - changed.keys()):
        raise ValueError(
            f'rows {not_compared} were not compared with their results: '
            'the states are those of Batch.results, not of Batch.state'
        )
    return changed


def ready(states: Mapping[str, RowState]) -> list[str]:
    """The keys of the rows that can be reduced now: complete and not running."""
    return [
        k
        for k, s in states.items()
        if not s.problems and s.status is not Status.PENDING
    ]


def remaining(states: Mapping[str, RowState]) -> list[str]:
    """
    The ready rows with no result, one that failed or was cancelled, or one
    that is outdated. Raises ``ValueError`` if a row was not compared with its
    result (:attr:`RowState.changed` is ``None``).
    """
    _compared(states)
    return [
        k
        for k in ready(states)
        if states[k].status is None or states[k].status in _RERUN or states[k].outdated
    ]


def earlier_settings(
    states: Mapping[str, RowState], columns: Collection[str]
) -> list[str]:
    """
    The ready rows whose completed result differs from their request only at
    settings: not at a column of ``columns``, and of the same spec. A tweak's
    settings are its own, so a tweak is not one of them. ``changed`` is read, so
    ``states`` are those of :meth:`~.reduction.Batch.state`; raises
    ``ValueError`` for a row that was not compared.

    A page that applies a change of settings to new rows only leaves these
    rows as they are, until a person reduces them again.
    """
    changed = _compared(states)
    return [
        k
        for k in ready(states)
        if states[k].status is Status.COMPLETED
        and changed[k]
        and changed[k].isdisjoint(columns)
        and not states[k].spec_changed
        and not states[k].tweaked
    ]


def left_to_reduce(
    states: Mapping[str, RowState], columns: Collection[str]
) -> list[str]:
    """
    The rows of :func:`remaining` but those of :func:`earlier_settings`: a
    change of settings applies to rows not reduced yet, and a change of a
    cell, or of the spec, to its row. Raises ``ValueError`` for a row that was
    not compared.
    """
    earlier = set(earlier_settings(states, columns))
    return [k for k in remaining(states) if k not in earlier]


def arrived(states: Mapping[str, RowState], at: Collection[str]) -> list[str]:
    """
    The ready rows with no result, or whose request differs from their result's
    at a column of ``at``: the rule of automatic reduction. ``changed`` is read,
    so ``states`` are those of :meth:`~.reduction.Batch.state`; raises
    ``ValueError`` for a row that was not compared.

    A failed or cancelled result counts as any other, so such a row is given
    again only for a change at ``at``. With ``at=set()``, no row with a result
    is given.
    """
    changed = _compared(states)
    return [
        k
        for k in ready(states)
        if states[k].record is None or not changed[k].isdisjoint(at)
    ]


def refused_again(
    states: Mapping[str, RowState], at: Collection[str], refused: Mapping[str, str]
) -> dict[str, str]:
    """
    Of the reasons ``refused`` by row, those of the rows that the next step of
    automatic reduction would submit again: the rows of :func:`arrived`. States
    that were not compared (:func:`compared`) tell this only for the rows with
    no result.
    """
    again = (
        arrived(states, at)
        if compared(states)
        else [k for k, s in states.items() if s.record is None]
    )
    return {k: refused[k] for k in again if k in refused}


EARLIER = 'earlier settings'
"""How a table marks a row of :func:`earlier_settings`."""


def status_texts(
    states: Mapping[str, RowState], columns: Collection[str]
) -> dict[str, str]:
    """
    The status of each row as a table shows it, by key: its
    :attr:`RowState.text`, but ``completed, earlier settings`` for a row of
    :func:`earlier_settings`, since a change of settings applies to the rows
    not reduced yet. States that were not compared mark no row.
    """
    earlier = set(earlier_settings(states, columns)) if compared(states) else set()
    return {
        k: f'completed, {EARLIER}' if k in earlier else s.text
        for k, s in states.items()
    }

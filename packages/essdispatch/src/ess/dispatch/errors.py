# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the client raises, beyond Python's own errors for a wrong call.

- :class:`SubmitError`: a request, a template, a push, a freeze, or a
  persist that is refused; it lists each reason as a :class:`FieldError`.
- :class:`NotReadable`: a record, an output, or an accumulator that exists
  but cannot be read, such as a record the client released. One that is
  unknown raises ``KeyError``; both are a ``LookupError``.
- :class:`RecordFailed`: a read of a record that failed or was cancelled.
- :class:`ClientEnded`: a call of a client that has ended.

Each holds only strings, integers, and tuples of these, so that a wire
format can carry it as it is::

    try:
        client.submit(batch)
    except SubmitError as error:
        for e in error.errors:
            form.mark(e.entry, e.loc, e.msg)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ess.spec.refs import Loc, ParamsError, loc_text

if TYPE_CHECKING:
    from .records import Status


@dataclass(frozen=True)
class FieldError:
    """
    One reason a request is refused, as pydantic's ``ValidationError.errors()``
    gives it: ``loc``, the place of the field at fault (see
    :data:`ess.spec.refs.Loc`), and ``msg``, why.

    ``loc`` is empty for a reason that names no field, such as an unknown
    spec. ``entry`` is the request's key, or its index, among the requests of
    one call, or ``None`` for a call with one request. It prints as::

        typo: reduction.bins: Input should be greater than 0
    """

    entry: str | int | None
    loc: Loc
    msg: str

    def __str__(self) -> str:
        entry = f'[{self.entry}]' if isinstance(self.entry, int) else self.entry
        place = loc_text(self.loc) if self.loc else None
        return ': '.join(p for p in (entry, place, self.msg) if p is not None)


class SubmitError(ValueError):
    """
    A request that cannot run, refused before any record exists; ``errors``
    lists why, each a :class:`FieldError`, and the message joins them.
    """

    def __init__(self, errors: Iterable[FieldError]) -> None:
        self.errors = tuple(errors)
        super().__init__(self.errors)

    @classmethod
    def at(cls, loc: Loc, msg: str) -> SubmitError:
        """One reason, at the place ``loc``, with no entry."""
        return cls([FieldError(None, loc, msg)])

    @classmethod
    def of(cls, error: ParamsError) -> SubmitError:
        """The reasons of params that :mod:`ess.spec` refuses, with no entry."""
        return cls(FieldError(None, loc, msg) for loc, msg in error.errors)

    def __str__(self) -> str:
        return '; '.join(map(str, self.errors))


class NotReadable(LookupError):
    """
    A record, an output, or an accumulator that exists, but that the client
    cannot read: a record it neither keeps nor persisted; an output whose
    write failed, that the store no longer holds, or that the workflow did
    not return; an accumulator it released, that stopped, or that is read
    through the record of its freeze; a state whose plain request would be
    refused, or one of whose pushes failed after the read pinned it. One that
    does not exist raises ``KeyError``.
    """


class RecordFailed(Exception):
    """
    A read of a record that failed or was cancelled. ``reason`` says why, or
    is ``None`` for a record a client cancelled.
    """

    def __init__(self, record: str, status: Status, reason: str | None) -> None:
        self.record = record
        self.status = status
        self.reason = reason
        super().__init__(record, status, reason)

    def __str__(self) -> str:
        why = f': {self.reason}' if self.reason is not None else ''
        return f'record {self.record} {self.status}{why}'


class ClientEnded(Exception):
    """A call of a client that has ended."""

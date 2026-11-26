# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The errors that the client raises, beyond Python's own errors for a wrong call.

- :class:`SubmitError`: a request, a template, a push, a freeze, or ``persist``
  is rejected. It lists each problem as a :class:`FieldError`.
- :class:`NotReadable`: a record, an output, or an accumulator exists but cannot
  be read, such as a released record. An unknown one raises ``KeyError``. Both
  are a ``LookupError``.
- :class:`RecordFailed`: a read of a record that failed or was cancelled.
- :class:`Stopped`: any call of a workspace that stopped or is closed.

Each error holds only strings, integers, and tuples of these, so that a wire
format can carry it unchanged::

    try:
        client.submit(batch)
    except SubmitError as error:
        for e in error.errors:
            form.mark(e.request_key, e.loc, e.msg)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ess.spec.refs import Loc, ParamsError, loc_text

if TYPE_CHECKING:
    from .records import Status


@dataclass(frozen=True)
class FieldError:
    """
    One problem with a request, as an item of pydantic's
    ``ValidationError.errors()``.

    ``loc`` is the position of the field at fault (see :data:`ess.spec.refs.Loc`).
    It is empty for a problem at no field, such as an unknown spec. ``msg``
    describes the problem. ``request_key`` is the request's key or index among
    the requests of one call, or ``None`` for a call with one request. A
    ``FieldError`` prints as::

        typo: reduction.bins: Input should be greater than 0
    """

    request_key: str | int | None
    loc: Loc
    msg: str

    def __str__(self) -> str:
        key = (
            f'[{self.request_key}]'
            if isinstance(self.request_key, int)
            else self.request_key
        )
        at = loc_text(self.loc) if self.loc else None
        return ': '.join(p for p in (key, at, self.msg) if p is not None)


class SubmitError(ValueError):
    """
    A request that cannot run, rejected before any record exists.

    ``errors`` lists each problem as a :class:`FieldError`, and the message
    joins them.
    """

    def __init__(self, errors: Iterable[FieldError]) -> None:
        self.errors = tuple(errors)
        super().__init__(self.errors)

    @classmethod
    def at(cls, loc: Loc, msg: str) -> SubmitError:
        """One reason, at the position ``loc``, with no request key."""
        return cls([FieldError(None, loc, msg)])

    @classmethod
    def of(cls, error: ParamsError) -> SubmitError:
        """The reasons of params that :mod:`ess.spec` refuses, with no request key."""
        return cls(FieldError(None, loc, msg) for loc, msg in error.errors)

    def __str__(self) -> str:
        return '; '.join(map(str, self.errors))


class NotReadable(LookupError):
    """
    A record, an output, or an accumulator that exists but cannot be read.

    :meth:`~ess.apps.protocol.WorkspaceProtocol.outputs` and
    :meth:`~ess.apps.protocol.WorkspaceProtocol.accumulator_outputs` list the
    cases. One that does not exist raises ``KeyError``.
    """


class RecordFailed(Exception):
    """
    A read of a record that failed or was cancelled.

    ``reason`` is why, or ``None`` for a record that a client cancelled.
    """

    def __init__(self, record: str, status: Status, reason: str | None) -> None:
        self.record = record
        self.status = status
        self.reason = reason
        super().__init__(record, status, reason)

    def __str__(self) -> str:
        why = f': {self.reason}' if self.reason is not None else ''
        return f'record {self.record} {self.status}{why}'


class Stopped(RuntimeError):
    """
    The workspace accepts no more calls: it is closed, or it stopped because an
    append to its log failed.
    """


def error_text(error: Exception) -> str:
    """
    The text for a person of an error that the code of an application raised.

    Such code is a row source, the file source, or a drawing::

        ValidationError        'run must be a valid integer', by field_error_text
        ValueError('bad')      'bad'
        OSError('gone')        'gone'
        RuntimeError('boom')   'RuntimeError: boom'
        KeyError('run')        "KeyError: 'run'"
    """
    name = type(error).__name__
    if isinstance(error, ValidationError):
        return '; '.join(
            field_error_text('.'.join(map(str, e['loc'])), e['msg'])
            if e['loc']
            else e['msg']
            for e in error.errors()
        )
    if isinstance(error, ValueError | OSError):
        return str(error) or name
    return f'{name}: {error}' if str(error) else name


def field_error_text(field: str, message: str) -> str:
    """
    A problem at ``field`` as a sentence::

        field_error_text('run', 'Field required')         # 'run is missing'
        field_error_text('run', 'Input should be > 0')    # 'run must be > 0'
        field_error_text('run', 'Value error, too big')   # 'run: too big'
    """
    message = message.removeprefix('Value error, ')
    if message in ('empty', 'missing', 'Field required'):
        return f'{field} is missing'
    if message.startswith('Input should be '):
        return f'{field} must be {message.removeprefix("Input should be ")}'
    return f'{field}: {message}'

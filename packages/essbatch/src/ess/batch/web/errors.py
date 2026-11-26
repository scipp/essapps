# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The exceptions by which the web layer rejects a request, and the messages made
of exceptions.

:mod:`.api` returns a :class:`Rejected` as a 422 with its message, and a
:class:`NotFound` as a 404. A message adds what the person asked for, such as
"The batch was not made: ", to the text of the exception as it is. So the page
copies no rule of the batch layer.

Code of the application, such as a row source, may raise anything.
:func:`reject_on_error` turns any exception into a :class:`Rejected`, worded by
:func:`ess.apps.errors.error_text`, which names the type of an unexpected
exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from ess.apps.errors import error_text, field_error_text

from ..definition import DefinitionWriteError
from . import forms


class Rejected(Exception):
    """A rejected request, with the message for the person."""


class NotFound(Exception):
    """No batch has the label asked for."""


def titled_field_error(title: str, message: str) -> str:
    """A reason at the field ``title`` as a sentence: "Thickness is missing"."""
    return field_error_text(title, forms.message(message))


def request_error_text(error: Exception) -> str:
    """
    The message of a ``ValueError``, ``OSError`` or ``KeyError``, without its
    type or quotes. For a ``KeyError``, it is the first argument.
    """
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return error_text(error)


@contextmanager
def reject_on_error(doing: str = '') -> Iterator[None]:
    """
    A block that runs code of the application, and so may raise anything.

    An exception becomes a :class:`Rejected` with the message
    ``'<doing>: <error_text>.'``, or the sentence of :func:`error_text` if
    ``doing`` is empty. A rejected write of the definitions becomes the
    message of :func:`reject_on_write_error`.
    """
    try:
        with reject_on_write_error():
            yield
    except Rejected:
        raise
    except Exception as error:  # code of the application
        text = error_text(error)
        text = f'{doing}: {text}' if doing else f'{text[:1].upper()}{text[1:]}'
        raise Rejected(text if text.endswith('.') else f'{text}.') from None


@contextmanager
def reject_on_write_error() -> Iterator[None]:
    """
    A block in which a :class:`~ess.batch.DefinitionWriteError` raises
    :class:`Rejected`.
    """
    try:
        yield
    except DefinitionWriteError as error:
        raise Rejected(f'The change was not kept: {error}') from None

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
How the web layer refuses a request, and says why: :class:`Problem` (a 422
with a message) or :class:`Missing` (a 404). :class:`~.pages.Pages` and
:class:`~.page.Page` raise them; :mod:`.api` answers them.

The batch layer refuses with exceptions, and the page says each as a
sentence for a person:

======================================  =====================================
raised by the batch layer               said
======================================  =====================================
``ValueError``                          its text, as it is (:func:`why`)
``KeyError``                            its first argument (:func:`why`)
``pydantic.ValidationError``            each error at its field (:func:`say`)
:class:`~ess.apps.batch.NotKept`        "The change was not kept: ..."
                                        (:func:`kept`)
======================================  =====================================

So the page copies no rule of the batch layer: it adds what the person
asked for, such as "The batch was not made: ", and says the rest as given.

Code of the application, such as a source, the file source or a drawing,
may raise anything; the page says it and keeps working
(:func:`ess.apps.errors.said`, which the command ``essapps-batch`` uses too).
A ``ValueError`` or an ``OSError``, such as a mount that went away, is a
refusal, said as its text. Anything else is a defect of that code, and is
said with its type, so that a person can tell: ``RuntimeError: boom``,
``KeyError: 'run'``. A request that runs such code says it with
:func:`refused`; the listing of the files, the step of Follow and the plot
show it in place of what they could not give.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import ValidationError

from ...errors import said
from ..definition import NotKept
from . import forms


class Problem(Exception):
    """A request that cannot be done, with what to tell the person."""


class Missing(Exception):
    """No batch has the label asked for."""


def say(title: str, message: str) -> str:
    """A reason at the field ``title``, as a sentence."""
    message = forms.message(message)
    if message in ('empty', 'missing', 'Field required'):
        return f'{title} is missing'
    if message.startswith('Input should be '):
        return f'{title} must be {message.removeprefix("Input should be ")}'
    return f'{title}: {message}'


def why(error: Exception) -> str:
    """What an exception of a request says, without its type or quotes."""
    if isinstance(error, ValidationError):
        return '; '.join(
            say('.'.join(map(str, e['loc'])), e['msg']) for e in error.errors()
        )
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error)


@contextmanager
def refused(doing: str = '') -> Iterator[None]:
    """
    A block that runs code of the application, such as a source, and so
    may raise anything: a write that the definitions refuse raises
    :class:`Problem` as :func:`kept` says it, and any other exception as
    ``'<doing>: <said>.'``, or as the sentence of :func:`said` if ``doing``
    is empty (:func:`said`).
    """
    try:
        with kept():
            yield
    except Problem:
        raise
    except Exception as error:  # code of the application
        text = said(error)
        text = f'{doing}: {text}' if doing else f'{text[:1].upper()}{text[1:]}'
        raise Problem(text if text.endswith('.') else f'{text}.') from None


@contextmanager
def kept() -> Iterator[None]:
    """
    A block whose writes to the definitions, if refused
    (:class:`~ess.apps.batch.NotKept`), raise :class:`Problem`.
    """
    try:
        yield
    except NotKept as error:
        raise Problem(f'The change was not kept: {error}') from None

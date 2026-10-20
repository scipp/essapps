# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend's history: an append-only log of what ran, with which params,
and what came of it.

There are seven events: a submission, a record that finished, an
accumulator that opened, a push into one, a push that failed, a request to
persist outputs of a record, and a write that ended. A persist request made
with a submission or a freeze is part of that event, and its write is the
record's: the record finishes once the outputs are written, so no write
event follows. A submission that read an accumulator, and a freeze, hold a
record of the plain request of the state read, as the accumulator and its
number of pushes, before the records that read it; the views list its
request when it is read, without the rows of the pushes whose failure was
logged before it. The backend's views, such as the records by ID, are built by
applying the events in order, when they are appended and again when a backend
starts from an existing log.

An event names only records and accumulators appended before it, or earlier
in the same submission: a finish, a persist request, or a write names its
record, a push its accumulator, a failed push its accumulator and a push
logged before it, a record the records it reads, and a record of a state its
accumulator. Applying the events in order relies on this, and
so does a restart, which walks the pending records backwards.

Clients and their stages and accumulators are not history, and neither are
output values or anything a binding computed; those have their own lifetime.
A submission is one event, so a backend that stops half-way through one has
all of it or none of it.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from datetime import datetime
from io import FileIO
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter, ValidationError

from .records import Params, Request, Status, Template, read_refs


class NewRecord(BaseModel, frozen=True, extra='forbid'):
    """
    One record of a submission, as the backend accepted it.

    ``persist`` names the outputs persisted at submission: the record
    finishes once they are written.
    """

    id: str
    request: Request
    outputs: tuple[str, ...]
    label: str | None = None
    member: str | None = None
    persist: tuple[str, ...] = ()


class NewStateRecord(BaseModel, frozen=True, extra='forbid'):
    """
    The record of the plain request of an accumulator's state: the
    accumulator's template with each table filled by the rows of its first
    ``pushes`` pushes, but those whose failure the log holds before this
    record. A freeze names the outputs it persists in ``persist``.
    """

    id: str
    accumulator: str
    pushes: int
    outputs: tuple[str, ...]
    persist: tuple[str, ...] = ()


class Submitted(BaseModel, frozen=True, extra='forbid'):
    kind: Literal['submitted'] = 'submitted'
    time: datetime
    proposal: str
    submitter: str
    records: tuple[NewRecord | NewStateRecord, ...]


class Finished(BaseModel, frozen=True, extra='forbid'):
    """
    A record that finished: ``failure`` says why it failed, or why the
    backend cancelled it. ``omitted`` names the optional outputs that the
    workflow of a completed record did not return.
    """

    kind: Literal['finished'] = 'finished'
    record: str
    status: Status
    failure: str | None = None
    omitted: tuple[str, ...] = ()


class Opened(BaseModel, frozen=True, extra='forbid'):
    """
    An accumulator that opened, with its template as the backend accepted it.

    The template's blanks are the tables that the pushes fill. ``params``
    holds the template's other values, defaults filled in, inline values in
    JSON's types, and the blank tables, without rows: the driver was made
    with the other values, and with the rows pushed they
    give the plain request of a state. ``template`` holds the values as the
    client gave them, names resolved, which the client gets back, as it does a
    stage's template.
    """

    kind: Literal['opened'] = 'opened'
    accumulator: str
    proposal: str
    template: Template
    params: Params


class Pushed(BaseModel, frozen=True, extra='forbid'):
    """
    A push into an accumulator: rows of one or more tables, in ``params``
    under the name of each, names resolved.
    """

    kind: Literal['pushed'] = 'pushed'
    accumulator: str
    params: Annotated[dict[str, list[Any]], BeforeValidator(read_refs)]


class PushFailed(BaseModel, frozen=True, extra='forbid'):
    """
    A push into an accumulator that failed: it added no rows, so the state
    after it is the state before it. ``push`` numbers it among the pushes
    into the accumulator, from 1. A push fails after later pushes may have
    been logged, and pushes fail in the order logged.
    """

    kind: Literal['push failed'] = 'push failed'
    accumulator: str
    push: int
    failure: str


class Persist(BaseModel, frozen=True, extra='forbid'):
    """
    A request to persist outputs of a completed or pending record, made after
    its submission.
    """

    kind: Literal['persist'] = 'persist'
    record: str
    outputs: tuple[str, ...]


class Written(BaseModel, frozen=True, extra='forbid'):
    """
    A write that ended, of outputs that a persist request named: ``failure``
    says why it failed, or is ``None``.
    """

    kind: Literal['written'] = 'written'
    record: str
    outputs: tuple[str, ...]
    failure: str | None = None


Event = Annotated[
    Submitted | Finished | Opened | Pushed | PushFailed | Persist | Written,
    Field(discriminator='kind'),
]
_event: TypeAdapter[Event] = TypeAdapter(Event)


def _events(path: Path, lines: list[bytes]) -> list[Event]:
    """
    The events of ``lines`` of the log file ``path``. Raises ``ValueError``
    naming the first line that is not an event of this format, such as one
    with a field an event lacks, which validating would drop silently.
    """

    def read(number: int, line: bytes) -> Event:
        try:
            return _event.validate_json(line)
        # A request refuses a field it lacks by its __init__, as a TypeError.
        except (ValidationError, TypeError) as error:
            raise ValueError(
                f'{path}, line {number}: not an event of a log of this format: {error}'
            ) from None

    return [read(number, line) for number, line in enumerate(lines, start=1)]


def _complete_lines(data: bytes) -> list[bytes]:
    """The lines of ``data``, without a last line cut short by a crash."""
    lines = data.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b'\n'):
        lines.pop()
    return lines


def _hold(path: Path) -> FileIO:
    """``path`` opened to read and append, held against every other open."""
    file = path.open('a+b', buffering=0)
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.close()
        raise RuntimeError(f'{path} is held by another log') from None
    return file


def _append_line(file: FileIO, line: bytes) -> None:
    """Append ``line`` whole, or leave the file as it was."""
    size = file.seek(0, os.SEEK_END)
    try:
        written = 0
        while written < len(line):
            written += file.write(line[written:])
    except BaseException:
        file.truncate(size)
        raise


class Log:
    """
    Events in the order they were appended.

    Without a path the log lives in memory. With a path it is a file of one
    JSON event per line, and the log holds the file until :meth:`close`: a
    second log on the same file, in this process or another, is refused, so
    a file has one writer. The operating system lets go of the file when the
    process ends, so a log opened after a crash takes it over. The events
    already in the file are read first. A last line cut short, by a crash
    while it was written, is dropped; any other line that is not an event of
    this format raises ``ValueError``. A write that fails, for example on a
    full disk, leaves the file as it was, so no later event follows a line cut
    short.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._file: FileIO | None = None
        self._events: list[Event] = []
        if path is not None:
            self._file = file = _hold(path)
            try:
                file.seek(0)
                lines = _complete_lines(file.readall())
                file.truncate(sum(map(len, lines)))
                self._events = _events(path, lines)
            except BaseException:
                file.close()
                raise

    @staticmethod
    def read(path: Path) -> list[Event]:
        """
        The events in a log file, without holding it.

        A log may be writing the file meanwhile, so a last line cut short is
        left out, not dropped from the file.
        """
        return _events(path, _complete_lines(path.read_bytes()))

    def close(self) -> None:
        """Let go of the file, if any; the log takes no more events."""
        if self._file is not None:
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, event: Event) -> Event:
        """
        Append an event, and return it as the log holds it.

        The returned event is the one read back from its JSON, so values that
        JSON does not keep apart, such as a tuple and a list, are the same
        whether the event was just appended or read from a file.
        """
        line = _event.dump_json(event) + b'\n'
        stored = _event.validate_json(line)
        if self._file is not None:
            _append_line(self._file, line)
        self._events.append(stored)
        return stored

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

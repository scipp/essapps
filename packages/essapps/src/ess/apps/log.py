# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend's history: an append-only log of what ran, with which inputs,
and what came of it.

There are four events: a submission, a record that finished, an accumulator
that opened, and a push into one. A record that read an accumulator names how
many pushes it read, and the accumulator's template and those pushes say what
that state is. The backend's views, such as the records by ID, are built by
applying the events in order, when they are appended and again when a backend
starts from an existing log.

Clients and their stages and accumulators are not history, and neither are
output values or anything a binding computed; those have their own lifetime.
A submission is one event, so a backend that stops half-way through one has
all of it or none of it.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
from collections.abc import Iterator
from datetime import datetime
from io import FileIO
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, TypeAdapter

from .records import Request, Row, Status, Template, map_refs


class NewRecord(BaseModel, frozen=True):
    """One record of a submission, as the backend accepted it."""

    id: str
    request: Request
    outputs: tuple[str, ...]
    label: str | None = None
    member: str | None = None


class Submitted(BaseModel, frozen=True):
    kind: Literal['submitted'] = 'submitted'
    time: datetime
    proposal: str
    submitter: str
    records: tuple[NewRecord, ...]


class Finished(BaseModel, frozen=True):
    kind: Literal['finished'] = 'finished'
    record: str
    status: Status
    failure: str | None = None


class Opened(BaseModel, frozen=True):
    """
    An accumulator that opened, with its template as the backend accepted it.

    The template's one blank is the table that the pushes fill.
    """

    kind: Literal['opened'] = 'opened'
    accumulator: str
    proposal: str
    template: Template


class Pushed(BaseModel, frozen=True):
    kind: Literal['pushed'] = 'pushed'
    accumulator: str
    row: Row


Event = Annotated[Submitted | Finished | Opened | Pushed, Field(discriminator='kind')]
_event = TypeAdapter(Event)


def _refs(value: Any) -> Any:
    """``value`` with references read as references, not dicts."""
    return map_refs(value, lambda ref: ref)


def _with_refs(event: Event) -> Event:
    """The event with references in values read as references, not dicts."""
    match event:
        case Submitted():
            records = tuple(
                r.model_copy(
                    update={'request': Request(r.request.spec, _refs(r.request.params))}
                )
                for r in event.records
            )
            return event.model_copy(update={'records': records})
        case Opened():
            params = _refs(event.template.params)
            template = dataclasses.replace(event.template, params=params)
            return event.model_copy(update={'template': template})
        case Pushed():
            return event.model_copy(update={'row': _refs(event.row)})
    return event


def _parse(line: bytes) -> Event:
    return _with_refs(_event.validate_json(line))


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
    a file has one writer. The events already in the file are read first. A
    last line cut short, by a crash while it was written, is dropped. A write
    that fails, for example on a full disk, leaves the file as it was, so no
    later event follows a line cut short.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._file = None if path is None else _hold(path)
        self._events: list[Event] = []
        if self._file is not None:
            try:
                self._file.seek(0)
                lines = _complete_lines(self._file.readall())
                self._file.truncate(sum(map(len, lines)))
                self._events = [_parse(line) for line in lines]
            except BaseException:
                self._file.close()
                raise

    @staticmethod
    def read(path: Path) -> list[Event]:
        """
        The events in a log file, without holding it.

        A log may be writing the file meanwhile, so a last line cut short is
        left out, not dropped from the file.
        """
        return [_parse(line) for line in _complete_lines(path.read_bytes())]

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
        stored = _parse(line)
        if self._file is not None:
            _append_line(self._file, line)
        self._events.append(stored)
        return stored

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

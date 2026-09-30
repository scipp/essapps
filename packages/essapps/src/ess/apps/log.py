# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend's history: an append-only log of what ran, with which inputs,
and what came of it.

There are three events: a submission, a record that finished, and a push into
an accumulator, whose elements a snapshot covers. The backend's views, such as
the records by ID, are built by applying the events in order, when they are
appended and again when a backend starts from an existing log.

Sessions and their holders are not history, and neither are output values or
anything a binding computed; those have their own lifetime. A submission is
one event, so a backend that stops half-way through one has all of it or none
of it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from .records import Element, Request, Status, Submission, map_refs


class NewRecord(BaseModel, frozen=True):
    """One record of a submission, as the backend accepted it."""

    id: str
    submitted: Submission
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


class Pushed(BaseModel, frozen=True):
    kind: Literal['pushed'] = 'pushed'
    accumulator: str
    element: Element


Event = Annotated[Submitted | Finished | Pushed, Field(discriminator='kind')]
_event = TypeAdapter(Event)


def _with_refs(event: Event) -> Event:
    """The event with references in request values read as references, not dicts."""
    if not isinstance(event, Submitted):
        return event
    records = tuple(
        r.model_copy(
            update={
                'submitted': Request(
                    r.submitted.spec, map_refs(r.submitted.params, lambda ref: ref)
                )
            }
        )
        if isinstance(r.submitted, Request)
        else r
        for r in event.records
    )
    return event.model_copy(update={'records': records})


def _parse(line: bytes) -> Event:
    return _with_refs(_event.validate_json(line))


def _append_line(path: Path, line: bytes) -> None:
    """Append ``line`` whole, or leave the file as it was."""
    with path.open('ab', buffering=0) as file:
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
    JSON event per line; the events already in the file are read first. A
    last line cut short, by a crash while it was written, is dropped. A write
    that fails, for example on a full disk, leaves the file as it was, so no
    later event follows a line cut short.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._events: list[Event] = []
        if path is not None and path.exists():
            self._events = self._read(path)

    @staticmethod
    def _read(path: Path) -> list[Event]:
        events = []
        with path.open('rb') as file:
            lines = file.readlines()
        for i, line in enumerate(lines):
            if i == len(lines) - 1 and not line.endswith(b'\n'):
                with path.open('r+b') as file:
                    file.truncate(sum(map(len, lines[:i])))
                break
            events.append(_parse(line))
        return events

    def append(self, event: Event) -> Event:
        """
        Append an event, and return it as the log holds it.

        The returned event is the one read back from its JSON, so values that
        JSON does not keep apart, such as a tuple and a list, are the same
        whether the event was just appended or read from a file.
        """
        line = _event.dump_json(event) + b'\n'
        stored = _parse(line)
        if self._path is not None:
            _append_line(self._path, line)
        self._events.append(stored)
        return stored

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

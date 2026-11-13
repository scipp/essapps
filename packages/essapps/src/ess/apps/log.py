# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workspace's history: an append-only log of what ran, with which params,
and what came of it, for one proposal, which each submission and opening names.

There are five events: a submission, a record that started, a record that
finished, an accumulator that opened, and a push into one. A record starts when
a worker begins its work, once what it reads is there, so a record that started
and did not finish was running when its workspace ended. A persist request is
part of a submission or a freeze, and its write is the record's: the record
finishes once the outputs are written. A freeze is a submission of one record of
the plain request of the state frozen, as the accumulator and its number of
pushes; the views list its request when it is read. The workspace's views,
such as the records by ID, are built by applying the events in order, when they
are appended and again when a workspace starts from an existing log.

An event names only records and accumulators appended before it: a start and
a finish name their record, a push its accumulator, a record the records it
reads, and the record of a freeze its accumulator. Applying the events in order
relies on this, and so does a restart, which walks the pending records
backwards.

A submission names the specs of its records by name and version. It carries,
in ``specs``, the digest (:attr:`ess.spec.WorkflowSpec.digest`) of each spec it
names that the log does not hold yet under that name and version, or holds with
another digest, keyed as ``"iofq/v1"``. So the digest of a record's spec is the
last one logged for its name and version before the record, and a log holds a
digest for the spec of every record. What the digest covers is part of the
format.

A log file starts with a header line that names its format, such as
``{"format": 3}``, and holds one event per line after it. A log reads only
files of its own format, :data:`FORMAT`, and leaves a file it refuses as it was.

Stages and accumulators are not history, and neither are output values or
anything a binding computed; those have their own lifetime.
A submission is one event, so a workspace that stops half-way through one has
all of it or none of it.
"""

from __future__ import annotations

import fcntl
import json
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
    One record of a submission, as the workspace accepted it.

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
    The record of a freeze: the record of the plain request of an
    accumulator's state, the accumulator's template with each table filled by
    the rows of its first ``pushes`` pushes. ``persist`` names the outputs the
    freeze persists.
    """

    id: str
    accumulator: str
    pushes: int
    outputs: tuple[str, ...]
    persist: tuple[str, ...] = ()


SpecDigests = dict[str, str]
"""The digests of specs by name and version, ``{"iofq/v1": "9f2c..."}``."""


class Submitted(BaseModel, frozen=True, extra='forbid'):
    """
    A submission or a freeze; ``specs`` holds the digests that the log does
    not hold yet of the specs its records name (see the module docstring).
    """

    kind: Literal['submitted'] = 'submitted'
    time: datetime
    proposal: str
    submitter: str
    specs: SpecDigests = Field(default_factory=dict)
    records: tuple[NewRecord | NewStateRecord, ...]


class Started(BaseModel, frozen=True, extra='forbid'):
    """A record whose work began on a worker: what it reads was there."""

    kind: Literal['started'] = 'started'
    record: str


class Finished(BaseModel, frozen=True, extra='forbid'):
    """
    A record that finished: ``failure`` says why it failed, or why the
    workspace cancelled it. ``omitted`` names the optional outputs that the
    workflow of a completed record did not return.
    """

    kind: Literal['finished'] = 'finished'
    record: str
    status: Status
    failure: str | None = None
    omitted: tuple[str, ...] = ()


class Opened(BaseModel, frozen=True, extra='forbid'):
    """
    An accumulator that opened, with its template as the workspace accepted it.

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


Event = Annotated[
    Submitted | Started | Finished | Opened | Pushed,
    Field(discriminator='kind'),
]
_event: TypeAdapter[Event] = TypeAdapter(Event)

# The format of a log file, named by its first line. Bumped on any change to
# the events, to what a spec's digest covers, or to the files of the store
# (ess.apps.hdf5), since stored values are found through the log.
FORMAT = 3
_HEADER = json.dumps({'format': FORMAT}).encode() + b'\n'


def _format(line: bytes) -> Any:
    """The format a header line names, or None if the line is no header."""
    try:
        header = json.loads(line)
    except ValueError:
        return None
    if isinstance(header, dict) and header.keys() == {'format'}:
        return header['format']
    return None


def _events(path: Path, lines: list[bytes]) -> list[Event]:
    """
    The events of ``lines`` of the log file ``path``, the first line being the
    header. Raises ``ValueError`` if the header is not the one of this format,
    or naming the first line that is not an event of this format, such as one
    with a field an event lacks, which validating would drop silently.
    """
    if not lines:
        return []
    # An integer only: JSON's true and 1.0 compare equal to 1 in Python.
    if type(found := _format(lines[0])) is not int or found != FORMAT:
        found_text = 'no format' if found is None else f'format {found}'
        raise ValueError(
            f'{path} is a log of {found_text}; this version reads format {FORMAT}'
        )

    def read(number: int, line: bytes) -> Event:
        try:
            return _event.validate_json(line)
        # A request refuses a field it lacks by its __init__, as a TypeError.
        except (ValidationError, TypeError) as error:
            raise ValueError(
                f'{path}, line {number}: not an event of a log of this format: {error}'
            ) from None

    return [read(number, line) for number, line in enumerate(lines[1:], start=2)]


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
    JSON event per line, after a first line that names the format of the file,
    ``{"format": FORMAT}``, written when the file is created. The header is
    written into an empty file, and into one that holds only the beginning of
    the header, cut short by a crash. A file whose first line is not the header
    of :data:`FORMAT`, or that has no complete line and holds anything else,
    raises ``ValueError`` and is left as it was.
    The log holds the file until :meth:`close`: a second log on the same
    file, in this process or another, is refused, so a file has one writer.
    The operating system lets go of the file when the process ends, so a log
    opened after a crash takes it over. The events
    already in the file are read first. A last line cut short, by a crash
    while it was written, is dropped from the file once the lines before it
    are read; any other line that is not an event of this format raises
    ``ValueError``. A write that fails, for example on a full disk, leaves the
    file as it was, so no later event follows a line cut short.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._file: FileIO | None = None
        self._events: list[Event] = []
        if path is not None:
            self._file = file = _hold(path)
            try:
                file.seek(0)
                data = file.readall()
                lines = _complete_lines(data)
                if not lines:
                    # Empty, or a header cut short by a crash while it was written.
                    if not _HEADER.startswith(data):
                        raise ValueError(
                            f'{path} is not a log: it has no complete header line'
                        )
                    file.truncate(0)
                    _append_line(file, _HEADER)
                    lines = [_HEADER]
                self._events = _events(path, lines)
                file.truncate(sum(map(len, lines)))
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workspace's history of one proposal: an append-only log of what ran, with
which params, and what came of it.

The log holds seven events: :class:`Submitted`, :class:`Started`,
:class:`Finished`, :class:`Environment`, :class:`Cleared`, :class:`Opened` and
:class:`Pushed`. Stages, output values, and anything a binding computed are not
in the log. The workspace builds its views, such as the records by ID, by
applying the events in order. It does so when an event is appended, and again
when a workspace starts from an existing log.

A record starts when a worker begins its work, once what it reads is there. So
a record that started and did not finish was running when its workspace ended.
A freeze is logged as a submission of one :class:`NewStateRecord`. Persisting is
part of a submission or a freeze, and the record finishes once its outputs are
written.

An event names only records and accumulators appended before it. Applying the
events in order relies on this, and so does a restart, which walks the pending
records backwards. A submission is one event, so a workspace that stops
half-way through one has all of it or none of it.

A submission names the specs of its records by name and version. Its ``specs``
holds the digest (:attr:`ess.spec.WorkflowSpec.digest`) of each spec that the
log does not hold yet under that name and version, or holds with another
digest. So the digest of a record's spec is the last one logged for its name
and version before the record.

A record that ran in the workspace's process finishes with the ID of the
process's :class:`Environment`. The log holds each environment once, appended
before the first record that finishes with it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from io import FileIO
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter, ValidationError

from .records import Params, Request, Status, Template, read_refs


class NewRecord(BaseModel, frozen=True, extra='forbid'):
    """
    One record of a submission, as the workspace accepted it.

    ``persist`` names the outputs persisted at submission.
    """

    id: str
    request: Request
    outputs: tuple[str, ...]
    label: str | None = None
    member: str | None = None
    persist: tuple[str, ...] = ()


class NewStateRecord(BaseModel, frozen=True, extra='forbid'):
    """
    The record of a freeze, of the plain request of the accumulator's state
    after its first ``pushes`` pushes.

    ``persist`` names the outputs that the freeze persists.
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
    A submission or a freeze.

    ``specs`` holds the digests of the specs that are new to the log (see the
    module docstring).
    """

    kind: Literal['submitted'] = 'submitted'
    time: datetime
    proposal: str
    submitter: str
    specs: SpecDigests = Field(default_factory=dict)
    records: tuple[NewRecord | NewStateRecord, ...]


class Started(BaseModel, frozen=True, extra='forbid'):
    """A record whose work began on a worker, once what it reads was there."""

    kind: Literal['started'] = 'started'
    record: str


class Finished(BaseModel, frozen=True, extra='forbid'):
    """
    A record that finished.

    ``failure`` is why it failed, or why the workspace cancelled it. ``omitted``
    names the optional outputs that the workflow of a completed record did not
    return. ``environment`` is the ID of the :class:`Environment` of the process
    that ran the record, or ``None`` for a record whose workflow never ended,
    such as a cancelled record or one that failed at a restart.
    """

    kind: Literal['finished'] = 'finished'
    record: str
    status: Status
    failure: str | None = None
    omitted: tuple[str, ...] = ()
    environment: str | None = None


class Environment(BaseModel, frozen=True, extra='forbid'):
    """
    The software of a process: its installed distributions as ``name==version``,
    and their digest as ``id``.
    """

    kind: Literal['environment'] = 'environment'
    id: str
    software: tuple[str, ...]

    @classmethod
    def of(cls, software: Sequence[str]) -> Environment:
        """The environment of ``software``, with its digest as ``id``."""
        digest = hashlib.sha256('\n'.join(software).encode()).hexdigest()
        return cls(id=digest, software=tuple(software))


class Cleared(BaseModel, frozen=True, extra='forbid'):
    """
    Finished records whose outputs the workspace dropped. It is appended before
    the outputs are deleted from the store.
    """

    kind: Literal['cleared'] = 'cleared'
    records: tuple[str, ...]


class Opened(BaseModel, frozen=True, extra='forbid'):
    """
    An accumulator that opened, with its template as the workspace accepted it.

    ``template`` holds the values as the client gave them, with file names
    resolved. The client gets this template back, as it does a stage's.
    ``params`` holds the fixed values with defaults filled in and inline values
    in JSON's types, and each blank table without rows. The driver is made
    from the fixed values. With the rows pushed, ``params`` makes the plain
    request of a state.
    """

    kind: Literal['opened'] = 'opened'
    accumulator: str
    proposal: str
    template: Template
    params: Params


class Pushed(BaseModel, frozen=True, extra='forbid'):
    """
    A push into an accumulator: rows of one or more tables in ``params``, by
    table, with file names resolved.
    """

    kind: Literal['pushed'] = 'pushed'
    accumulator: str
    params: Annotated[dict[str, list[Any]], BeforeValidator(read_refs)]


Event = Annotated[
    Submitted | Started | Finished | Environment | Cleared | Opened | Pushed,
    Field(discriminator='kind'),
]
_event: TypeAdapter[Event] = TypeAdapter(Event)

# The format of a log file, named by its first line. Bumped on any change to
# the events, to the identities of files that requests hold (ADR 0010), to
# what a spec's digest covers, or to the files of the store (ess.apps.hdf5),
# since stored values are found through the log.
FORMAT = 5
_HEADER = json.dumps({'format': FORMAT}).encode() + b'\n'


def _format(line: bytes) -> Any:
    """The format that a header line names, or ``None`` if it is not a header."""
    try:
        header = json.loads(line)
    except ValueError:
        return None
    if isinstance(header, dict) and header.keys() == {'format'}:
        return header['format']
    return None


def _events(path: Path, lines: list[bytes]) -> list[Event]:
    """
    The events of ``lines`` of the log file ``path``, the first being the header.

    Raises ``ValueError`` if the header does not name this format, or for the
    first line that is not an event of this format.
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
        # A request rejects an unknown field in its __init__, with a TypeError.
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
    """``path`` opened to read and append, locked against every other open."""
    file = path.open('a+b', buffering=0)
    try:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        file.close()
        raise RuntimeError(
            f'{path} is held by another process, or by another log of this one'
        ) from None
    return file


@contextmanager
def hold(path: Path) -> Iterator[None]:
    """
    Lock the log file ``path`` as a :class:`Log` does, without reading it.

    Raises ``RuntimeError`` if a log or another hold has it. Makes the file if
    there is none.
    """
    with _hold(path):
        yield


def _append_line(file: FileIO, line: bytes) -> None:
    """Appends ``line`` whole, or leaves the file as it was."""
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

    Without a path the log lives in memory. With a path it is a file with a
    header line, ``{"format": FORMAT}``, and one JSON event per line after it.
    Opening reads the events already in the file, and drops a last line cut
    short by a crash. A file that is not a log of this format raises
    ``ValueError`` and is left as it was.

    The log locks the file until :meth:`close`, so a file has one writer. The
    operating system releases the lock when the process ends, so a log opened
    after a crash takes the file over. A write that fails, for example on a
    full disk, leaves the file as it was.
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
        """Closes the file, if any, which releases its lock."""
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the backend knows, built from its log.

:meth:`Views.apply` is the only way the views change, and what it does
depends on nothing but the events. A backend that applies its log again
therefore has the views of the backend that wrote it. The backend answers
every query from the views, and reads the log only when it starts. So the
views may change between versions, while the events must stay readable as
long as a log is kept. What is not history, such as clients and their stages
and accumulators, and output values, the backend keeps elsewhere.

Every order the backend relies on lies within one proposal: labels are kept
by proposal, an accumulator and its pushes belong to one proposal, and a
record reads only records of its own proposal. So history may be stored, and
dropped, per proposal.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from .log import (
    Event,
    Finished,
    NewRecord,
    NewStateRecord,
    Opened,
    Persist,
    Pushed,
    Submitted,
    Written,
)
from .records import Record, Request, Status


class _Records(Mapping[str, Record]):
    """
    The records by ID, in the order submitted.

    A record of an accumulator's state is held as the accumulator and its
    number of pushes, and its request is listed from the accumulator's
    template and pushes each time it is read, so the views grow by a
    constant amount per read of an accumulator.
    """

    def __init__(self, views: Views) -> None:
        self._views = views
        self._held: dict[str, Record | tuple[NewStateRecord, Submitted]] = {}

    def add(self, held: Record | tuple[NewStateRecord, Submitted]) -> None:
        record_id = held.id if isinstance(held, Record) else held[0].id
        self._held[record_id] = held

    def outputs(self, record_id: str) -> tuple[str, ...]:
        """The names of a record's outputs, without listing its request."""
        held = self._held[record_id]
        return held.outputs if isinstance(held, Record) else held[0].outputs

    def __getitem__(self, record_id: str) -> Record:
        held = self._held[record_id]
        if isinstance(held, Record):
            return held
        new, event = held
        return Record(
            id=new.id,
            request=self._views.plain(new.accumulator, new.pushes),
            proposal=event.proposal,
            submitter=event.submitter,
            created=event.time,
            outputs=new.outputs,
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self._held)

    def __len__(self) -> int:
        return len(self._held)


PENDING: Literal['pending'] = 'pending'
"""The write of an output that a persist request names has not ended."""

WRITTEN: Literal['written'] = 'written'
"""
An output that a persist request names is in the store, or has no value to
write, since the workflow did not return it.
"""


@dataclass(frozen=True)
class WriteFailed:
    """The write of an output that a persist request names failed, and why."""

    reason: str


Write = Literal['pending', 'written'] | WriteFailed


class Views:
    """
    The records, labels, accumulators, pushes, and writes that the events
    describe.

    ``finished`` holds the event that finished a record; a record without one
    is pending. ``states`` holds the accumulator and number of pushes of each
    record of a state. ``accumulators`` holds the event that opened each
    accumulator, and ``pushes`` its pushes in order, each a row per table.

    ``persisted`` names the outputs each record persists at submission; their
    write is the record's: pending while it is, written once it completes.
    ``writes`` holds the write of each output that a persist request names
    later. :meth:`write` gives either.
    """

    def __init__(self) -> None:
        self.records = _Records(self)
        self.finished: dict[str, Finished] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.states: dict[str, tuple[str, int]] = {}
        self.accumulators: dict[str, Opened] = {}
        self.pushes: dict[str, list[Pushed]] = {}  # by accumulator ID
        self.persisted: dict[str, tuple[str, ...]] = {}
        self.writes: dict[tuple[str, str], Write] = {}  # by (record ID, output)

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    self._add(new, event)
            case Finished():
                self.finished[event.record] = event
                for key in self._pending(event.record):
                    if event.status is not Status.COMPLETED:
                        self.writes[key] = WriteFailed(f'the record {event.status}')
                    elif key[1] in event.omitted:
                        self.writes[key] = WRITTEN
            case Opened():
                self.accumulators[event.accumulator] = event
            case Pushed():
                self.pushes.setdefault(event.accumulator, []).append(event)
            case Persist():
                for name in event.outputs:
                    omitted = self.omitted((event.record, name))
                    self.writes[(event.record, name)] = WRITTEN if omitted else PENDING
            case Written():
                for name in event.outputs:
                    self.writes[(event.record, name)] = (
                        WRITTEN if event.failure is None else WriteFailed(event.failure)
                    )

    def _pending(self, record_id: str) -> list[tuple[str, str]]:
        return [
            (record_id, n)
            for n in self.records.outputs(record_id)
            if self.writes.get((record_id, n)) == PENDING
        ]

    def _add(self, new: NewRecord | NewStateRecord, event: Submitted) -> None:
        if isinstance(new, NewStateRecord):
            self.records.add((new, event))
            self.states[new.id] = (new.accumulator, new.pushes)
        else:
            self.records.add(
                Record(
                    id=new.id,
                    request=new.request,
                    proposal=event.proposal,
                    submitter=event.submitter,
                    created=event.time,
                    outputs=new.outputs,
                    label=new.label,
                    member=new.member,
                )
            )
            if new.label is not None:
                key = (event.proposal, new.label)
                self.labels.setdefault(key, []).append(new.id)
        if new.persist:
            self.persisted[new.id] = new.persist

    def plain(self, accumulator_id: str, pushes: int) -> Request:
        """
        The plain request of the accumulator's state after ``pushes`` pushes:
        its typed fixed values, with each table filled by the rows pushed into
        it, in push order.
        """
        opened = self.accumulators[accumulator_id]
        rows = [p.rows for p in self.pushes.get(accumulator_id, [])[:pushes]]
        tables = {t: [r[t] for r in rows if t in r] for t in opened.template.blanks}
        return Request(opened.template.spec, {**opened.fixed, **tables})

    def status(self, record_id: str) -> Status:
        finished = self.finished.get(record_id)
        return Status.PENDING if finished is None else finished.status

    def omitted(self, key: tuple[str, str]) -> bool:
        """Whether the record completed without the output, as optional."""
        finished = self.finished.get(key[0])
        return finished is not None and key[1] in finished.omitted

    def write(self, key: tuple[str, str]) -> Write | None:
        """
        The write of an output, or ``None`` if no persist request names it.

        One persisted at submission is pending while its record is, written
        once it completes, and failed with the record's failure otherwise.
        """
        if key[1] in self.persisted.get(key[0], ()):
            finished = self.finished.get(key[0])
            if finished is None:
                return PENDING
            if finished.status is Status.COMPLETED:
                return WRITTEN
            return WriteFailed(finished.failure or f'the record {finished.status}')
        return self.writes.get(key)

    def persists(self, key: tuple[str, str]) -> bool:
        """Whether the output is persisted: written, or its write pending."""
        return self.write(key) in (PENDING, WRITTEN)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the backend knows, built from its log.

:meth:`Views.apply` is the only way the views change, and what it does
depends on nothing but the events. A backend that applies its log again
therefore has the views of the backend that wrote it. What is not history,
such as clients and their stages and accumulators, and output values, the
backend keeps elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

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


class Views:
    """
    The records, labels, accumulators, pushes, and writes that the events
    describe.

    ``finished`` holds the event that finished a record; a record without one
    is pending. ``states`` holds the accumulator and number of pushes of each
    record of a state. ``accumulators`` holds the event that opened each
    accumulator, and ``pushes`` its pushes in order, each a row per table.

    Each output that a persist request names is in one of ``writing``, a
    write pending, ``written``, ``omitted``, written as not returned by the
    workflow, or ``unwritten``, with why its write failed. ``persisted``
    names the outputs each record persists at submission. A record that
    fails or is cancelled has no write pending.
    """

    def __init__(self) -> None:
        self.records = _Records(self)
        self.finished: dict[str, Finished] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.states: dict[str, tuple[str, int]] = {}
        self.accumulators: dict[str, Opened] = {}
        self.pushes: dict[str, list[Pushed]] = {}  # by accumulator ID
        self.persisted: dict[str, tuple[str, ...]] = {}
        self.writing: set[tuple[str, str]] = set()  # (record ID, output)
        self.written: set[tuple[str, str]] = set()
        self.omitted: set[tuple[str, str]] = set()
        self.unwritten: dict[tuple[str, str], str] = {}

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    self._add(new, event)
            case Finished():
                self.finished[event.record] = event
                if event.status is not Status.COMPLETED:
                    outputs = self.records[event.record].outputs
                    self.writing -= {(event.record, n) for n in outputs}
            case Opened():
                self.accumulators[event.accumulator] = event
            case Pushed():
                self.pushes.setdefault(event.accumulator, []).append(event)
            case Persist():
                for name in event.outputs:
                    self.unwritten.pop((event.record, name), None)
                    self.writing.add((event.record, name))
            case Written():
                for name in event.outputs:
                    key = (event.record, name)
                    self.writing.discard(key)
                    if event.failure is not None:
                        self.unwritten[key] = event.failure
                    elif name in event.omitted:
                        self.omitted.add(key)
                    else:
                        self.written.add(key)

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
            self.writing.update((new.id, n) for n in new.persist)

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

    def persists(self, key: tuple[str, str]) -> bool:
        """Whether the output is persisted: written, or its write pending."""
        return key in self.writing or key in self.written or key in self.omitted

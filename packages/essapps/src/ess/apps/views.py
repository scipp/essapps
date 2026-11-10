# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the workspace knows, built from its log.

:meth:`Views.apply` is the only way the views change, and what it does
depends on nothing but the events. A workspace that applies its log again
therefore has the views of the workspace that wrote it. The workspace answers
every query from the views, and reads the log only when it starts. So the
views may change between versions, while the events must stay readable as
long as a log is kept. What is not history, such as stages, accumulators, and
output values, the workspace keeps elsewhere.

A log, and so the views, hold the history of one proposal: the workspace's.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from .log import (
    Event,
    Finished,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    Submitted,
)
from .records import Changes, Record, Request, Status


class _Records(Mapping[str, Record]):
    """
    The records by ID, in the order submitted.

    The record of a freeze is held as the accumulator and its number of
    pushes, and its request is listed from the accumulator's template and
    pushes each time it is read, so the views grow by a constant amount per
    freeze.
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


class Views:
    """
    The records, labels, accumulators, and pushes that the events describe.

    ``finished`` holds the event that finished a record; a record without one
    is pending. ``states`` holds the accumulator and number of pushes of each
    record of a freeze. ``accumulators`` holds the event that opened each
    accumulator, and ``pushes`` its pushes in order, each rows of its tables.

    ``persisted`` names the outputs each record persists; their write is the
    record's: pending while it is, done once it completes.

    ``feed`` holds the ID of a record each time an event submits or finishes
    it, in the order of the events. A position in it is a
    cursor of :meth:`changes`. Opening an accumulator and pushing to it are not in
    the feed.
    """

    def __init__(self) -> None:
        self.records = _Records(self)
        self.finished: dict[str, Finished] = {}
        self.labels: dict[str, list[str]] = {}
        self.states: dict[str, tuple[str, int]] = {}
        self.accumulators: dict[str, Opened] = {}
        self.pushes: dict[str, list[Pushed]] = {}  # by accumulator ID
        self.persisted: dict[str, tuple[str, ...]] = {}
        self.feed: list[str] = []

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    self._add(new, event)
                    self.feed.append(new.id)
            case Finished():
                self.feed.append(event.record)
                self.finished[event.record] = event
            case Opened():
                self.accumulators[event.accumulator] = event
            case Pushed():
                self.pushes.setdefault(event.accumulator, []).append(event)

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
                self.labels.setdefault(new.label, []).append(new.id)
        if new.persist:
            self.persisted[new.id] = new.persist

    def plain(self, accumulator_id: str, pushes: int) -> Request:
        """
        The plain request of the accumulator's state after ``pushes`` pushes:
        its fixed values, with each blank table filled by the rows pushed
        into it, in push order.
        """
        opened = self.accumulators[accumulator_id]
        params = dict(opened.params)
        copied: set[str] = set()  # the tables copied from the opening's
        for push in self.pushes.get(accumulator_id, [])[:pushes]:
            for name, rows in push.params.items():
                if name not in copied:
                    params[name] = list(params[name])
                    copied.add(name)
                params[name].extend(rows)
        return Request(opened.template.spec, params=params)

    def status(self, record_id: str) -> Status:
        finished = self.finished.get(record_id)
        return Status.PENDING if finished is None else finished.status

    def omitted(self, key: tuple[str, str]) -> bool:
        """Whether the record completed without the output, as optional."""
        finished = self.finished.get(key[0])
        return finished is not None and key[1] in finished.omitted

    def persists(self, key: tuple[str, str]) -> bool:
        """Whether the record persists the output."""
        return key[1] in self.persisted.get(key[0], ())

    def changes(self, after: int) -> Changes:
        """
        The records that events changed since position ``after`` of the feed,
        each once, in the order of their first change.

        Raises ``ValueError`` if ``after`` is negative or past the end.
        """
        feed = self.feed
        if not 0 <= after <= len(feed):
            raise ValueError(
                f'after is a position in 0..{len(feed)} of the feed, not {after}'
            )
        return Changes(tuple(dict.fromkeys(feed[after:])), len(feed))

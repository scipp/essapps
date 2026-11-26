# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the workspace knows, built from its log.

:meth:`Views.apply` is the only way the views change, and it depends on
nothing but the events. So a workspace that applies its log again has the views
of the workspace that wrote it. The workspace looks up every query in the
views, and reads the log only when it starts. So the views may change between
versions, while the events must stay readable as long as a log exists.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from .log import (
    Cleared,
    Environment,
    Event,
    Finished,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    SpecDigests,
    Started,
    Submitted,
)
from .records import Changes, Record, RecordStatus, Request, SpecId, Status


class _Records(Mapping[str, Record]):
    """
    The records by ID, in the order submitted.

    The record of a freeze is held as the accumulator and its number of pushes.
    Its request is built from the accumulator's template and pushes each time
    it is read, so the views grow by a constant amount per freeze.
    """

    def __init__(self, views: Views) -> None:
        self._views = views
        self._held: dict[str, Record | tuple[NewStateRecord, Submitted]] = {}

    def add(self, held: Record | tuple[NewStateRecord, Submitted]) -> None:
        record_id = held.id if isinstance(held, Record) else held[0].id
        self._held[record_id] = held

    def outputs(self, record_id: str) -> tuple[str, ...]:
        """The names of a record's outputs, without building its request."""
        held = self._held[record_id]
        return held.outputs if isinstance(held, Record) else held[0].outputs

    def spec(self, record_id: str) -> SpecId:
        """The spec of a record, without building its request."""
        held = self._held[record_id]
        if isinstance(held, Record):
            return held.spec
        return self._views.accumulators[held[0].accumulator].template.spec

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

    ``finished`` holds the event that finished each record. A record without
    one is pending. ``states`` holds the accumulator and number of pushes of the
    record of each freeze. ``persisted`` holds the names of the outputs that each
    record persists. Their write is part of the record: pending while the record
    is, done once it completes. ``spec_digests`` holds the digest last logged for
    each spec, and ``digests`` the digest of each record's spec, the last logged
    before the record. ``environments`` holds the software of each environment
    by ID. ``cleared`` holds the records whose outputs are cleared.

    ``feed`` holds the ID of a record each time an event submits, finishes or
    clears it, in event order. A position in it is a cursor of :meth:`changes`.
    """

    def __init__(self) -> None:
        self.records = _Records(self)
        self.started: set[str] = set()
        self.finished: dict[str, Finished] = {}
        self.labels: dict[str, list[str]] = {}
        self.states: dict[str, tuple[str, int]] = {}
        self.accumulators: dict[str, Opened] = {}
        self.pushes: dict[str, list[Pushed]] = {}  # by accumulator ID
        self.persisted: dict[str, tuple[str, ...]] = {}
        self.spec_digests: SpecDigests = {}
        self.digests: dict[str, str] = {}  # by record ID
        self.environments: dict[str, tuple[str, ...]] = {}
        self.cleared: set[str] = set()
        self.feed: list[str] = []

    def apply(self, event: Event) -> None:
        # Only a submission and the environment of a finish are checked. A
        # start, finish or push that names an unknown record or accumulator is
        # applied as it is.
        match event:
            case Submitted():
                self.spec_digests.update(event.specs)
                for new in event.records:
                    self._add(new, event)
                    self.feed.append(new.id)
            case Started():
                self.started.add(event.record)
            case Finished():
                if (
                    event.environment is not None
                    and event.environment not in self.environments
                ):
                    raise ValueError(
                        f'the finish of record {event.record} names environment '
                        f'{event.environment}, which the log does not hold'
                    )
                self.feed.append(event.record)
                self.finished[event.record] = event
            case Environment():
                self.environments[event.id] = event.software
            case Cleared():
                self.cleared.update(event.records)
                self.feed.extend(event.records)
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
        spec = str(self.records.spec(new.id))
        if spec not in self.spec_digests:
            raise ValueError(
                f'the submission of record {new.id} names spec {spec}, of which '
                'the log holds no digest'
            )
        self.digests[new.id] = self.spec_digests[spec]
        if new.persist:
            self.persisted[new.id] = new.persist

    def new_digests(self, digests: Mapping[SpecId, str]) -> SpecDigests:
        """
        The ``digests`` that the log does not hold, or holds with another digest.

        An event that refers to these specs carries their digests.
        """
        return {
            str(s): d for s, d in digests.items() if self.spec_digests.get(str(s)) != d
        }

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

    def record_status(self, record_id: str) -> RecordStatus:
        """
        The status of a record, why it failed, the software that ran it, and
        whether its outputs are cleared.
        """
        finished = self.finished.get(record_id)
        if finished is None:
            return RecordStatus(Status.PENDING)
        environment = finished.environment
        return RecordStatus(
            finished.status,
            finished.failure,
            None if environment is None else self.environments[environment],
            cleared=record_id in self.cleared,
        )

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

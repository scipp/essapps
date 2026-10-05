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

from .log import Event, Finished, Opened, Pushed, Submitted
from .records import Record, Status


class Views:
    """
    The records, labels, accumulators, and pushes that the events describe.

    ``finished`` holds the event that finished a record; a record without one
    is pending. ``accumulators`` holds the event that opened each accumulator,
    and ``pushes`` its pushes in order, each a table and a row; a record that
    read it after ``upto`` pushes read the first ``upto``.
    """

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}
        self.finished: dict[str, Finished] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.accumulators: dict[str, Opened] = {}
        self.pushes: dict[str, list[Pushed]] = {}  # by accumulator ID

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    self.records[new.id] = Record(
                        id=new.id,
                        request=new.request,
                        proposal=event.proposal,
                        submitter=event.submitter,
                        created=event.time,
                        outputs=new.outputs,
                        label=new.label,
                        member=new.member,
                    )
                    if new.label is not None:
                        key = (event.proposal, new.label)
                        self.labels.setdefault(key, []).append(new.id)
            case Finished():
                self.finished[event.record] = event
            case Opened():
                self.accumulators[event.accumulator] = event
            case Pushed():
                self.pushes.setdefault(event.accumulator, []).append(event)

    def status(self, record_id: str) -> Status:
        finished = self.finished.get(record_id)
        return Status.PENDING if finished is None else finished.status

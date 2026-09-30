# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the backend knows, built from its log.

:meth:`Views.apply` is the only way the views change, and what it does
depends on nothing but the events. A backend that applies its log again
therefore has the views of the backend that wrote it. What is not history,
such as output values, staged callables, and an accumulator's held value,
the backend keeps elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .log import (
    AccumulatorOpened,
    Event,
    Finished,
    Pushed,
    SessionClosed,
    SessionOpened,
    StageOpened,
    Submitted,
)
from .records import Element, Failure, Read, Record, SpecId, Status


@dataclass(frozen=True)
class StageView:
    session: str
    proposal: str
    spec: SpecId
    blanks: tuple[str, ...]


@dataclass
class AccumulatorView:
    """
    An accumulator's history: its elements, and how many of them are done.

    ``elements`` only grows, so the record of a read keeps it and uses the
    first ``upto``. ``ready`` counts the leading elements whose records have
    all completed, and ``failed`` is the position of the first element whose
    record did not complete.
    """

    session: str
    proposal: str
    spec: SpecId
    open: bool = True
    elements: list[Element] = field(default_factory=list)
    ready: int = 0
    failed: int | None = None

    def fail(self, position: int) -> None:
        self.failed = position if self.failed is None else min(self.failed, position)


class Views:
    """
    The records, labels, sessions, and holders that the events describe.

    ``elements_of`` maps an unfinished record to the accumulators and
    positions at which it was pushed.
    """

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.sessions: dict[str, str] = {}  # open session ID to proposal
        self.stages: dict[str, StageView] = {}  # the stages of open sessions
        self.accumulators: dict[str, AccumulatorView] = {}
        self.elements_of: dict[str, list[tuple[str, int]]] = {}

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    record = Record(
                        id=new.id,
                        submitted=new.submitted,
                        proposal=event.proposal,
                        submitter=event.submitter,
                        created=event.time,
                        outputs=new.outputs,
                        label=new.label,
                        member=new.member,
                    )
                    if isinstance(new.submitted, Read):
                        accumulator = self.accumulators[new.submitted.accumulator]
                        record = record.with_elements(accumulator.elements)
                    self.records[new.id] = record
                    if new.label is not None:
                        key = (event.proposal, new.label)
                        self.labels.setdefault(key, []).append(new.id)
            case Finished():
                failure = (
                    None if event.failure is None else Failure(message=event.failure)
                )
                self.records[event.record] = self.records[event.record].model_copy(
                    update={'status': event.status, 'failure': failure}
                )
                for accumulator_id, position in self.elements_of.pop(event.record, ()):
                    accumulator = self.accumulators[accumulator_id]
                    if event.status is Status.COMPLETED:
                        self._advance(accumulator)
                    else:
                        accumulator.fail(position)
            case SessionOpened():
                self.sessions[event.session] = event.proposal
            case SessionClosed():
                del self.sessions[event.session]
                self.stages = {
                    i: s for i, s in self.stages.items() if s.session != event.session
                }
                for accumulator in self.accumulators.values():
                    if accumulator.session == event.session:
                        accumulator.open = False
            case StageOpened():
                self.stages[event.stage] = StageView(
                    event.session,
                    self.sessions[event.session],
                    event.spec,
                    event.blanks,
                )
            case AccumulatorOpened():
                self.accumulators[event.accumulator] = AccumulatorView(
                    event.session, self.sessions[event.session], event.spec
                )
            case Pushed():
                accumulator = self.accumulators[event.accumulator]
                position = len(accumulator.elements)
                accumulator.elements.append(event.element)
                for record_id in {ref.record for ref in event.element.values()}:
                    if not self.records[record_id].status.finished:
                        self.elements_of.setdefault(record_id, []).append(
                            (event.accumulator, position)
                        )
                self._advance(accumulator)

    def _advance(self, accumulator: AccumulatorView) -> None:
        elements = accumulator.elements
        while accumulator.ready < len(elements) and all(
            self.records[ref.record].status is Status.COMPLETED
            for ref in elements[accumulator.ready].values()
        ):
            accumulator.ready += 1

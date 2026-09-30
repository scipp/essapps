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
from .records import Element, Failure, Record, Snapshot, SpecId


@dataclass(frozen=True)
class StageView:
    session: str
    proposal: str
    spec: SpecId
    blanks: tuple[str, ...]


@dataclass
class AccumulatorView:
    """
    An accumulator's history: its elements in push order.

    ``elements`` only grows, so the record of a snapshot keeps it and uses the
    first ``upto``.
    """

    session: str
    proposal: str
    spec: SpecId
    open: bool = True
    elements: list[Element] = field(default_factory=list)


class Views:
    """The records, labels, sessions, and holders that the events describe."""

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.sessions: dict[str, str] = {}  # open session ID to proposal
        self.stages: dict[str, StageView] = {}  # the stages of open sessions
        self.accumulators: dict[str, AccumulatorView] = {}

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
                    if isinstance(new.submitted, Snapshot):
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
                self.accumulators[event.accumulator].elements.append(event.element)

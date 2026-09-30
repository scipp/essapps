# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the backend knows, built from its log.

:meth:`Views.apply` is the only way the views change, and what it does
depends on nothing but the events. A backend that applies its log again
therefore has the views of the backend that wrote it. What is not history,
such as sessions and their holders, output values, and an accumulator's held value,
the backend keeps elsewhere.
"""

from __future__ import annotations

from .log import Event, Finished, Pushed, Submitted
from .records import Element, Failure, Record


class Views:
    """
    The records, labels, and accumulator elements that the events describe.

    ``elements`` holds each accumulator's elements in push order; a snapshot
    of it covers the first ``upto``.
    """

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}
        self.labels: dict[tuple[str, str], list[str]] = {}  # (proposal, label)
        self.elements: dict[str, list[Element]] = {}  # accumulator ID to elements

    def apply(self, event: Event) -> None:
        match event:
            case Submitted():
                for new in event.records:
                    self.records[new.id] = Record(
                        id=new.id,
                        submitted=new.submitted,
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
                failure = (
                    None if event.failure is None else Failure(message=event.failure)
                )
                self.records[event.record] = self.records[event.record].model_copy(
                    update={'status': event.status, 'failure': failure}
                )
            case Pushed():
                self.elements.setdefault(event.accumulator, []).append(event.element)

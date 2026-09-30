# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules and the trigger loop: automatic reduction of new datasets.

A rule is plain data. The trigger loop is a driver: it runs next to the client,
never in the backend, and keeps no memory of its own. Which datasets a rule
has handled is read from the records under the rule's label, so a restarted
or replaced loop picks up where the last one stopped. A dataset whose record
failed counts as handled; running it again is the user's decision.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field

from ess.reduce.spec import DatasetRef

from .batch import Lookup, apply, dataset_blank, run_number
from .client import Client
from .datasets import DatasetSource, Selector
from .records import Record, Request, SubmitError, Template


@dataclass(frozen=True)
class Rule:
    """
    Apply a template to each new dataset the selector matches.

    With ``series``, a request instead takes every dataset with the same value
    of that metadata field so far, in run order, and that value is its member.
    """

    name: str
    template: Template
    selector: Selector
    label: str
    member_field: str = 'run'
    lookup: Lookup | None = None
    series: str | None = None


@dataclass
class RuleStatus:
    """A rule's last step: what it submitted, or the reason it submitted nothing."""

    submitted: list[Record] = field(default_factory=list)
    reason: str | None = None


class TriggerLoop:
    def __init__(
        self, client: Client, source: DatasetSource, rules: list[Rule]
    ) -> None:
        self._client = client
        self._source = source
        self._rules = rules
        self._status = {rule.name: RuleStatus() for rule in rules}

    def step(self) -> list[Record]:
        """Submit what arrived since the last step; return the records submitted."""
        submitted: list[Record] = []
        for rule in self._rules:
            status = self._status[rule.name] = RuleStatus()
            requests = self._requests(rule)
            if not requests:
                status.reason = 'no new dataset'
                continue
            try:
                records = self._client.submit(requests, label=rule.label)
            except (SubmitError, LookupError, ValueError) as error:
                status.reason = str(error)
                continue
            status.submitted = list(records.values())
            submitted.extend(status.submitted)
        return submitted

    def run(self, interval: float = 1.0) -> None:
        """Step forever."""
        while True:
            self.step()
            time.sleep(interval)

    def status(self, rule: Rule) -> RuleStatus:
        return self._status[rule.name]

    def _requests(self, rule: Rule) -> dict[str, Request]:
        handled = {
            ref
            for record in self._client.records(label=rule.label)
            for ref in record.request.datasets()
        }
        matching = self._source.list(rule.selector)
        new = [ref for ref in matching if ref not in handled]
        if rule.series is None:
            return apply(
                rule.template,
                new,
                self._source,
                member_field=rule.member_field,
                lookup=rule.lookup,
            )
        return self._series_requests(rule, matching, new)

    def _series_requests(
        self, rule: Rule, matching: list[DatasetRef], new: list[DatasetRef]
    ) -> dict[str, Request]:
        series: dict[str, list[DatasetRef]] = defaultdict(list)
        for ref in matching:
            series[str(self._source.metadata(ref)[rule.series])].append(ref)
        blank = dataset_blank(rule.template, None)
        return {
            value: rule.template.fill(
                {blank: sorted(refs, key=lambda r: run_number(r, self._source))}
            )
            for value, refs in series.items()
            if any(ref in new for ref in refs)
        }

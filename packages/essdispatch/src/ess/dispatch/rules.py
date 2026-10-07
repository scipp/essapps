# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules and the trigger loop: automatic reduction of new datasets.

A rule is plain data. The trigger loop is a driver: it uses a client, never
runs in the backend, and keeps no memory of its own. It persists what it
submits, so its client keeps nothing, and any client of the proposal reads the
results. It finds new datasets
through the client. Which datasets a rule has handled is read from the records
under the rule's label, so a restarted or replaced loop picks up where the last
one stopped. The label belongs to the rule: any record under it counts,
whoever submitted it and whether or not it failed. So the loop does not retry a
failed dataset, which could fail again at every step; running it again is the
user's decision.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from ess.spec import DatasetRef

from .batch import Lookup, dataset_blank, run_number, try_apply
from .client import Client
from .datasets import Selector
from .records import Record, Request, SubmitError, Template


@dataclass(frozen=True)
class Rule:
    """
    Apply a template to each new dataset the selector matches.

    With ``series``, a request instead takes every dataset with the same value
    of that metadata field so far, in run order, and that value is its member.
    The blank is then a table, and each dataset is a row of it, in the row's
    field ``row_field``.

    ``persist`` names the outputs the loop persists, or is ``None`` for every
    one; the loop always persists, so that its client keeps nothing. What the
    template references must be persisted for the same reason.
    """

    name: str
    template: Template
    selector: Selector
    label: str
    member_field: str = 'run'
    lookup: Lookup | None = None
    series: str | None = None
    row_field: str = 'run'
    persist: tuple[str, ...] | None = None


@dataclass
class RuleStatus:
    """
    A rule's last step.

    ``reason`` says why the rule submitted nothing, or which datasets it
    skipped and why; it is ``None`` if the rule submitted a request for each
    new dataset.
    """

    reason: str | None = None


class TriggerLoop:
    def __init__(self, client: Client, rules: list[Rule]) -> None:
        self._client = client
        self._source = client.datasets
        self._rules = rules
        self._status = {rule.name: RuleStatus() for rule in rules}

    def step(self) -> list[Record]:
        """
        Submit what arrived since the last step; return the records submitted.

        A dataset whose request cannot be made, such as a sample with no can
        measured before it, is skipped: no record names it, so the next step
        tries it again. A rule whose requests cannot be made as a whole or are
        refused submits nothing. The rule's status says why; the other
        datasets and rules still submit.
        """
        submitted: list[Record] = []
        for rule in self._rules:
            reasons: list[str] = []
            try:
                requests, skipped = self._requests(rule)
                reasons += [f'skipped {r.dataset}: {e}' for r, e in skipped.items()]
                if requests:
                    records = self._client.submit(
                        requests,
                        label=rule.label,
                        persist=True if rule.persist is None else rule.persist,
                    )
                    submitted.extend(records.values())
                elif not skipped:
                    reasons.append('no new dataset')
            except (SubmitError, LookupError, ValueError) as error:
                reasons.append(str(error))
            self._status[rule.name] = RuleStatus('; '.join(reasons) or None)
        return submitted

    def run(self, interval: float = 1.0) -> None:
        """Step forever."""
        while True:
            self.step()
            time.sleep(interval)

    def status(self, rule: Rule) -> RuleStatus:
        return self._status[rule.name]

    def _requests(
        self, rule: Rule
    ) -> tuple[dict[str, Request], dict[DatasetRef, LookupError]]:
        """The requests for the new datasets, and the datasets skipped, with why."""
        handled = {
            ref
            for record in self._client.records(label=rule.label)
            for ref in record.request.datasets()
        }
        matching = self._source.list(rule.selector)
        new = [ref for ref in matching if ref not in handled]
        if rule.series is None:
            return try_apply(
                rule.template,
                new,
                self._source,
                member_field=rule.member_field,
                lookup=rule.lookup,
            )
        return self._series_requests(rule, matching, new), {}

    def _series_requests(
        self, rule: Rule, matching: list[DatasetRef], new: list[DatasetRef]
    ) -> dict[str, Request]:
        series: dict[str, list[DatasetRef]] = defaultdict(list)
        for ref in sorted(matching, key=lambda r: run_number(r, self._source)):
            series[str(self._source.metadata(ref)[rule.series])].append(ref)
        blank = dataset_blank(rule.template, None)
        return {
            value: rule.template.fill({blank: [{rule.row_field: r} for r in refs]})
            for value, refs in series.items()
            if any(ref in new for ref in refs)
        }

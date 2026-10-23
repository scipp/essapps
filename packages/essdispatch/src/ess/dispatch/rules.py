# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules and the trigger loop: automatic reduction of new datasets.

A rule is plain data. The trigger loop is a controller: it uses a client, never
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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ess.spec import DatasetRef
from ess.spec.refs import Loc, iter_refs

from .batch import Lookup, dataset_place, holding, run_number, try_apply
from .client import Client
from .datasets import Selector
from .errors import SubmitError
from .records import Record, Request, Template


@dataclass(frozen=True)
class Rule:
    """
    Apply a template to each new dataset the selector matches.

    A dataset fills the place ``at`` in the blank the lookup leaves, as
    :func:`apply` fills it: a file field of the blank, a group, such as
    ``('sample', 'file')``, and ``fields`` gives the blank's other fields, as
    :func:`apply` takes them; for a series, of each row. A dataset is new
    until a record under the label holds it at that place: a dataset that a
    request holds elsewhere, such as a can that the lookup fills, is not
    handled by it.

    With ``series``, a request instead takes every dataset with the same
    value of that metadata field so far, in run order, and that value is its
    member. The blank is then a table with a row for each dataset, and the
    lookup is not used. ``at`` names the place in each row, the blank and
    the fields within a row down to the file: ``at=('runs', 'file')`` makes
    the rows ``[{'file': dataset}, ...]``. A dataset is new until a record
    under the label holds it at that place in a row, ``runs[i].file``.

    ``persist`` names the outputs the loop persists, or is ``None`` for every
    one; the loop always persists, so that its client keeps nothing. What the
    template references must be persisted for the same reason.

    Raises ``ValueError`` when made if no single blank is left for a dataset,
    if ``at`` is not in it (see :func:`dataset_place`), or if ``fields``
    gives the place ``at`` (see :func:`~ess.dispatch.batch.holding`).
    """

    name: str
    template: Template
    selector: Selector
    label: str
    at: tuple[str, ...]
    fields: Mapping[str, Any] | None = None
    member_field: str = 'run'
    lookup: Lookup | None = None
    series: str | None = None
    persist: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        holding(self.place, None, self.fields)  # refuses an at that none fills

    @property
    def place(self) -> tuple[str, ...]:
        """The place a dataset fills, in a row of the blank for a series."""
        lookup = self.lookup if self.series is None else None
        return dataset_place(self.template, lookup, self.at)


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
        place = rule.place
        handled = {
            ref
            for record in self._client.records(label=rule.label)
            for loc, ref in iter_refs(record.request.params)
            if isinstance(ref, DatasetRef) and _fills(loc, place, rule.series)
        }
        matching = self._source.list(rule.selector)
        new = [ref for ref in matching if ref not in handled]
        if rule.series is None:
            return try_apply(
                rule.template,
                new,
                self._source,
                at=place,
                fields=rule.fields,
                member_field=rule.member_field,
                lookup=rule.lookup,
            )
        return self._series_requests(rule, place, matching, new), {}

    def _series_requests(
        self,
        rule: Rule,
        place: tuple[str, ...],
        matching: list[DatasetRef],
        new: list[DatasetRef],
    ) -> dict[str, Request]:
        series: dict[str, list[DatasetRef]] = defaultdict(list)
        for ref in sorted(matching, key=lambda r: run_number(r, self._source)):
            series[str(self._source.metadata(ref)[rule.series])].append(ref)
        return {
            value: rule.template.fill(
                params={place[0]: [holding(place, ref, rule.fields) for ref in refs]}
            )
            for value, refs in series.items()
            if any(ref in new for ref in refs)
        }


def _fills(loc: Loc, place: tuple[str, ...], series: str | None) -> bool:
    """
    Whether a dataset at ``loc`` in a request of a rule fills the rule's
    ``place``: is at it, or for a series, at it in a row of the blank table,
    such as ``('runs', 2, 'file')`` for the place ``('runs', 'file')``.
    """
    if series is None:
        return loc == place
    blank, *row = place
    return (
        len(loc) == len(place) + 1
        and loc[0] == blank
        and isinstance(loc[1], int)
        and list(loc[2:]) == row
    )

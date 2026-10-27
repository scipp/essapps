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
from dataclasses import dataclass

from pydantic import BaseModel

from ess.spec import DatasetRef, field_types
from ess.spec.refs import Loc, iter_refs

from .batch import Lookup, dataset_place, run_number, try_apply
from .client import Client
from .datasets import Selector
from .errors import SubmitError
from .records import Record, Request, Template


@dataclass(frozen=True)
class Rule:
    """
    Apply a template to each new dataset the selector matches.

    A dataset fills the blank ``at`` that the lookup leaves, a parameter of
    a dataset model, as :func:`apply` fills it, such as ``'sample'``. A
    dataset is new until a record under the label holds it there: a dataset
    that a request holds elsewhere, such as a can that the lookup fills, is
    not handled by it.

    With ``series``, a request instead takes every dataset with the same
    value of that metadata field so far, in run order, and that value is its
    member. The blank ``at`` is then a table of a dataset model, with a row
    for each dataset, and the lookup is not used: ``at='runs'`` makes the
    rows ``[dataset, ...]``. A dataset is new until a record under the label
    holds it in a row, ``runs[i]``.

    ``persist`` names the outputs the loop persists, or is ``None`` for every
    one; the loop always persists, so that its client keeps nothing. What the
    template references must be persisted for the same reason.

    Raises ``ValueError`` when made if no single blank is left for a dataset,
    or if ``at`` is not that blank (see :func:`dataset_place`).
    """

    name: str
    template: Template
    selector: Selector
    label: str
    at: str
    member_field: str = 'run'
    lookup: Lookup | None = None
    series: str | None = None
    persist: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        _ = self.place  # refuses an at that no dataset fills

    @property
    def place(self) -> str:
        """The blank a dataset fills, a row of it for a series."""
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
    """
    Steps the ``rules`` on ``client``. Raises ``ValueError`` for a rule of a
    spec the backend runs that could not submit what it finds (see
    :func:`_check`); a rule of a spec it does not run submits nothing, with
    the reason in its status.
    """

    def __init__(self, client: Client, rules: list[Rule]) -> None:
        for rule in rules:
            try:
                spec = client.spec(rule.template.spec)
            except KeyError:
                continue
            _check(rule, spec.params)
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
                member_field=rule.member_field,
                lookup=rule.lookup,
            )
        return self._series_requests(rule, place, matching, new), {}

    def _series_requests(
        self,
        rule: Rule,
        place: str,
        matching: list[DatasetRef],
        new: list[DatasetRef],
    ) -> dict[str, Request]:
        series: dict[str, list[DatasetRef]] = defaultdict(list)
        for ref in sorted(matching, key=lambda r: run_number(r, self._source)):
            series[str(self._source.metadata(ref)[rule.series])].append(ref)
        return {
            value: rule.template.fill(params={place: refs})
            for value, refs in series.items()
            if any(ref in new for ref in refs)
        }


def _check(rule: Rule, params: type[BaseModel]) -> None:
    """
    Refuse ``rule``, of a spec with the params model ``params``, if a dataset
    it lists could not fill its blank: the blank ``at``, and each blank of
    its lookup, is a parameter of a dataset model, a list one if and only if
    the rule has ``series``, and the rule's or the lookup's selector names
    each key and value of the model's metadata, so that every dataset it
    lists fits the model (see :class:`~ess.spec.DatasetModel`).
    """
    selectors = {rule.place: rule.selector}
    if rule.lookup is not None and rule.series is None:
        selectors |= {b: r.selector for b, r in rule.lookup.rules.items()}
    for blank, selector in selectors.items():
        series = blank == rule.place and rule.series is not None
        listed = field_types.is_list(params, blank)
        if series and not listed:
            raise ValueError(
                f'{rule.name}: a series fills a list parameter, not {blank}'
            )
        if listed and not series:
            fills = 'a series' if blank == rule.place else 'no lookup'
            raise ValueError(
                f'{rule.name}: {blank} is a list parameter; {fills} fills it'
            )
        model = field_types.value_type(params.model_fields[blank].annotation)
        if not field_types.is_dataset(model):
            raise ValueError(
                f'{rule.name}: {blank} is not a parameter of a dataset model'
            )
        need = Selector(**model.metadata).fields
        if any(selector.fields.get(k, _UNSET) != v for k, v in need.items()):
            raise ValueError(
                f'{rule.name}: {selector} lists datasets that do not fit '
                f'{model.__name__} at {blank}; a selector names each of {need}'
            )


_UNSET = object()


def _fills(loc: Loc, place: str, series: str | None) -> bool:
    """
    Whether a dataset at ``loc`` in a request of a rule fills the rule's
    ``place``: is it, or for a series, a row of it, such as ``('runs', 2)``
    for the place ``'runs'``.
    """
    if series is None:
        return loc == (place,)
    return len(loc) == 2 and loc[0] == place and isinstance(loc[1], int)

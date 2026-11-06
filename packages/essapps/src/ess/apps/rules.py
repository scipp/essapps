# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules and the trigger loop: automatic reduction of new files, as described in
``docs/developer/automatic-reduction.md``.

A rule is plain data. The trigger loop is a controller of a client and never
runs in the backend. Which files a rule has handled comes from the records under
the rule's label. The loop reads them from the proposal's changes
(:meth:`~ess.apps.Client.changes`), from the start when it starts, so it holds
no more than a position in them and the files it has seen.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass

from pydantic import BaseModel

from ess.spec import FileRef, field_types
from ess.spec.refs import Loc, iter_refs

from .batch import Lookup, check_at, run_number, try_apply
from .client import Client
from .errors import SubmitError
from .files import Selector
from .records import Record, Request, Template


@dataclass(frozen=True)
class Rule:
    """
    Apply a template to each new file the selector matches.

    A file fills the blank ``at`` that the lookup leaves, as :func:`apply` fills
    it. With ``series``, a request instead takes every file with the same value of
    that metadata field so far, in run order, and ``at`` is then a table of files.

    ``persist`` names the outputs the loop persists, or is ``None`` for every one.
    What the template references must be persisted too, since the loop's client
    keeps nothing.

    Raises ``ValueError`` when made if no single blank is left for a file,
    or if ``at`` is not that blank (see :func:`check_at`).
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
        lookup = self.lookup if self.series is None else None
        check_at(self.template, lookup, self.at)


@dataclass
class RuleStatus:
    """
    A rule's last step.

    ``reason`` says why the rule submitted nothing, or which files it
    skipped and why; it is ``None`` if the rule submitted a request for each
    new file.
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
        self._source = client.files
        self._rules = rules
        self._cursor = 0
        self._handled: dict[str, set[FileRef]] = {rule.name: set() for rule in rules}
        self._status = {rule.name: RuleStatus() for rule in rules}

    def step(self) -> list[Record]:
        """
        Submit what arrived since the last step; return the records submitted.

        A file whose request cannot be made, such as a sample with no can
        measured before it, is skipped: no record names it, so the next step
        tries it again. A rule whose requests cannot be made as a whole or are
        refused submits nothing. The rule's status says why; the other
        files and rules still submit.
        """
        submitted: list[Record] = []
        for rule in self._rules:
            reasons: list[str] = []
            try:
                requests, skipped = self._requests(rule)
                reasons += [f'skipped {r.file}: {e}' for r, e in skipped.items()]
                if requests:
                    records = self._client.submit(
                        requests,
                        label=rule.label,
                        persist=True if rule.persist is None else rule.persist,
                    )
                    submitted.extend(records.values())
                elif not skipped:
                    reasons.append('no new file')
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
    ) -> tuple[dict[str, Request], dict[FileRef, LookupError]]:
        """The requests for the new files, and the files skipped, with why."""
        self._read()
        handled = self._handled[rule.name]
        matching = self._source.list(rule.selector)
        new = [ref for ref in matching if ref not in handled]
        if rule.series is None:
            return try_apply(
                rule.template,
                new,
                self._source,
                at=rule.at,
                member_field=rule.member_field,
                lookup=rule.lookup,
            )
        return self._series_requests(rule, matching, new), {}

    def _read(self) -> None:
        """Take the files of the records that changed into each rule's handled."""
        changes = self._client.changes(after=self._cursor)
        self._cursor = changes.cursor
        for record in self._client.records(ids=changes.records):
            for rule in self._rules:
                if record.label == rule.label:
                    self._handled[rule.name] |= {
                        ref
                        for loc, ref in iter_refs(record.request.params)
                        if isinstance(ref, FileRef)
                        and _fills(loc, rule.at, rule.series)
                    }

    def _series_requests(
        self,
        rule: Rule,
        matching: list[FileRef],
        new: list[FileRef],
    ) -> dict[str, Request]:
        series: dict[str, list[FileRef]] = defaultdict(list)
        for ref in sorted(matching, key=lambda r: run_number(r, self._source)):
            series[str(self._source.metadata(ref)[rule.series])].append(ref)
        return {
            value: rule.template.fill(params={rule.at: refs})
            for value, refs in series.items()
            if any(ref in new for ref in refs)
        }


def _check(rule: Rule, params: type[BaseModel]) -> None:
    """
    Refuse ``rule``, of a spec with the params model ``params``, if a file
    it lists could not fill its blank: the blank ``at``, and each blank of
    its lookup, is a file parameter, a file field or a list of one (see
    :class:`~ess.spec.File`), a list one if and only if the rule has
    ``series``, and the rule's or the lookup's selector names each key and
    value of the marker's metadata, so that every file it lists fits the
    field.
    """
    selectors = {rule.at: rule.selector}
    if rule.lookup is not None and rule.series is None:
        selectors |= {b: r.selector for b, r in rule.lookup.rules.items()}
    for blank, selector in selectors.items():
        series = blank == rule.at and rule.series is not None
        listed = field_types.is_list(params, blank)
        if series and not listed:
            raise ValueError(
                f'{rule.name}: a series fills a list parameter, not {blank}'
            )
        if listed and not series:
            fills = 'a series' if blank == rule.at else 'no lookup'
            raise ValueError(
                f'{rule.name}: {blank} is a list parameter; {fills} fills it'
            )
        if (marker := field_types.file_of(params.model_fields[blank])) is None:
            raise ValueError(f'{rule.name}: {blank} is not a file parameter')
        need = Selector.of(marker).fields
        if not need.items() <= selector.fields.items():
            raise ValueError(
                f'{rule.name}: {selector} lists files that do not fit {blank}; a '
                f'selector names each of {need}'
            )


def _fills(loc: Loc, at: str, series: str | None) -> bool:
    """
    Whether a file at ``loc`` in a request of a rule fills the rule's
    ``at``: is it, or for a series, a row of it, such as ``('runs', 2)``
    for ``at='runs'``.
    """
    if series is None:
        return loc == (at,)
    return len(loc) == 2 and loc[0] == at and isinstance(loc[1], int)

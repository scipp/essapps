# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rules and the trigger loop: automatic reduction of new files, as described in
``docs/developer/automatic-reduction.md``.

What to run for a file belongs to the application, which knows the technique: how
to pair runs, and which requests one file needs. The trigger loop is a controller
of a client that never runs in the backend. It finds the new files and hands each
to the application's function. Which files a rule has handled comes from the
records under the rule's label. The loop reads them from the proposal's changes
(:meth:`~ess.apps.Client.changes`), from the start when it starts, so it holds no
more than a position in them and the files it has seen.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ess.spec import FileRef
from ess.spec.refs import iter_refs

from .client import Client
from .files import Selector
from .records import Record


@dataclass(frozen=True)
class Rule:
    """
    Call ``submit`` for each new file the selector matches.

    ``submit(client, file)`` submits what the file needs, any number of requests,
    and returns the record that settles the file: one under ``label`` that holds
    the file at the parameter ``at``, or in a row of it if ``at`` is a list. From
    then on the rule has handled the file, whatever becomes of the record.

    To leave a file for the next step, such as a sample whose transmission run has
    not arrived, ``submit`` raises ``LookupError`` or ``ValueError``, as a refused
    request does with ``SubmitError``. The rule's status names the file and the
    reason. A step that stops between two requests of a file calls ``submit`` again,
    so it submits the settling record last, and reuses what an earlier call submitted
    and did not fail, as ``docs/developer/automatic-reduction.md`` shows.

    ``submit`` does not wait for a record or read an output: the loop calls one
    function at a time, and a request can reference a pending record. It persists what
    it submits: the loop's client should keep nothing, and a reference must stay
    readable when the loop's process has restarted.
    """

    name: str
    submit: Callable[[Client, FileRef], Record]
    selector: Selector
    label: str
    at: str


@dataclass
class RuleStatus:
    """
    A rule's last step.

    ``reason`` says why the rule submitted nothing, or which files it
    skipped and why; it is ``None`` if the rule settled each new file.
    """

    reason: str | None = None


class TriggerLoop:
    """Steps the ``rules`` on ``client``. Raises ``ValueError`` if two share a name."""

    def __init__(self, client: Client, rules: list[Rule]) -> None:
        names = [rule.name for rule in rules]
        if duplicates := {name for name in names if names.count(name) > 1}:
            raise ValueError(f'two rules are named {sorted(duplicates)}')
        self._client = client
        self._rules = rules
        self._cursor = 0
        self._handled: dict[str, set[FileRef]] = {rule.name: set() for rule in rules}
        self._status = {rule.name: RuleStatus() for rule in rules}

    def step(self) -> list[Record]:
        """
        Call each rule for what arrived since the last step; return the records
        that settled a file.

        A file that its rule skips, or whose request is refused, stays new, so the
        next step tries it again; the other files and rules go on.

        Raises
        ------
        TypeError
            If ``submit`` returned something that does not settle the file. The
            next step would submit for the file again, so the loop stops.
        """
        self._read()
        settled: list[Record] = []
        for rule in self._rules:
            handled = self._handled[rule.name]
            reasons: list[str] = []
            new = [
                r for r in self._client.files.list(rule.selector) if r not in handled
            ]
            # A record may settle a later file of this list too, as a row.
            for ref in new:
                if ref in handled:
                    continue
                try:
                    record = rule.submit(self._client, ref)
                except (LookupError, ValueError) as error:  # a SubmitError too
                    reasons.append(f'skipped {ref.file}: {error}')
                    continue
                _check_settles(record, rule, ref)
                self._take(record)
                settled.append(record)
            reason = '; '.join(reasons) or (None if new else 'no new file')
            self._status[rule.name] = RuleStatus(reason)
        return settled

    def run(self, interval: float = 1.0) -> None:
        """Step forever."""
        while True:
            self.step()
            time.sleep(interval)

    def status(self, rule: Rule) -> RuleStatus:
        return self._status[rule.name]

    def _read(self) -> None:
        """Take the records that changed into each rule's handled files."""
        changes = self._client.changes(after=self._cursor)
        self._cursor = changes.cursor
        for record in self._client.records(ids=changes.records):
            self._take(record)

    def _take(self, record: Record) -> None:
        for rule in self._rules:
            if record.label == rule.label:
                self._handled[rule.name] |= _files_at(record, rule.at)


def _files_at(record: Record, at: str) -> set[FileRef]:
    """The files that ``record`` holds at the parameter ``at``, or in a row of it."""
    return {
        ref
        for loc, ref in iter_refs(record.request.params)
        if isinstance(ref, FileRef)
        and loc[0] == at
        and (len(loc) == 1 or (len(loc) == 2 and isinstance(loc[1], int)))
    }


def _check_settles(record: object, rule: Rule, ref: FileRef) -> None:
    if (
        not isinstance(record, Record)
        or record.label != rule.label
        or ref not in _files_at(record, rule.at)
    ):
        raise TypeError(
            f'{rule.name}: submit for {ref.file} returned {record!r}, not a record '
            f'under the label {rule.label!r} that holds the file at {rule.at!r}'
        )

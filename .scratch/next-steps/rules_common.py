"""
Shared setup for the rules_*.py scripts: automatic reduction written with the
batch model (``ess.apps.batch``), over the toy specs of the user stories.

Run each script with ``.scratch/venv-container/bin/python``. Nothing here
changes the repository: the pieces that ``ess.apps.batch`` lacks are written
in this file, marked MISSING, so each script shows what it needs.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'packages/essapps/tests/stories'))

import toys  # noqa: E402  the toy specs of user-stories.md

from ess.apps import Client, Record, Status, SubmitError, Workspace  # noqa: E402
from ess.apps.batch import Batch, ready, remaining  # noqa: E402
from ess.apps.log import Log  # noqa: E402
from ess.apps.testing import FakeFiles, FakeStore  # noqa: E402
from ess.spec import FileRef  # noqa: E402
from ess.spec.refs import iter_refs  # noqa: E402


class Proposal:
    """
    One proposal of the automatic reduction: files, a history, and a store that
    outlive a workspace, so that :meth:`restart` is a restart of the process.
    """

    def __init__(self, submitter: str = 'auto') -> None:
        self.directory = Path(tempfile.mkdtemp(prefix='rules-'))
        self.files = FakeFiles('p1', self.directory)
        self.log = Log()
        self.store = FakeStore()
        self.submitter = submitter
        self.workspace: Workspace | None = None
        self.bind = list(toys.TOYS.values())

    def open(self, bind: list | None = None) -> Client:
        if self.workspace is not None:
            self.workspace.close()
        self.workspace = Workspace(
            proposal='p1',
            files=self.files,
            bind=self.bind if bind is None else bind,
            log=self.log,
            store=self.store,
        )
        return Client(self.workspace, submitter=self.submitter)

    restart = open

    def measure(self, run: int, counts: list[float], **fields: Any) -> FileRef:
        return self.files.measure(run, counts, **fields)

    def close(self) -> None:
        if self.workspace is not None:
            self.workspace.close()


# The obvious first attempt: the batch's "reduce remaining", called by a loop.


def naive_step(batch: Batch, settings: Mapping[str, Any]) -> dict[str, Record] | str:
    """Scan, then reduce the remaining rows; the error text if refused."""
    batch.scan()
    try:
        return batch.reduce(settings, remaining(batch.state(settings)))
    except SubmitError as error:
        return f'refused: {error}'


# MISSING 1: "handled" as rules read it, for the rows of a batch.
# A row needs a reduction if its cell at the column `at` holds a file that no
# record under the batch's label holds at `at`. Read from the records, so it
# survives a restart; any spec, member, and submitter count, as for a rule.


def files_at(record: Record, at: str) -> set[FileRef]:
    """rules._files_at, unchanged."""
    return {
        ref
        for loc, ref in iter_refs(record.request.params)
        if isinstance(ref, FileRef)
        and loc[0] == at
        and (len(loc) == 1 or (len(loc) == 2 and isinstance(loc[1], int)))
    }


def unhandled(batch: Batch, at: str) -> list[str]:
    """The ready rows whose files at ``at`` no record under the label holds."""
    handled: set[FileRef] = set()
    for record in batch.client.records(label=batch.label):
        handled |= files_at(record, at)
    states = batch.state()
    return [
        key
        for key in ready(states)
        if {ref for _, ref in iter_refs({at: batch.rows[key].values.get(at)})}
        - handled
    ]


# MISSING 2: one refused row must not hold back the others.
# Batch.reduce is all-or-nothing; the loop keeps the reason of each refusal, as
# RuleStatus does. Row by row (reduce_each) calls batch.state() per row, so a
# step with N new rows costs O(N^2) (rules_cost.py). reduce_all_but_refused
# submits all rows, drops the rows a refusal names, and submits the rest again.


def reduce_each(
    batch: Batch, settings: Mapping[str, Any], keys: Sequence[str]
) -> tuple[dict[str, Record], dict[str, str]]:
    records: dict[str, Record] = {}
    refused: dict[str, str] = {}
    for key in keys:
        try:
            records |= batch.reduce(settings, [key])
        except SubmitError as error:
            refused[key] = str(error)
    return records, refused


def reduce_all_but_refused(
    batch: Batch, settings: Mapping[str, Any], keys: Sequence[str]
) -> tuple[dict[str, Record], dict[str, str]]:
    keys = list(keys)
    refused: dict[str, str] = {}
    while keys:
        try:
            return batch.reduce(settings, keys), refused
        except SubmitError as error:
            named = {e.entry for e in error.errors if e.entry is not None}
            if not named:  # a reason of the settings: every row is refused
                return {}, refused | dict.fromkeys(keys, str(error))
            refused |= {k: str(SubmitError([e for e in error.errors if e.entry == k]))
                        for k in named}
            keys = [k for k in keys if k not in named]
    return {}, refused


def auto_step(
    batch: Batch, settings: Mapping[str, Any], at: str, each: bool = False
) -> tuple[dict[str, Record], dict[str, str]]:
    """The loop of the automatic reduction with both missing pieces."""
    batch.scan()
    submit = reduce_each if each else reduce_all_but_refused
    return submit(batch, settings, unhandled(batch, at))


def show(client: Client, records: Mapping[str, Record] | Sequence[Record]) -> Any:
    items = records.values() if isinstance(records, Mapping) else records
    items = list(items)
    client.wait(items)
    return [(r.member, str(client.status(r))) for r in items]


def runs(client: Client, record: Record, at: str) -> list[int]:
    return sorted(client.files.metadata(ref)['run'] for ref in files_at(record, at))


__all__ = [
    'Batch',
    'Client',
    'Proposal',
    'Record',
    'Status',
    'auto_step',
    'files_at',
    'naive_step',
    'reduce_each',
    'remaining',
    'runs',
    'show',
    'toys',
    'unhandled',
]

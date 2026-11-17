# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch reduced as its files arrive. The guide is the
section "Automatic reduction" of ``packages/essapps/docs/batch.md``.

A step does what a person does with the buttons of a batch's page to scan and
to reduce, with the rule :func:`~.state.arrived` in place of the person's
choice::

    batch.scan()
    state = batch.state()
    batch.reduce(arrived(state, at))

Which rows a step reduces follows from the files, the batch's rows and
settings, and the records under its label. :class:`Automatic` keeps nothing
that changes it: :attr:`Automatic.refused` says why rows of the last step were
refused, and a refused row is tried again at every step. So a step that raises
loses nothing.

The loop keeps the results under its label equal to its batch's rows at the
columns of ``at``, so a label has one batch. A record that another ``Batch``
submits under the label with other cells at ``at`` is replaced at the next
step. A restarted loop reduces no row again whose result equals its row at
``at``. An edit is kept by the batch's definitions (:mod:`.definition`), so
with them, an edited row is not reduced again either; without them, the edit
is gone after a restart, and a row edited at ``at`` is reduced again with the
source's cells.

A step costs the scan of all files and the state of all rows:
the status of every row's result, and the comparison of every row's cells with
its result's request, which resolves the cells that differ as given, such as a
file named by run (:meth:`~.reduction.Batch.state`). Since a scan drops what
the batch kept resolved, such a cell is resolved at every step, at any column.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from collections.abc import Set as AbstractSet
from contextlib import contextmanager

from ..errors import SubmitError
from ..records import Record
from .reduction import Batch
from .state import arrived, ready


class Automatic:
    """
    Reduces the rows of ``batch`` that :func:`~.state.arrived` gives at each
    step. See ``packages/essapps/docs/batch.md``.

    Parameters
    ----------
    batch:
        The batch, with a source. It should persist what it submits
        (``persist=True``), since nothing releases its records.
    at:
        The columns at which a change of a row's request reduces the row
        again, such as ``{'sample_runs', 'trans_runs'}``. A row with no result
        is reduced whatever ``at`` is.

    Attributes
    ----------
    refused:
        Why the last step's submission refused each row, by key: the rows it
        submitted that a ``SubmitError`` names, or every row it submitted if
        the reason names none. If the settings are refused, every ready row.

    Raises
    ------
    KeyError
        If an element of ``at`` is not a column of ``batch``.
    TypeError
        If ``at`` is a string, not a set of columns.
    ValueError
        If ``batch`` has no source.
    """

    def __init__(
        self,
        batch: Batch,
        *,
        at: AbstractSet[str],
    ) -> None:
        if isinstance(at, str):
            raise TypeError(f'at is a set of columns, such as {{{at!r}}}')
        if unknown := sorted(set(at) - set(batch.columns)):
            raise KeyError(f'{unknown} are not columns of the batch')
        if batch.source is None:
            raise ValueError('this batch has no source of rows')
        self.batch = batch
        self.at = frozenset(at)
        self.refused: dict[str, str] = {}

    def step(self) -> dict[str, Record]:
        """
        Scan, and reduce the rows that :func:`~.state.arrived` gives, with the
        batch's settings as they are at the step; their records by key. A row
        whose request is refused does not hold back the others, and
        :attr:`refused` says why.

        If the settings are refused, no row can run: the step reduces none,
        and :attr:`refused` gives the reason for each ready row.
        """
        self.batch.scan()
        return self.reduce()

    def reduce(self) -> dict[str, Record]:
        """
        The second half of :meth:`step`, without the scan: reduce the rows
        that :func:`~.state.arrived` gives now. A page that scans by itself,
        such as the batch page, calls this after its scan.
        """
        batch = self.batch
        try:
            state = batch.state()
        except SubmitError as error:
            self.refused = dict.fromkeys(ready(batch.results()), str(error))
            return {}
        records, self.refused = self._reduce(arrived(state, self.at))
        return records

    def _reduce(self, keys: list[str]) -> tuple[dict[str, Record], dict[str, str]]:
        """
        Submit ``keys``, less those a refusal names, or none if a refusal names
        none; the records by key, and why each row was refused.
        """
        refused: dict[str, str] = {}
        while keys:
            try:
                return self.batch.reduce(keys), refused
            except SubmitError as error:  # noqa: PERF203
                named = set(keys).intersection(e.entry for e in error.errors)
                if not named:
                    return {}, refused | dict.fromkeys(keys, str(error))
                for key in named:
                    errors = [e for e in error.errors if e.entry == key]
                    refused[key] = str(SubmitError(errors))
                keys = [k for k in keys if k not in named]
        return {}, refused


def run(loops: Sequence[Automatic], interval: float = 1.0) -> None:
    """
    Step each of ``loops`` in the order given, then wait ``interval`` seconds,
    until a step raises. A batch whose source reads the results of another
    comes after it.
    """
    while True:
        for loop in loops:
            loop.step()
        time.sleep(interval)


@contextmanager
def every(seconds: float, actions: Iterable[Callable[[], object]]) -> Iterator[None]:
    """
    Call ``actions`` in turn from a thread, one every ``seconds``, until all are
    called or the block ends. The end of the block waits for an action that
    runs. A demo makes files arrive with it while it serves a page.
    """
    stop = threading.Event()

    def call() -> None:
        for action in actions:
            if stop.wait(seconds):
                return
            action()

    thread = threading.Thread(target=call, name='every')
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the pages of the batches of a workspace share, without HTTP:
:class:`Pages`. :mod:`.api` serves the page of each batch of a
:class:`~ess.apps.batch.BatchManager` at ``/batches/<label>/``; the
:class:`~.server.Server` of a page reads what is shared from here.

The pages share one lock. Every request of every page and each step of Follow
take it, so that what the pages share, such as the corrections of runs and
the batches of the manager, changes in one request at a time. One person
works with the application, so a request waits for at most one step, or for
the counts (:meth:`Pages.counts`), which scan every batch.

The runs are the same on every page. A correction made on one page scans its
batch at once; the page of each other batch scans its batch at its next poll
(:attr:`Pages.corrected`), and says once if that scan fails.

Follow is the one loop of the application
(:class:`~ess.apps.batch.auto.Follow`). :meth:`Pages.tick` steps it, with or
without a page open, keeps what the step raised, and has the page of the batch
that follows show what the step changed. :func:`~.api.create_app` owns the
timer that calls it.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from pydantic import BaseModel

from ess.spec import FileRef

from .. import BatchManager, CorrectedFiles, RowState, Words, refused_again
from ..auto import Follow
from . import forms
from .models import BatchCounts, Batches, BatchRow
from .server import Draw, Problem, Server, page_statuses


class Missing(Exception):
    """No batch has the label asked for."""


class Pages:
    """
    What the pages of the batches of ``manager`` share. See the module.

    Parameters
    ----------
    manager:
        The batches.
    follow:
        The loop of the application over them.
    title:
        The title of the application.
    draw:
        Draws results (see :data:`~.server.Draw`), by the name of the spec of
        a batch.
    files:
        The file source of the client, to correct runs in.
    marks:
        The fields of a run that a person corrects, as a pydantic model.
    folder:
        What the header names as the place of the data, such as a folder.

    Attributes
    ----------
    lock:
        The lock of every request of every page and of each step of Follow.
    runs_version:
        Counts the changes of the runs, so that the Runs view of each page
        asks for them again.
    corrected:
        Counts the corrections of runs, so that the page of each batch scans
        again at its next poll after one.
    failure:
        What the last step of Follow raised, ``None`` if it raised nothing.
    """

    def __init__(
        self,
        manager: BatchManager,
        follow: Follow,
        *,
        title: str = '',
        draw: Mapping[str, Draw] | None = None,
        files: CorrectedFiles | None = None,
        marks: type[BaseModel] | None = None,
        folder: str = '',
    ) -> None:
        self.manager = manager
        self.follow = follow
        self.title = title
        self.draw = {} if draw is None else draw
        self.files = files
        self.marks = marks
        self.folder = folder
        self.lock = threading.RLock()
        self.runs_version = 1
        # The runs, with their metadata, as listed at the last version.
        self._runs: Mapping[FileRef, Mapping[str, Any]] = {}
        # The batches and the label that follows, as listed at the last version.
        self._batches: tuple[object, ...] = ()
        self._batches_version = 0
        self.corrected = 0
        self.failure: str | None = None
        self._pages: dict[str, Server] = {}

    def page(self, label: str) -> Server:
        """
        The server of the page of ``label``, made at the first call. Raises
        :class:`Missing` if no batch has the label, and
        :class:`~.server.Problem` if its batch cannot be made, such as when
        its source raises.
        """
        with self.lock:
            if label not in self.manager.batches():
                self._pages.pop(label, None)
                raise Missing(f'There is no batch {label}.')
            if (server := self._pages.get(label)) is None:
                try:
                    server = Server(self, label)
                except Exception as error:  # a source is code of the application
                    raise Problem(f'Cannot show {label}: {error}') from None
                self._pages[label] = server
            return server

    def home(self) -> str | None:
        """
        The label of the batch that follows, else the last label; ``None`` if
        there is no batch.
        """
        with self.lock:
            batches = self.manager.batches()
            if self.follow.label in batches:
                return self.follow.label
            return next(reversed(batches), None)

    def tick(self) -> None:
        """
        A step of Follow, then :meth:`Server.stepped <.server.Server.stepped>`
        of the page of the batch that follows; :func:`~.api.create_app` calls
        it on a timer. What the step raises is :attr:`failure`, and the next
        step tries again.
        """
        # Under the lock, so that a tick that waited for a request sees what
        # that request did: no step after "Stopped following". What cannot be
        # said, such as an exception whose text raises, or a page that cannot
        # be made, must not end the timer.
        with self.lock, suppress(Exception):
            try:
                self.follow.step()
                self.failure = None
            except Exception as error:  # the next step tries again
                self.failure = f'Follow could not step: {str(error) or repr(error)}'
            if (label := self.follow.label) is not None:
                self.page(label).stepped()

    def runs_listed(
        self, runs: Mapping[FileRef, Mapping[str, Any]], *, changed: bool = False
    ) -> None:
        """
        A new version of the runs, which the Runs view of each page asks for,
        if ``runs``, as a page lists them now, differ from those of the last
        version, or if ``changed``, as after a correction.
        """
        if changed or runs != self._runs:
            self._runs = runs
            self.runs_version += 1

    def batches_version(self) -> int:
        """
        The version of the list of batches, so that the Batches view of each
        page asks for it again. It moves when the headers of the batches or
        the label that follows differ from those of the last version, whoever
        changed them.
        """
        with self.lock:
            listed = (tuple(self.manager.batches().items()), self.follow.label)
            if listed != self._batches:
                self._batches = listed
                self._batches_version += 1
            return self._batches_version

    def waiting(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row of ``states`` that its next step
        would submit again, by key; none unless the batch of ``label`` follows.
        """
        source = self.manager.source(label)
        if source is None or self.follow.label != label:
            return {}
        reasons = {
            key: f'Follow could not reduce it: {forms.message(reason)}'
            for key, reason in self.follow.refused.items()
        }
        return refused_again(states, source.columns, reasons)

    def batches(self) -> Batches:
        """The batches, as the Batches view of each page lists them."""
        with self.lock:
            rows = []
            for label, header in self.manager.batches().items():
                name, sources = header.source, self.manager.sources
                # listed even if its source is no longer registered
                source = None if name is None else sources.get(name)
                rows.append(
                    BatchRow(
                        label=label,
                        title=header.title,
                        description=header.description,
                        words=Words() if source is None else source.words,
                        follows=label == self.follow.label,
                    )
                )
            return Batches(version=self.batches_version(), rows=rows)

    def counts(self) -> BatchCounts:
        """
        The counts of the manager (:meth:`~ess.apps.batch.BatchManager.counts`)
        in the words of the page: the statuses are those of the page of each
        batch (:func:`~.server.page_statuses`). Every page and Follow wait
        while it scans.
        """
        with self.lock:
            counts = self.manager.counts()
            rows = {}
            for label, states in counts.states.items():
                columns = self.manager.batch(label).columns
                waiting = self.waiting(label, states)
                statuses = page_statuses(states, columns, waiting)
                rows[label] = dict(Counter(statuses.values()))
            sources = self.manager.sources
            return BatchCounts(
                rows=rows,
                failed=counts.failed,
                outside={
                    name: sources[name].words.count(n)
                    for name, n in counts.outside.items()
                },
            )

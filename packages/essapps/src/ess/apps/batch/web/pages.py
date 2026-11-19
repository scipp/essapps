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
works with the application, so a request waits for at most one step.

Follow is the one loop of the application
(:class:`~ess.apps.batch.auto.Follow`). :meth:`Pages.tick` steps it, with or
without a page open, keeps what the step raised, and has the page of the batch
that follows show what the step changed. :func:`~.api.create_app` owns the
timer that calls it.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from contextlib import suppress

from pydantic import BaseModel

from .. import BatchManager, CorrectedFiles
from ..auto import Follow
from .server import Draw, Problem, Server


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

    def new_runs(self) -> None:
        """A new version of the runs, which the Runs view of each page asks for."""
        self.runs_version += 1

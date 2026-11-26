# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that the client lists, as every page shows them: the listing of
the pickers and the Runs view, and the runs as a person corrects them:
:class:`Files`.

The pages share one listing of what the client lists (:attr:`Files.listing`):
the files, with their metadata, and the records. A page asks to list again
(:meth:`Files.list_again`) when the records or its batch table changed, at a
step of Follow, at "List runs again", and as it is made; the listing counts
the changes of the files and of why they cannot be listed
(:attr:`SafeListing.version`), which every page compares at its poll.
The runs are the files, the same on every page (:meth:`Files.runs`), and a
person corrects them on any page, with or without a batch
(:meth:`Files.correct`). A correction changes no row of a batch: a batch
table names files, and the page shows each with its metadata now. The Runs
view names the batches of each run from the files that each batch names,
which come with the counts (:meth:`Pages.counts <.pages.Pages.counts>`),
since Follow adds rows without a change of the runs.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from pydantic import BaseModel

from ess.apps.client import Client
from ess.apps.errors import said
from ess.apps.fields import UNSET, Fields
from ess.apps.files import Selector
from ess.apps.listing import Listing
from ess.spec import File, FileRef

from ..corrections import CorrectedFiles
from . import forms, view
from .models import CellEdit, Cells, Column, Notice, Runs
from .refusals import Problem, kept


class SafeListing(Listing):
    """
    What the client lists now, as :class:`Listing` gives it, but without files
    if the file source raises: a picker then offers none, the Runs view lists
    none, and :attr:`failure` says why. It lists every file with its metadata
    as it is made (:attr:`files_listed`), and :attr:`version` counts, from
    the first listing, each listing whose files or failure differ from those
    of the listing ``previous``.
    """

    error: str | None = None
    """What the file source raised, ``None`` if it raised nothing."""

    def __init__(self, client: Client, previous: SafeListing | None = None) -> None:
        super().__init__(client, previous)
        self.files_listed = self.listed(Selector())
        # The files and why they cannot be listed, as made: a picker may
        # list files later, and set :attr:`error`.
        self._said = (self.files_listed, self.failure)
        self.version = 1
        if previous is not None:
            self.version = previous.version + (self._said != previous._said)

    @property
    def failure(self) -> str | None:
        """Why the files cannot be listed, as the page says it."""
        return (
            None if self.error is None else f'The files cannot be listed: {self.error}'
        )

    def files(self, marker: File) -> dict[str, FileRef]:
        try:
            return super().files(marker)
        except Exception as error:  # the file source is code of the application
            self.error = said(error)
            return {}

    def listed(self, selector: Selector) -> dict[FileRef, dict[str, Any]]:
        try:
            return super().listed(selector)
        except Exception as error:  # the file source is code of the application
            self.error = said(error)
            return {}


class Files:
    """
    The files that the client lists, for every page: the listing of the
    pickers (:attr:`listing`) and the Runs view (:meth:`runs`). Each public
    method takes ``lock``; a property reads one value without it.

    Parameters
    ----------
    client:
        The client whose files are listed.
    lock:
        The lock of all pages.
    marks:
        The fields of a run that a person corrects, as a pydantic model.
    corrected:
        The file source of the client, to correct runs in.
    """

    def __init__(
        self,
        client: Client,
        lock: AbstractContextManager[object],
        *,
        marks: type[BaseModel] | None = None,
        corrected: CorrectedFiles | None = None,
    ) -> None:
        self._client = client
        self._lock = lock
        # What a person corrects of a run, and where; None if nothing.
        self._marking = (
            None if marks is None or corrected is None else (marks, corrected)
        )
        self._listing = SafeListing(client)

    @property
    def listing(self) -> SafeListing:
        """
        What the client lists, as :meth:`list_again` listed it last: the
        files, their labels and the records, for every page.
        """
        return self._listing

    @property
    def runs_version(self) -> int | None:
        """
        :attr:`SafeListing.version`, which the Runs view of each page compares
        to ask for the runs again; ``None`` if the application corrects no
        runs, so that the pages have no Runs view.
        """
        return self._listing.version if self.corrects_runs else None

    @property
    def corrects_runs(self) -> bool:
        """Whether the application corrects runs: it gives their marks and files."""
        return self._marking is not None

    def list_again(self) -> None:
        """
        List what the client lists now (:attr:`listing`): when the records
        or a table changed, at a step of Follow, at "List runs again", as a
        page is made, and for the Runs view.
        """
        with self._lock:
            self._listing = SafeListing(self._client, previous=self._listing)

    def _marks(self) -> tuple[Fields, CorrectedFiles]:
        """
        The form of what a person corrects of a run, and the files to
        correct; :class:`~.refusals.Problem` if the application corrects no runs.
        """
        if self._marking is None:
            raise Problem('This application corrects no runs.')
        marks, corrected = self._marking
        return Fields(marks, self._listing), corrected

    def runs(self) -> Runs:
        """
        The runs, listed again, with what a person corrected of each, as the
        Runs view of every page shows them, and that of the page without a
        batch; none, with why, if the file source raises. Raises
        :class:`~.refusals.Problem` if the application corrects no runs.
        """
        with self._lock:
            marks, corrected = self._marks()
            self.list_again()
            listing = self._listing
            return Runs(
                version=listing.version,
                columns=[
                    Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                    for n, f in marks.fields.items()
                ],
                rows=[
                    view.run(ref, marks, corrected, listing)
                    for ref in listing.files_listed
                ],
                problem=listing.failure,
            )

    def correct(self, edit: CellEdit) -> Notice:
        """Correct a field of the runs ``edit.keys``, as its field reads the value."""
        with self._lock:
            marks, corrected = self._marks()
            value = view.read(marks, edit)
            fields = {edit.column: None if value is UNSET else value}
            self._kept(
                lambda: corrected.correct_each(
                    [FileRef(file=key) for key in edit.keys], **fields
                )
            )
            n = len(edit.keys)
            title = marks.titles[edit.column]
            return Notice(notice=f'{title} corrected on {n} run{"s" * (n > 1)}.')

    def revert_run(self, cells: Cells) -> Notice:
        """Take a field of runs back to what the files say, as :meth:`correct`."""
        with self._lock:
            marks, corrected = self._marks()
            self._kept(
                lambda: corrected.reset_each(
                    [FileRef(file=key) for key in cells.keys], cells.column
                )
            )
            n = len(cells.keys)
            title = marks.titles.get(cells.column, cells.column)
            return Notice(notice=f'{title} set back on {n} run{"s" * (n > 1)}.')

    def _kept(self, correction: Callable[[], None]) -> None:
        """
        Make ``correction``, then list again, so that the pages show the runs
        again. Raises :class:`~.refusals.Problem` if the definitions do not keep
        it, or if the runs cannot be listed after it.
        """
        with kept():
            correction()
        self.list_again()
        if (error := self._listing.error) is not None:
            raise Problem(f'Corrected, but the runs cannot be listed: {error}')

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that the client lists, shared by every page: :class:`Runs`.

The pickers and the Runs view of every page use one listing
(:attr:`Runs.listing`). A correction of a run changes no batch table: a cell
refers to a file, and the page shows the file with its metadata now. The Runs
view gets the batches of each run from
:meth:`Pages.counts <.pages.Pages.counts>`, not from :meth:`Runs.shown`, since
Follow adds rows to a batch without a change of the runs.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from pydantic import BaseModel

from ess.apps.client import Client
from ess.apps.errors import error_text
from ess.apps.fields import UNSET, Fields
from ess.apps.files import Selector
from ess.apps.listing import Listing
from ess.spec import File, FileRef

from ..corrections import CorrectedFiles
from . import forms, models, view
from .errors import Rejected, reject_on_write_error
from .models import CellEdit, Cells, Column, Notice, RunCell, RunRow


class SafeListing(Listing):
    """
    A :class:`Listing` that lists no files if the file source raises, and
    then sets :attr:`error`.

    :attr:`revision` starts at 1 and counts each listing whose files or
    failure differ from those of ``previous``.
    """

    error: str | None = None
    """What the file source raised, ``None`` if it raised nothing."""

    def __init__(self, client: Client, previous: SafeListing | None = None) -> None:
        super().__init__(client, previous)
        self.files_listed = self.listed(Selector())
        # A snapshot: a picker may list files later and set :attr:`error`.
        self._shown = (self.files_listed, self.failure)
        self.revision = 1
        if previous is not None:
            self.revision = previous.revision + (self._shown != previous._shown)

    @property
    def failure(self) -> str | None:
        """The message the page shows if the files cannot be listed."""
        return (
            None if self.error is None else f'The files cannot be listed: {self.error}'
        )

    def files(self, marker: File) -> dict[str, FileRef]:
        try:
            return super().files(marker)
        except Exception as error:  # the file source is code of the application
            self.error = error_text(error)
            return {}

    def listed(self, selector: Selector) -> dict[FileRef, dict[str, Any]]:
        try:
            return super().listed(selector)
        except Exception as error:  # the file source is code of the application
            self.error = error_text(error)
            return {}


class Runs:
    """
    The files that the client lists, for the pickers (:attr:`listing`) and the
    Runs view (:meth:`shown`) of every page.

    Each public method holds ``lock``. A property reads one value without it.

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
        """What the client listed at the last :meth:`list_again`."""
        return self._listing

    @property
    def revision(self) -> int | None:
        """
        :attr:`SafeListing.revision`, or ``None`` if the application corrects
        no runs. The page has no Runs view if ``None``.
        """
        return self._listing.revision if self.corrects else None

    @property
    def corrects(self) -> bool:
        """Whether the application passed ``marks`` and ``corrected``."""
        return self._marking is not None

    def list_again(self) -> None:
        """Replace :attr:`listing` with what the client lists now."""
        with self._lock:
            self._listing = SafeListing(self._client, previous=self._listing)

    def _marks(self) -> tuple[Fields, CorrectedFiles]:
        """The fields of a run that a person corrects, and the file source."""
        if self._marking is None:
            raise Rejected('This application corrects no runs.')
        marks, corrected = self._marking
        return Fields(marks, self._listing), corrected

    def shown(self) -> models.Runs:
        """
        The runs, listed again, with the corrections of each.

        Raises :class:`~.errors.Rejected` if the application corrects no runs.
        """
        with self._lock:
            marks, corrected = self._marks()
            self.list_again()
            listing = self._listing
            return models.Runs(
                revision=listing.revision,
                columns=[
                    Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                    for n, f in marks.fields.items()
                ],
                rows=[
                    _run(ref, marks, corrected, listing) for ref in listing.files_listed
                ],
                problem=listing.failure,
            )

    def correct(self, edit: CellEdit) -> Notice:
        """Correct a field of the runs ``edit.keys``."""
        with self._lock:
            marks, corrected = self._marks()
            value = view.read(marks, edit)
            fields = {edit.column: None if value is UNSET else value}
            self._correct_and_list(
                lambda: corrected.correct_each(
                    [FileRef(file=key) for key in edit.keys], **fields
                )
            )
            n = len(edit.keys)
            title = marks.titles[edit.column]
            return Notice(notice=f'{title} corrected on {n} run{"s" * (n > 1)}.')

    def revert(self, cells: Cells) -> Notice:
        """Set a field of runs back to the value in the file."""
        with self._lock:
            marks, corrected = self._marks()
            self._correct_and_list(
                lambda: corrected.reset_each(
                    [FileRef(file=key) for key in cells.keys], cells.column
                )
            )
            n = len(cells.keys)
            title = marks.titles.get(cells.column, cells.column)
            return Notice(notice=f'{title} set back on {n} run{"s" * (n > 1)}.')

    def _correct_and_list(self, correction: Callable[[], None]) -> None:
        """
        Call ``correction``, then list again.

        Raises :class:`~.errors.Rejected` if the definitions cannot be written,
        or if the runs cannot be listed afterwards.
        """
        with reject_on_write_error():
            correction()
        self.list_again()
        if (error := self._listing.error) is not None:
            raise Rejected(f'Corrected, but the runs cannot be listed: {error}')


def _run(
    ref: FileRef, marks: Fields, files: CorrectedFiles, listing: Listing
) -> RunRow:
    """The row of the file ``ref`` in the Runs view."""
    metadata = files.metadata(ref)
    original = files.source.metadata(ref)
    corrected = files.corrections(ref)
    cells = {
        name: RunCell(
            value=forms.cell(field, metadata.get(name, UNSET)),
            text=view.text(listing, metadata.get(name, UNSET)),
            edited=name in corrected,
            original=view.text(listing, original.get(name, UNSET)),
        )
        for name, field in marks.fields.items()
    }
    return RunRow(key=ref.file, file=listing.file_label(ref), cells=cells)

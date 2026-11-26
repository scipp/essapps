# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the pages of all batches share, without HTTP: :class:`Pages`.

One lock, :attr:`Pages.lock`, serializes every request, every step of Follow
and every count. One person uses the application, so a request waits for at
most one step of Follow or one count.

:meth:`Pages.tick` runs one step of Follow, whether a page is open or not.

The objects of the page and the state each one owns: ``README.md`` in this
folder.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ess.apps.fields import Fields
from ess.apps.listing import Listing
from ess.spec import WorkflowSpec

from ..corrections import CorrectedFiles
from ..intake import Intake, fits, step_failure
from ..source import Range, RowSource
from ..table import read_table
from ..writing import Writing
from . import forms, view
from .errors import (
    NotFound,
    Rejected,
    reject_on_error,
    reject_on_write_error,
    request_error_text,
)
from .last_step import LastStep
from .models import (
    BatchCounts,
    Batches,
    BatchRow,
    Made,
    NewBatch,
    Notice,
    Param,
    SourceChoice,
    SourceRows,
    SpecChoice,
)
from .page import Page
from .runs import Runs
from .tempfiles import read_file

if TYPE_CHECKING:
    from ess.apps.scicat import Catalogue
    from ess.apps.writers import Writer

    from ..publish import Publisher
    from .page import Draw


class Pages:
    """
    What the pages of the batches of ``intake.manager`` share.

    Each public method takes :attr:`lock`, which a caller may hold already.

    Parameters
    ----------
    intake:
        The row sources and Follow; its manager holds the batches.
    interval:
        The seconds between two steps of Follow, for the timer of
        :func:`~.api.create_app`.
    title:
        The title of the application.
    draw:
        The function that draws results (:data:`~.page.Draw`), by spec name.
    files:
        The file source of the client, in which a person corrects runs.
    marks:
        The fields of a run that a person corrects, as a pydantic model.
    folder:
        The location of the data shown in the header, such as a folder path.
    writers:
        The writers that the tab "Output" offers, by spec name, in addition
        to :data:`~ess.apps.writers.SCIPP_HDF5`.
    catalogue:
        The catalogue to publish results to (:class:`~..publish.Publisher`).
        Without it, the page has no button "Publish" and no column
        "Published". With it, the default owner and contact of the output
        options are the principal investigator of the proposal, read from
        the catalogue once, here.

    Attributes
    ----------
    lock:
        The reentrant lock of every request, step of Follow and count.
    runs:
        The files listed, for the pickers and the Runs view of every page.
    last_step:
        What the last step of Follow said, for the page of the batch that
        follows.
    """

    def __init__(
        self,
        intake: Intake,
        *,
        interval: float = 5.0,
        title: str = '',
        draw: Mapping[str, Draw] | None = None,
        files: CorrectedFiles | None = None,
        marks: type[BaseModel] | None = None,
        folder: str = '',
        writers: Mapping[str, Sequence[Writer]] | None = None,
        catalogue: Catalogue | None = None,
    ) -> None:
        self.intake = intake
        writers = {} if writers is None else writers
        self.publisher: Publisher | None = None
        if catalogue is None:
            self.writing = Writing(intake.manager, writers)
        else:
            from ess.apps.scicat import principal_investigator  # needs the extra scicat

            from ..publish import Publisher

            proposal = intake.manager.client.proposal
            self.writing = Writing(
                intake.manager, writers, principal_investigator(catalogue, proposal)
            )
            self.publisher = Publisher(self.writing, catalogue)
        self.interval = interval
        self.title = title
        self.draw = {} if draw is None else draw
        self.folder = folder
        self.lock = threading.RLock()
        self.runs = Runs(intake.manager.client, self.lock, marks=marks, corrected=files)
        self.last_step = LastStep(intake, interval)
        self._pages: dict[str, Page] = {}

    def page(self, label: str) -> Page:
        """
        The page of ``label``, created at the first call.

        Raises :class:`~.errors.NotFound` if no batch has the label, and
        :class:`~.errors.Rejected` if its batch cannot be loaded, such as when
        the workspace binds no spec of its name or its batch table cannot be
        read.
        """
        with self.lock:
            self.check(label)
            if (page := self._pages.get(label)) is None:
                try:
                    page = Page(self, label)
                except (ValueError, OSError) as error:
                    raise Rejected(f'Cannot show {label}: {error}') from None
                self._pages[label] = page
            return page

    def check(self, label: str) -> None:
        """
        Raise :class:`~.errors.NotFound` unless a batch has ``label``.

        If none has, the page of ``label`` is dropped: a batch created later
        with the label gets a new page.
        """
        with self.lock:
            if label not in self.intake.manager.headers():
                self._pages.pop(label, None)
                raise NotFound(f'There is no batch {label}.')

    def home(self) -> str | None:
        """
        The label of the batch that follows, else of the only batch.

        ``None`` if no batch follows and there are zero or several batches, so
        that ``/`` lists them.
        """
        with self.lock:
            if (label := self.intake.following) is not None:
                return label
            headers = self.intake.manager.headers()
            return next(iter(headers)) if len(headers) == 1 else None

    def tick(self) -> None:
        """
        Run one step of Follow on the batch that follows, if one does.

        Then the page of that batch shows the changes, if it is open.
        Otherwise the files are listed again, so that every page shows the
        new files at its next poll. If the step raises, the page of the batch
        shows the error, and the next step tries again.
        """
        # Under the lock, so that a tick that waited for a request sees its
        # effect: no step after "Stop following". No exception, not even one
        # whose message raises, may end the timer.
        with self.lock, suppress(Exception):
            if (label := self.intake.following) is None:
                return
            try:
                if (done := self.intake.step()) is None:
                    return
                self.last_step.record_step(label, done)
            except Exception as error:  # the next step tries again
                self.last_step.failure = step_failure(error)
            if (page := self._pages.get(label)) is None:
                self.runs.list_again()
            else:
                page.after_step()

    def batches(self) -> Batches:
        """The batches, as the Batches view of each page lists them."""
        with self.lock:
            rows = [
                BatchRow(
                    label=label,
                    title=header.title,
                    description=header.description,
                    follows=label == self.intake.following,
                )
                for label, header in self.intake.manager.headers().items()
            ]
            return Batches(
                application=self.title,
                runs=self.runs.revision,
                rows=rows,
            )

    def sources(self) -> list[SourceChoice]:
        """
        The choices of the form "New batch".

        Each registered row source comes with the specs that fit it
        (:func:`~ess.batch.intake.fits`), and a row source that no spec fits is
        left out. The last choice is no row source, with every spec. The
        parameters offered as columns are the source columns, then each other
        parameter whose cell the page can edit.
        """
        with self.lock:
            client = self.intake.manager.client
            names = dict.fromkeys(spec.name for spec in client.specs())
            specs = [client.spec_named(name) for name in names]
            listing = self.runs.listing

            def choice(name: str | None, source: RowSource | None) -> SourceChoice:
                source_columns = () if source is None else source.source_columns
                return SourceChoice(
                    name=name,
                    source_columns=list(source_columns),
                    specs=[
                        _spec_choice(spec, source_columns, listing)
                        for spec in specs
                        if fits(spec, source)
                    ],
                )

            given = [choice(n, s) for n, s in self.intake.sources.items()]
            return [c for c in given if c.specs] + [choice(None, None)]

    def rows_of(
        self,
        source: str,
        first: str | None = None,
        last: str | None = None,
        later: bool = False,
    ) -> SourceRows:
        """
        The rows of the row source ``source`` now, each marked for a new batch
        of ``Range(first, last, later)`` (:meth:`~ess.batch.Intake.marked`).

        Raises :class:`~.errors.Rejected` if no row source has that name, if
        the range is invalid, or if the source raises.
        """
        with self.lock:
            with reject_on_error():
                given = self.intake.marked(source, Range(first, last, later))
            return view.listed(given, self.intake.sources[source].source_columns)

    def create(self, new: NewBatch, file: tuple[str, bytes] | None = None) -> Made:
        """
        Create the batch of ``new`` in one call, so that an error leaves no
        batch.

        With ``new.source``, the batch holds the rows of
        ``Range(new.first, new.last, new.later)`` among the rows of the source
        complete now (:meth:`~ess.batch.Intake.create`). With a table
        ``file``, given as name and content, it holds the rows of the file.
        Otherwise it holds no rows. Each of ``new.parameter_columns`` that is
        not a source column becomes an empty column. With ``new.follow``, the
        batch follows instead of the batch that followed before.

        The batch starts with the settings of the batch ``new.settings_from``,
        else with the settings that the application gives its spec. A setting
        at a parameter column stays, and applies to each row whose cell is
        empty. The :class:`Intake` drops the settings at source columns.

        Raises :class:`~.errors.Rejected` if ``new.settings_from`` is no
        batch, if the file, the manager, the :class:`Intake` or the source
        raises, or if the definitions cannot be written.
        """
        with self.lock, reject_on_error('The batch was not made'):
            manager = self.intake.manager
            settings = None
            if (other := new.settings_from) is not None:
                if other not in manager.headers():
                    raise ValueError(f'there is no batch {other} to start from')
                settings = manager.batch(other).settings
            rows = None
            if file is not None:
                name, content = file
                params = manager.spec_named(new.spec).params
                try:
                    rows = read_file(name, content, lambda p: read_table(p, params))
                except ValueError as error:
                    raise ValueError(f'{name}: {error}') from None
            elif new.source is not None:
                batch = self.intake.create(
                    new.spec,
                    title=new.title,
                    description=new.description,
                    source=new.source,
                    choose=Range(new.first, new.last, new.later),
                    parameter_columns=new.parameter_columns,
                    settings=settings,
                    follow=new.follow,
                )
                if new.follow:  # the failure was that of the batch that followed before
                    self.last_step.failure = None
                return Made(label=batch.label)
            batch = manager.create(
                new.spec,
                title=new.title,
                description=new.description,
                table=rows,
                parameter_columns=new.parameter_columns,
                settings=settings,
            )
        return Made(label=batch.label)

    def remove(self, label: str) -> Notice:
        """
        Remove the batch ``label`` (:meth:`~ess.batch.BatchManager.remove`).

        Its definitions are deleted and its records stay. If it follows, no
        batch follows afterwards. A :class:`~.page.Page` of the batch handed
        out before raises :class:`~.errors.NotFound` from then on.

        Raises :class:`~.errors.NotFound` if no batch has the label, and
        :class:`~.errors.Rejected` if the definitions cannot be written.
        """
        with self.lock, reject_on_write_error():
            self.check(label)
            title = self.intake.manager.headers()[label].title
            self.intake.manager.remove(label)
            self._pages.pop(label, None)
            return Notice(notice=f'Removed {title}.')

    def counts(self) -> BatchCounts:
        """
        The number of rows of each batch by status, and the files that each
        batch refers to (:meth:`~ess.batch.Batch.files`).

        The state of each batch is read as its page reads it
        (:meth:`~ess.batch.Batch.shown`), without asking its row source. A
        batch that cannot be loaded, such as one whose batch table cannot be
        read, has its reason in ``failed`` and no files.
        """
        with self.lock:
            manager = self.intake.manager
            rows: dict[str, dict[str, int]] = {}
            files: dict[str, list[str]] = {}
            failed: dict[str, str] = {}
            for label in manager.headers():
                try:
                    batch = manager.batch(label)
                    states, _ = batch.shown()
                    named = batch.files()
                except (ValueError, OSError) as error:
                    failed[label] = request_error_text(error)
                    continue
                rows[label] = dict(Counter(s.status for s in states.values()))
                files[label] = sorted(ref.file for ref in named)
            return BatchCounts(rows=rows, files=files, failed=failed)


def _spec_choice(
    spec: WorkflowSpec[Any, Any], source_columns: Sequence[str], listing: Listing
) -> SpecChoice:
    """
    ``spec`` as a choice for a row source with ``source_columns``: these
    first, then each other parameter whose cell the page can edit.
    """
    form = Fields(spec.params, listing)
    named = [
        *source_columns,
        *(n for n in forms.edited(form) if n not in source_columns),
    ]
    return SpecChoice(
        name=spec.name,
        title=spec.title,
        params=[Param(name=n, title=form.titles[n]) for n in named],
    )

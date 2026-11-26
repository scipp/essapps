# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the pages of the batches of a workspace share, without HTTP:
:class:`Pages`. :mod:`.api` serves the page of each batch of a
:class:`~ess.batch.BatchManager` at ``/batches/<label>/``; each
:class:`~.page.Page` reads what is shared from here.

The pages share one lock. Every request of every page and each step of Follow
take it, so that what the pages share, such as the corrections of runs and
the batches of the manager, changes in one request at a time. One person
works with the application, so a request waits for at most one step, or for
the counts (:meth:`Pages.counts`), which read the state of every batch.

The pages share one listing of what the client lists, and the Runs view
(:class:`~.runs.Runs`).

A person makes a batch from the list of batches, of the sources and specs
that :meth:`Pages.sources` offers, in one of three ways
(:meth:`Pages.create`): from a source, of a range of the rows that it makes
now (:meth:`Pages.rows_of`); from a table file; or without rows. Made from
the page of a batch, it may start with the settings of that batch. A batch
made from a source may follow it from then on, in place of the batch that
followed before; no other action of the page makes a batch follow
(:meth:`~ess.batch.Intake.follow` does, for the command). The manager and the
intake decide what can be made. With no batch at all, the page is served
without one: it shows the list of batches and the runs. So is the page of a
batch that cannot be made (:meth:`Pages.page`), with the reason, and that of
a batch removed (:meth:`Pages.remove`): a :class:`~.page.Page` handed out
before answers :class:`~.errors.NotFound` from then on.

``Intake`` keeps which batch follows
(:attr:`~ess.batch.Intake.following`). :meth:`Pages.tick` is one
step of it (:meth:`~ess.batch.Intake.step`), with or without a
page open, plus what the page shows of the step
(:class:`~.last_step.LastStep`): why rows were refused, how many rows wait,
and what the step raised; then the page of the batch that follows, if it
is open, shows what the step changed, and otherwise the files are listed
again, so that every page sees the files that arrived at its next poll.
:func:`~.api.create_app` owns the timer that calls it, every
:attr:`Pages.interval` seconds.
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
    What the pages of the batches of ``intake.manager`` share. It owns, and
    only it writes, the files listed and their corrections (:attr:`runs`),
    and the step of Follow and what it said (:attr:`last_step`); a
    :class:`~.page.Page` writes its batch through the manager. Each public
    method takes :attr:`lock`, which a caller may hold already. See the
    module.

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
        Draws results (see :data:`~.page.Draw`), by the name of the spec of
        a batch.
    files:
        The file source of the client, to correct runs in.
    marks:
        The fields of a run that a person corrects, as a pydantic model.
    folder:
        What the header names as the place of the data, such as a folder.
    writers:
        The writers of each spec, by its name, that the form of the tab
        "Output" may pick (:class:`~..writing.Writing`), beside
        :data:`~ess.apps.writers.SCIPP_HDF5`.
    catalogue:
        The catalogue that the tab "Output" publishes results to
        (:class:`~..publish.Publisher`); without it, the tab has no button
        "Publish", and the grid no column "Published". With it, the owner
        and contact of output options not set are the principal investigator of the
        proposal, as the catalogue says, asked once here.

    Attributes
    ----------
    lock:
        The lock of every request of every page and of each step of Follow.
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
        The page of ``label``, made at the first call. Raises
        :class:`~.errors.NotFound` if no batch has the label
        (:meth:`check`), and :class:`~.errors.Rejected` if its batch cannot
        be made, such as when the workspace binds no spec of its name, or its
        batch table cannot be read (:meth:`~ess.batch.BatchManager.batch`).
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
        Raise :class:`~.errors.NotFound` unless a batch has ``label``; the
        page of a label that has none is forgotten, whoever asks, since no
        request is served by it again and a batch made later with the label
        gets a page of its own.
        """
        with self.lock:
            if label not in self.intake.manager.headers():
                self._pages.pop(label, None)
                raise NotFound(f'There is no batch {label}.')

    def home(self) -> str | None:
        """
        The label of the batch that follows, else that of the only batch;
        ``None`` if no batch follows and there are none or several, so that
        ``/`` lists them rather than guess.
        """
        with self.lock:
            if (label := self.intake.following) is not None:
                return label
            headers = self.intake.manager.headers()
            return next(iter(headers)) if len(headers) == 1 else None

    def tick(self) -> None:
        """
        A step of Follow on the batch that follows, if one does
        (:meth:`~ess.batch.Intake.step`), and what the page shows
        of it; then :meth:`Page.after_step <.page.Page.after_step>` of its page if
        it is open, else :meth:`Runs.list_again <.runs.Runs.list_again>`,
        so that every page and the Runs view see the files that arrived.
        :func:`~.api.create_app` calls it on a timer. What the step raises is
        said on the page (:meth:`LastStep.shown <.last_step.LastStep.shown>`),
        and the next step tries again.
        """
        # Under the lock, so that a tick that waited for a request sees what
        # that request did: no step after "Stopped following". What cannot be
        # said, such as an exception whose text raises, must not end the timer.
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
        What a new batch can be made with: each registered source that a spec
        fits (:func:`~ess.batch.intake.fits`), then no source, which every spec
        fits, each with the specs that fit it. The specs are those that the
        workspace binds, of the version that
        :meth:`~ess.batch.BatchManager.create` takes. The parameters
        offered as columns are those that the source fills, and the others
        whose cell the page edits.
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
        The rows that the source registered as ``source`` makes now, each
        row marked as taken, left out, or waiting, for a new batch of the
        :class:`~ess.batch.Range` of ``first``, ``last`` and ``later``
        (:meth:`~ess.batch.Intake.marked`). Raises
        :class:`~.errors.Rejected` if no source is registered so, if the
        range is refused, and with what the source refuses
        (:func:`~.errors.reject_on_error`).
        """
        with self.lock:
            with reject_on_error():
                given = self.intake.marked(source, Range(first, last, later))
            return view.listed(given, self.intake.sources[source].source_columns)

    def create(self, new: NewBatch, file: tuple[str, bytes] | None = None) -> Made:
        """
        Make the batch of ``new`` in one call, with the :class:`Intake` if it
        has a source (:meth:`~ess.batch.Intake.create`), else with the
        manager (:meth:`~ess.batch.BatchManager.create`), so that a
        refusal leaves no batch: with a source, of the rows of the
        :class:`~ess.batch.Range` of ``new.first``, ``new.last`` and
        ``new.later`` of the rows of the source complete now, with an empty
        column for each other of ``new.columns``; with a table ``file``, its
        name and content, of its rows
        (:func:`~ess.batch.table.read_table`); else of a batch table of
        ``new.columns`` without rows. With ``new.follow``, the batch follows
        from then on, in place of the batch that followed before, and the
        timer steps it. The batch layer decides which ranges it takes.

        It starts with the settings of the batch ``new.settings_from``, else
        with those that the application gives its spec. A setting at a column
        of ``new.columns`` stays, and each row with an empty cell takes it;
        the :class:`Intake` leaves out the setting of a column that the
        source fills, whose cells are those of the source.

        Raises :class:`~.errors.Rejected` with what the file, the manager,
        the :class:`Intake` or the source refuses, for a label of
        ``settings_from`` that is no batch, if the definitions
        refuse a write, and with what the source raises otherwise
        (:func:`~.errors.reject_on_error`).
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
                if new.follow:  # what was said of the batch that followed before
                    self.last_step.failure = None
                return Made(label=batch.label)
            batch = manager.create(
                new.spec,
                title=new.title,
                description=new.description,
                rows=rows,
                parameter_columns=new.parameter_columns,
                settings=settings,
            )
        return Made(label=batch.label)

    def remove(self, label: str) -> Notice:
        """
        Remove the batch of ``label`` with the manager
        (:meth:`~ess.batch.BatchManager.remove`): its definitions go,
        its records stay, and if it follows, none follows. Its page goes, and
        a page handed out before answers :class:`~.errors.NotFound`. What
        it did, in words. Raises :class:`~.errors.NotFound` if no batch has
        the label, and :class:`~.errors.Rejected` if the definitions refuse
        a write.
        """
        with self.lock, reject_on_write_error():
            self.check(label)
            title = self.intake.manager.headers()[label].title
            self.intake.manager.remove(label)
            self._pages.pop(label, None)
            return Notice(notice=f'Removed {title}.')

    def counts(self) -> BatchCounts:
        """
        The rows of each batch by status, in the words of the page of each
        batch (:attr:`~ess.batch.RowState.status`), and the files that
        each batch names (:meth:`~ess.batch.Batch.files`), which the Runs
        view joins to its runs. Every page and Follow wait while it counts.

        The state of each batch is read as its page reads it, without asking
        its source, and not compared while its settings are refused
        (:meth:`~ess.batch.Batch.shown`). A batch that
        cannot be made, such as one whose batch table cannot be read, has its
        reason in ``failed``, and names no files.
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
    ``spec`` as a choice of the source that fills ``source_columns``: those first,
    then each other parameter whose cell the page edits.
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the pages of the batches of a workspace share, without HTTP:
:class:`Pages`. :mod:`.api` serves the page of each batch of a
:class:`~ess.apps.batch.BatchManager` at ``/batches/<label>/``; each
:class:`~.page.Page` reads what is shared from here.

The pages share one lock. Every request of every page and each step of Follow
take it, so that what the pages share, such as the corrections of runs and
the batches of the manager, changes in one request at a time. One person
works with the application, so a request waits for at most one step, or for
the counts (:meth:`Pages.counts`), which read the state of every batch.

The pages share one listing of what the client lists (:attr:`Pages.listing`):
the files, with their metadata, and the records. A page asks to list again
(:meth:`Pages.list_again`) when the records or its batch table changed, at a step
of Follow, at "List runs again", and as it is made; :class:`Pages` counts
the changes of the files and of why they cannot be listed
(:attr:`Pages.files_version`), which every page compares at its poll.
The runs are the files, the same on every page (:meth:`Pages.runs`), and a
person corrects them on any page, with or without a batch
(:meth:`Pages.correct`). A correction changes no row of a batch: a
batch table names files, and the page shows each with its metadata now. The Runs
view names the batches of each run from the files that each batch names,
which come with the counts (:meth:`Pages.counts`), since Follow adds rows
without a change of the runs.

A person makes a batch from the list of batches, of the sources and specs
that :meth:`Pages.sources` offers, in one of three ways
(:meth:`Pages.create`): from a source, of a range of the rows that it makes
now (:meth:`Pages.rows_of`); from a table file; or without rows. Made from
the page of a batch, it may start with the settings of that batch. A batch
made from a source may follow it from then on, in place of the batch that
followed before; no other action of the page makes a batch follow
(:meth:`~ess.apps.batch.Intake.follow` does, for the command). The manager and the
intake decide what can be made. With no batch at all, the page is served
without one: it shows the list of batches and the runs. So is the page of a
batch that cannot be made (:meth:`Pages.page`), with the reason, and that of
a batch removed (:meth:`Pages.remove`): a :class:`~.page.Page` handed out
before answers :class:`~.refusals.Missing` from then on.

``Intake`` keeps which batch follows
(:attr:`~ess.apps.batch.Intake.following`). :meth:`Pages.tick` is one
step of it (:meth:`~ess.apps.batch.Intake.step`), with or without a
page open, plus what the page shows of the step
(:meth:`Pages.follow_shown`, :meth:`Pages.unsubmitted`): why rows were
refused, how many rows wait, and what the step raised; then
the page of the batch that follows shows what the step changed.
:func:`~.api.create_app` owns the timer that calls it, every
:attr:`Pages.interval` seconds.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ess.spec import File, FileRef, WorkflowSpec

from ...client import Client
from ...errors import said
from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ..auto import Step
from ..corrections import CorrectedFiles
from ..intake import Intake, fits, step_failure
from ..source import Range, RowSource
from ..state import RowState, not_reduced
from ..table import read_table
from ..writing import Writing
from . import forms
from .models import (
    BatchCounts,
    Batches,
    BatchRow,
    CellEdit,
    Cells,
    Column,
    Following,
    Made,
    NewBatch,
    Notice,
    Param,
    RunCell,
    RunRow,
    Runs,
    SourceChoice,
    SourceRows,
    SpecChoice,
)
from .page import Draw, Page, listed, read, read_file
from .refusals import Missing, Problem, kept, refused, why

if TYPE_CHECKING:
    from ...scicat import Catalogue
    from ...writers import Writer
    from ..publish import Publisher


class SafeListing(Listing):
    """
    What the client lists now, as :class:`Listing` gives it, but without files
    if the file source raises: a picker then offers none, the Runs view lists
    none, and :attr:`failure` says why. It lists every file with its metadata
    as it is made (:attr:`files_listed`), so that :class:`Pages` sees a
    change of the files.
    """

    error: str | None = None
    """What the file source raised, ``None`` if it raised nothing."""

    def __init__(self, client: Client, previous: Listing | None = None) -> None:
        super().__init__(client, previous)
        self.files_listed = self.listed(Selector())

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


class Pages:
    """
    What the pages of the batches of ``intake.manager`` share. It owns, and only it
    writes, the listing of the files and its version, the corrections of
    runs, and the step of Follow and what it said; a :class:`~.page.Page`
    writes its batch through the manager. Each public method takes
    :attr:`lock`, which a caller may hold already; a property reads one value
    without it. See the module.

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
        and contact of a form not set are the principal investigator of the
        proposal, as the catalogue says, asked once here.

    Attributes
    ----------
    lock:
        The lock of every request of every page and of each step of Follow.
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
            from ...scicat import principal_investigator  # needs the extra scicat
            from ..publish import Publisher

            proposal = intake.manager.client.proposal
            self.writing = Writing(
                intake.manager, writers, principal_investigator(catalogue, proposal)
            )
            self.publisher = Publisher(self.writing, catalogue)
        self.interval = interval
        self.title = title
        self.draw = {} if draw is None else draw
        # What a person corrects of a run, and where; None if nothing.
        self._marking = None if marks is None or files is None else (marks, files)
        self.folder = folder
        self.lock = threading.RLock()
        # What Follow said: what its last step raised, and the label and the
        # last step that did not raise, whose refusals :meth:`applied` empties.
        self._failure: str | None = None
        self._last: tuple[str, Step] | None = None
        self._listing = SafeListing(intake.manager.client)
        # The files of the listing with their metadata, and why they cannot
        # be listed, as at :attr:`files_version`.
        self._files = (self._listing.files_listed, self._listing.failure)
        self._files_version = 1
        self._pages: dict[str, Page] = {}

    def page(self, label: str) -> Page:
        """
        The page of ``label``, made at the first call. Raises
        :class:`~.refusals.Missing` if no batch has the label
        (:meth:`check`), and :class:`~.refusals.Problem` if its batch cannot
        be made, such as when the workspace binds no spec of its name, or its
        batch table cannot be read (:meth:`~ess.apps.batch.BatchManager.batch`).
        """
        with self.lock:
            self.check(label)
            if (page := self._pages.get(label)) is None:
                try:
                    page = Page(self, label)
                except (ValueError, OSError) as error:
                    raise Problem(f'Cannot show {label}: {error}') from None
                self._pages[label] = page
            return page

    def check(self, label: str) -> None:
        """
        Raise :class:`~.refusals.Missing` unless a batch has ``label``; the
        page of a label that has none is forgotten, whoever asks, since no
        request is served by it again and a batch made later with the label
        gets a page of its own.
        """
        with self.lock:
            if label not in self.intake.manager.headers():
                self._pages.pop(label, None)
                raise Missing(f'There is no batch {label}.')

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
        (:meth:`~ess.apps.batch.Intake.step`), and what the page shows
        of it; then :meth:`Page.stepped <.page.Page.stepped>` of its page.
        :func:`~.api.create_app` calls it on a timer. What the step raises is
        said on the page (:meth:`follow_shown`), and the next step tries
        again.
        """
        # Under the lock, so that a tick that waited for a request sees what
        # that request did: no step after "Stopped following". What cannot be
        # said, such as an exception whose text raises, or a page that cannot
        # be made, must not end the timer.
        with self.lock, suppress(Exception):
            if (label := self.intake.following) is None:
                return
            try:
                if (done := self.intake.step()) is None:
                    return
                self._last = (label, done)
                self._failure = None
            except Exception as error:  # the next step tries again
                self._failure = step_failure(error)
            self.page(label).stepped()

    @property
    def listing(self) -> SafeListing:
        """
        What the client lists, as :meth:`list_again` listed it last: the
        files, their labels and the records, for every page.
        """
        return self._listing

    @property
    def files_version(self) -> int:
        """Counts the changes of the files listed, and of why they cannot be."""
        return self._files_version

    @property
    def runs_version(self) -> int | None:
        """
        :attr:`files_version`, which the Runs view of each page compares to
        ask for the runs again; ``None`` if the application corrects no runs,
        so that the pages have no Runs view.
        """
        return self._files_version if self.corrects_runs else None

    @property
    def corrects_runs(self) -> bool:
        """Whether the application corrects runs: it gives their marks and files."""
        return self._marking is not None

    def list_again(self) -> None:
        """
        List what the client lists now (:attr:`listing`): when the records
        or a table changed, at a step of Follow, at "List runs again", as a
        page is made, and for the Runs view. :attr:`files_version` moves if
        the files or why they cannot be listed differ from the last listing.
        """
        with self.lock:
            self._listing = SafeListing(
                self.intake.manager.client, previous=self._listing
            )
            self._new_files()

    def _new_files(self) -> None:
        """Move :attr:`files_version` if the files of :attr:`listing` changed."""
        files = (self._listing.files_listed, self._listing.failure)
        if files != self._files:
            self._files = files
            self._files_version += 1

    def _marks(self) -> tuple[Fields, CorrectedFiles]:
        """
        The form of what a person corrects of a run, and the files to
        correct; :class:`~.refusals.Problem` if the application corrects no runs.
        """
        if self._marking is None:
            raise Problem('This application corrects no runs.')
        marks, files = self._marking
        return Fields(marks, self._listing), files

    def runs(self) -> Runs:
        """
        The runs, listed again, with what a person corrected of each, as the
        Runs view of every page shows them, and that of the page without a
        batch; none, with why, if the file source raises. Raises
        :class:`~.refusals.Problem` if the application corrects no runs.
        """
        with self.lock:
            marks, files = self._marks()
            self.list_again()
            listing = self._listing
            columns = [
                Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                for n, f in marks.fields.items()
            ]

            def text(value: Any) -> str:
                return '' if value is UNSET else listing.text(value)

            rows = []
            for ref in listing.files_listed:
                metadata = files.metadata(ref)
                original = files.source.metadata(ref)
                corrected = files.corrections(ref)
                cells = {
                    name: RunCell(
                        value=forms.cell(field, metadata.get(name, UNSET)),
                        text=text(metadata.get(name, UNSET)),
                        edited=name in corrected,
                        original=text(original.get(name, UNSET)),
                    )
                    for name, field in marks.fields.items()
                }
                label = listing.file_label(ref)
                rows.append(RunRow(key=ref.file, file=label, cells=cells))
            return Runs(
                version=self._files_version,
                columns=columns,
                rows=rows,
                problem=listing.failure,
            )

    def correct(self, edit: CellEdit) -> Notice:
        """Correct a field of the runs ``edit.keys``, as its field reads the value."""
        with self.lock:
            marks, files = self._marks()
            value = read(marks, edit)
            fields = {edit.column: None if value is UNSET else value}
            self._kept(
                lambda: files.correct_each(
                    [FileRef(file=key) for key in edit.keys], **fields
                )
            )
            n = len(edit.keys)
            title = marks.titles[edit.column]
            return Notice(notice=f'{title} corrected on {n} run{"s" * (n > 1)}.')

    def revert_run(self, cells: Cells) -> Notice:
        """Take a field of runs back to what the files say, as :meth:`correct`."""
        with self.lock:
            marks, files = self._marks()
            self._kept(
                lambda: files.reset_each(
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

    def _step(self, label: str) -> Step | None:
        """Follow's last step that did not raise, if on ``label``, which follows."""
        if self.intake.following != label or self._last is None:
            return None
        on, step = self._last
        return step if on == label else None

    def follow_token(self, label: str) -> object:
        """
        What the page of the batch of ``label`` shows of the last step of
        Follow, as a value to compare with one given before, and not to
        read: equal while what the page shows of Follow stays the same.
        """
        with self.lock:
            if self.intake.following != label:
                return None
            step = self._step(label)
            shown = None if step is None else (len(step.waiting), step.refused)
            return self._failure, shown

    def follow_shown(self, label: str) -> Following:
        """
        Follow, as the page of the batch of ``label`` shows it: whether the
        batch follows, and while it does, what the last step raised and how
        many rows wait.
        """
        with self.lock:
            on = self.intake.following == label
            step = self._step(label)
            return Following(
                on=on,
                source=self.intake.source(label),
                interval=self.interval,
                failure=self._failure if on else None,
                waiting=0 if step is None else len(step.waiting),
            )

    def unsubmitted(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row of ``states`` that its next step
        would try again, by key; none unless the batch of ``label`` follows.
        """
        with self.lock:
            if (step := self._step(label)) is None:
                return {}
            return {
                key: f'Follow could not reduce it: {forms.message(step.refused[key])}'
                for key in not_reduced(states)
                if key in step.refused
            }

    def applied(self, label: str) -> None:
        """
        Forget why Follow could not submit rows of the batch of ``label``,
        whose page applied settings: its next step tries them with these.
        """
        with self.lock:
            if (step := self._step(label)) is not None:
                self._last = (label, replace(step, refused={}))

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
                runs=self.runs_version,
                rows=rows,
            )

    def sources(self) -> list[SourceChoice]:
        """
        What a new batch can be made with: each registered source that a spec
        fits (:func:`~ess.apps.batch.intake.fits`), then no source, which every spec
        fits, each with the specs that fit it. The specs are those that the
        workspace binds, of the version that
        :meth:`~ess.apps.batch.BatchManager.create` takes. The parameters
        offered as columns are those that the source fills, and the others
        whose cell the page edits.
        """
        with self.lock:
            client = self.intake.manager.client
            names = dict.fromkeys(spec.name for spec in client.specs())
            specs = [client.spec_named(name) for name in names]
            listing = self._listing

            def choice(name: str | None, source: RowSource | None) -> SourceChoice:
                fills = () if source is None else source.fills
                return SourceChoice(
                    name=name,
                    fills=list(fills),
                    specs=[
                        _spec_choice(spec, fills, listing)
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
        :class:`~ess.apps.batch.Range` of ``first``, ``last`` and ``later``
        (:meth:`~ess.apps.batch.Intake.marked`). Raises
        :class:`~.refusals.Problem` if no source is registered so, if the
        range is refused, and with what the source refuses
        (:func:`~.refusals.refused`).
        """
        with self.lock:
            with refused():
                given = self.intake.marked(source, Range(first, last, later))
            return listed(given, self.intake.sources[source].fills)

    def create(self, new: NewBatch, file: tuple[str, bytes] | None = None) -> Made:
        """
        Make the batch of ``new`` in one call, with the :class:`Intake` if it
        has a source (:meth:`~ess.apps.batch.Intake.create`), else with the
        manager (:meth:`~ess.apps.batch.BatchManager.create`), so that a
        refusal leaves no batch: with a source, of the rows of the
        :class:`~ess.apps.batch.Range` of ``new.first``, ``new.last`` and
        ``new.later`` of those that the source has filled now, with an empty
        column for each other of ``new.columns``; with a table ``file``, its
        name and content, of its rows
        (:func:`~ess.apps.batch.table.read_table`); else of a batch table of
        ``new.columns`` without rows. With ``new.follow``, the batch follows
        from then on, in place of the batch that followed before, and the
        timer steps it. The batch layer decides which ranges it takes.

        It starts with the settings of the batch ``new.settings_from``, else
        with those that the application gives its spec. A setting at a column
        of ``new.columns`` stays, and each row with an empty cell takes it;
        the :class:`Intake` leaves out the setting of a column that the
        source fills, whose cells are those of the source.

        Raises :class:`~.refusals.Problem` with what the file, the manager,
        the :class:`Intake` or the source refuses, for a label of
        ``settings_from`` that is no batch, if the definitions
        refuse a write, and with what the source raises otherwise
        (:func:`~.refusals.refused`).
        """
        with self.lock, refused('The batch was not made'):
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
                if new.follow:
                    self._failure = None  # said of the batch that followed before
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
        (:meth:`~ess.apps.batch.BatchManager.remove`): its definitions go,
        its records stay, and if it follows, none follows. Its page goes, and
        a page handed out before answers :class:`~.refusals.Missing`. What
        it did, in words. Raises :class:`~.refusals.Missing` if no batch has
        the label, and :class:`~.refusals.Problem` if the definitions refuse
        a write.
        """
        with self.lock, kept():
            self.check(label)
            title = self.intake.manager.headers()[label].title
            self.intake.manager.remove(label)
            self._pages.pop(label, None)
            return Notice(notice=f'Removed {title}.')

    def counts(self) -> BatchCounts:
        """
        The rows of each batch by status, in the words of the page of each
        batch (:attr:`~ess.apps.batch.RowState.text`), and the files that
        each batch names (:meth:`~ess.apps.batch.Batch.files`), which the Runs
        view joins to its runs. Every page and Follow wait while it counts.

        The state of each batch is read as its page reads it, without asking
        its source, and not compared while its settings are refused
        (:meth:`~ess.apps.batch.Batch.shown`). A batch that
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
                    failed[label] = why(error)
                    continue
                rows[label] = dict(Counter(s.text for s in states.values()))
                files[label] = sorted(ref.file for ref in named)
            return BatchCounts(rows=rows, files=files, failed=failed)


def _spec_choice(
    spec: WorkflowSpec[Any, Any], fills: Sequence[str], listing: Listing
) -> SpecChoice:
    """
    ``spec`` as a choice of the source that fills ``fills``: those first,
    then each other parameter whose cell the page edits.
    """
    form = Fields(spec.params, listing)
    names = [*fills, *(n for n in forms.edited(form) if n not in fills)]
    return SpecChoice(
        name=spec.name,
        title=spec.title,
        params=[Param(name=n, title=form.titles[n]) for n in names],
    )

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

The runs are the same on every page (:meth:`Pages.runs`), and a person
corrects them on any page, with or without a batch (:meth:`Pages.correct`).
A correction changes no row of a batch: a table names files, and the page
shows each with its metadata now. The Runs view names the batches of each
run from the files that each batch names, which come with the counts
(:meth:`Pages.counts`), since Follow adds rows without a change of the runs.

A person makes a batch from the list of batches, of the sources and specs
that :meth:`Pages.sources` offers, in one of three ways
(:meth:`Pages.create`): from a source, of a range of the rows that it makes
now (:meth:`Pages.rows_of`); from a table file; or without rows. Made from
the page of a batch, it may start with the settings of that batch. A batch
made from a source may follow it from then on, in place of the batch that
followed before; no other action makes a batch follow. The manager decides
what can be made. With no batch at all, the page is served
without one: it shows the list of batches and the runs. So is the page of a
batch that cannot be made (:meth:`Pages.page`), with the reason, and that of
a batch removed (:meth:`Pages.remove`): a :class:`~.page.Page` handed out
before answers :class:`~.page.Missing` from then on.

The manager keeps which batch follows
(:attr:`~ess.apps.batch.BatchManager.following`). :meth:`Pages.tick` is one
step of it (:meth:`~ess.apps.batch.BatchManager.step`), with or without a
page open, plus what the page shows of the step (:meth:`Pages.following`):
why rows were refused, how many rows wait, and what the step raised; then
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
from typing import Any

import pandas as pd
from pydantic import BaseModel

from ess.spec import FileRef, WorkflowSpec

from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from ..auto import Step
from ..corrections import CorrectedFiles
from ..definition import NotKept
from ..manager import BatchManager, fits
from ..source import Source, between
from ..state import RowState, not_reduced
from ..table import read_table, with_column
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
from .page import (
    Draw,
    Missing,
    Page,
    Problem,
    listed,
    read,
    read_file,
    why,
)


class Pages:
    """
    What the pages of the batches of ``manager`` share, which only it
    writes: the lock, the runs and their corrections, and the step of Follow
    and what it said. See the module.

    Parameters
    ----------
    manager:
        The batches.
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

    Attributes
    ----------
    lock:
        The lock of every request of every page and of each step of Follow.
    runs_version:
        Counts the changes of the runs, so that the Runs view of each page
        asks for them again.
    failure:
        What the last step of Follow raised, ``None`` if it raised nothing.
    last:
        The label of the batch that the last step of Follow that did not
        raise was on, and the step (:class:`~ess.apps.batch.auto.Step`), with
        why it refused rows and which rows of the source wait; ``None``
        before the first. What it refused is said only while that batch
        follows, and emptied when its page applies settings
        (:meth:`applied`). A page reads both through :meth:`said` and
        :meth:`following`.
    """

    def __init__(
        self,
        manager: BatchManager,
        *,
        interval: float = 5.0,
        title: str = '',
        draw: Mapping[str, Draw] | None = None,
        files: CorrectedFiles | None = None,
        marks: type[BaseModel] | None = None,
        folder: str = '',
    ) -> None:
        self.manager = manager
        self.interval = interval
        self.title = title
        self.draw = {} if draw is None else draw
        # What a person corrects of a run, and where; None if nothing.
        self._marking = None if marks is None or files is None else (marks, files)
        self.folder = folder
        self.lock = threading.RLock()
        self.runs_version = 1
        # The runs, with their metadata, as listed at the last version.
        self._runs: Mapping[FileRef, Mapping[str, Any]] = {}
        # What the runs, their corrections and a new batch are read with,
        # listed again for each.
        self._listing: Listing | None = None
        self.failure: str | None = None
        self.last: tuple[str, Step] | None = None
        self._pages: dict[str, Page] = {}

    def page(self, label: str) -> Page:
        """
        The page of ``label``, made at the first call. Raises
        :class:`Missing` if no batch has the label, and
        :class:`~.page.Problem` if its batch cannot be made, such as when the
        workspace binds no spec of its name, or its table cannot be read
        (:meth:`~ess.apps.batch.BatchManager.batch`).
        """
        with self.lock:
            if label not in self.manager.headers():
                self._pages.pop(label, None)
                raise Missing(f'There is no batch {label}.')
            if (page := self._pages.get(label)) is None:
                try:
                    page = Page(self, label)
                except (ValueError, OSError) as error:
                    raise Problem(f'Cannot show {label}: {error}') from None
                self._pages[label] = page
            return page

    def home(self) -> str | None:
        """
        The label of the batch that follows, else that of the only batch;
        ``None`` if no batch follows and there are none or several, so that
        ``/`` lists them rather than guess.
        """
        with self.lock:
            if (label := self.manager.following) is not None:
                return label
            headers = self.manager.headers()
            return next(iter(headers)) if len(headers) == 1 else None

    def tick(self) -> None:
        """
        A step of Follow on the batch that follows, if one does
        (:meth:`~ess.apps.batch.BatchManager.step`), and what the page shows
        of it; then :meth:`Page.stepped <.page.Page.stepped>` of its page.
        :func:`~.api.create_app` calls it on a timer. What the step raises is
        :attr:`failure`, and the next step tries again.
        """
        # Under the lock, so that a tick that waited for a request sees what
        # that request did: no step after "Stopped following". What cannot be
        # said, such as an exception whose text raises, or a page that cannot
        # be made, must not end the timer.
        with self.lock, suppress(Exception):
            if (label := self.manager.following) is None:
                return
            try:
                if (done := self.manager.step()) is None:
                    return
                self.last = (label, done)
                self.failure = None
            except Exception as error:  # the next step tries again
                self.failure = f'Follow could not step: {str(error) or repr(error)}'
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

    @property
    def corrects_runs(self) -> bool:
        """Whether the application corrects runs: it gives their marks and files."""
        return self._marking is not None

    def _relisted(self) -> Listing:
        """What the client lists now."""
        self._listing = Listing(self.manager.client, previous=self._listing)
        return self._listing

    def _marked(self) -> tuple[Fields, CorrectedFiles, Listing]:
        """
        The form of what a person corrects of a run, the files to correct,
        and what the client lists now, which the form reads;
        :class:`~.page.Problem` if the application corrects no runs.
        """
        if self._marking is None:
            raise Problem('This application corrects no runs.')
        marks, files = self._marking
        listing = self._relisted()
        return Fields(marks, listing), files, listing

    def runs(self) -> Runs:
        """
        The runs, with what a person corrected of each, as the Runs view of
        every page shows them, and that of the page without a batch; none,
        with why, if the file source raises. Raises :class:`~.page.Problem`
        if the application corrects no runs.
        """
        with self.lock:
            marks, files, listing = self._marked()
            columns = [
                Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                for n, f in marks.fields.items()
            ]
            try:
                refs = files.list(Selector())
            except Exception as error:  # the file source is code of the application
                problem = f'The files cannot be listed: {error}'
                return Runs(
                    version=self.runs_version, columns=columns, rows=[], problem=problem
                )

            def text(value: Any) -> str:
                return '' if value is UNSET else listing.text(value)

            rows = []
            for ref in refs:
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
            return Runs(version=self.runs_version, columns=columns, rows=rows)

    def correct(self, edit: CellEdit) -> Notice:
        """Correct a field of the runs ``edit.keys``, as its field reads the value."""
        with self.lock:
            marks, files, _ = self._marked()
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
            marks, files, _ = self._marked()
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
        Make ``correction``, then have the pages show the runs again. Raises
        :class:`~.page.Problem` if the definitions do not keep it, or if the
        runs cannot be listed after it.
        """
        try:
            correction()
        except NotKept as error:
            raise Problem(f'The change was not kept: {error}') from None
        try:
            runs = self._relisted().listed(Selector())
        except Exception as error:  # the file source of the application
            raise Problem(
                f'Corrected, but the runs cannot be listed: {error}'
            ) from None
        self.runs_listed(runs, changed=True)

    def said(self, label: str) -> tuple[str | None, Step | None] | None:
        """
        What Follow said of the batch of ``label``: what its last step raised,
        and its last step that did not raise; ``None`` unless the batch
        follows. A page compares it to see a step that changed what it shows.
        """
        if self.manager.following != label:
            return None
        step = None if self.last is None or self.last[0] != label else self.last[1]
        return self.failure, step

    def following(
        self, label: str, states: Mapping[str, RowState]
    ) -> tuple[Following, dict[str, str]]:
        """
        What the page of the batch of ``label`` shows of Follow: whether it
        follows, with what the last step raised and how many rows wait; and
        why Follow could not submit each row of ``states`` that its next step
        would try again, by key.
        """
        failure, step = self.said(label) or (None, None)
        shown = Following(
            on=self.manager.following == label,
            source=self.manager.source(label),
            interval=self.interval,
            failure=failure,
            waiting=0 if step is None else len(step.waiting),
        )
        if step is None:
            return shown, {}
        return shown, {
            key: f'Follow could not reduce it: {forms.message(step.refused[key])}'
            for key in not_reduced(states)
            if key in step.refused
        }

    def applied(self, label: str) -> None:
        """
        Forget why Follow could not submit rows of the batch of ``label``,
        whose page applied settings: its next step tries them with these.
        """
        if (said := self.said(label)) is not None and (step := said[1]) is not None:
            self.last = (label, replace(step, refused={}))

    def batches(self) -> Batches:
        """The batches, as the Batches view of each page lists them."""
        with self.lock:
            rows = [
                BatchRow(
                    label=label,
                    title=header.title,
                    description=header.description,
                    follows=label == self.manager.following,
                )
                for label, header in self.manager.headers().items()
            ]
            return Batches(
                application=self.title,
                runs=self.runs_version if self.corrects_runs else None,
                rows=rows,
            )

    def sources(self) -> list[SourceChoice]:
        """
        What a new batch can be made with: each registered source that a spec
        fits (:func:`~ess.apps.batch.fits`), then no source, which every spec
        fits, each with the specs that fit it. The specs are those that the
        workspace binds, of the version that
        :meth:`~ess.apps.batch.BatchManager.create` takes. The parameters
        offered as columns are those that the source fills, and the others
        whose cell the page edits.
        """
        with self.lock:
            client = self.manager.client
            names = dict.fromkeys(spec.name for spec in client.specs())
            specs = [client.spec_named(name) for name in names]
            listing = self._relisted()

            def choice(name: str | None, source: Source | None) -> SourceChoice:
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

            given = [choice(n, s) for n, s in self.manager.sources.items()]
            return [c for c in given if c.specs] + [choice(None, None)]

    def rows_of(
        self,
        source: str,
        first: str | None = None,
        last: str | None = None,
        later: bool = False,
    ) -> SourceRows:
        """
        The table that the source registered as ``source`` makes now, each
        row marked as taken, left out, or waiting, for a new batch of the
        rows that :func:`taken` gives
        (:meth:`~ess.apps.batch.BatchManager.marked`). Raises
        :class:`~.page.Problem` if no source is registered so, with what the
        source raises, or if the range is refused.
        """
        with self.lock:
            try:
                given = self.manager.marked(source, taken(first, last, later))
            except Exception as error:  # a source is code of the application
                said = why(error)
                raise Problem(f'{said[:1].upper()}{said[1:]}.') from None
            return listed(given, self.manager.sources[source].fills)

    def create(self, new: NewBatch, file: tuple[str, bytes] | None = None) -> Made:
        """
        Make the batch of ``new`` with the manager, in one call
        (:meth:`~ess.apps.batch.BatchManager.create`), so that it is made as
        asked or not at all: with a source, of the rows that :func:`taken`
        gives of those that the source has filled now, with an empty column
        for each other of ``new.columns``; with a table ``file``, its name
        and content, of its table (:func:`~ess.apps.batch.table.read_table`);
        else of a table of ``new.columns`` without rows. With ``new.follow``,
        the batch follows from then on, in place of the batch that followed
        before, and the timer steps it; it has no last row, since Follow adds
        every row that arrives later.

        It starts with the settings of the batch ``new.settings_from``, else
        with those that the application gives its spec. A setting of that
        batch at a column of ``new.columns`` is a column with the setting's
        value in each row (:func:`~ess.apps.batch.table.with_column`), unless
        the source fills the column: its cells are those of the source. Raises
        :class:`~.page.Problem` with what the file, the manager or the source
        refuses.
        """
        with self.lock:
            try:
                manager = self.manager
                settings = (
                    None
                    if new.settings_from is None
                    else manager.batch(new.settings_from).settings
                )
                if new.follow and new.last is not None:
                    raise ValueError(
                        'a batch that follows takes every row that arrives '
                        'later, so it has no last row'
                    )
                given = settings or {}
                moved = [c for c in new.columns if c in given]
                columns = [c for c in new.columns if c not in moved]

                def completed(table: pd.DataFrame) -> pd.DataFrame:
                    """
                    ``table`` with the columns of ``new.columns`` it lacks: a
                    column that the source fills keeps its cells.
                    """
                    empty = [c for c in columns if c not in table.columns]
                    table = table.assign(**dict.fromkeys(empty))
                    for name in moved:
                        if name not in table.columns:
                            table = with_column(table, name, given[name])
                    return table

                rows = choose = None
                if file is not None:
                    name, content = file
                    params = manager.client.spec_named(new.spec).params
                    try:
                        rows = read_file(name, content, lambda p: read_table(p, params))
                    except ValueError as error:
                        raise ValueError(f'{name}: {error}') from None
                elif new.source is None:
                    rows = completed(pd.DataFrame())
                else:
                    chosen = taken(new.first, new.last, new.later)

                    def choose(arrived: pd.DataFrame) -> pd.DataFrame:
                        return completed(chosen(arrived))

                label = manager.create(
                    new.spec,
                    title=new.title,
                    description=new.description,
                    rows=rows,
                    source=None if rows is not None else new.source,
                    choose=choose,
                    settings=settings,
                    follow=new.follow,
                )
                if new.follow:
                    self.failure = None  # said of the batch that followed before
            except Exception as error:  # a source is code of the application
                raise Problem(f'The batch was not made: {why(error)}.') from None
            return Made(label=label)

    def remove(self, label: str) -> None:
        """
        Remove the batch of ``label`` with the manager
        (:meth:`~ess.apps.batch.BatchManager.remove`): its definitions go,
        its records stay, and if it follows, none follows. Its page goes, and
        a page handed out before answers :class:`~.page.Missing`. Raises
        :class:`~.page.Missing` if no batch has the label, and
        :class:`~.page.Problem` if the definitions refuse a write.
        """
        with self.lock:
            if label not in self.manager.headers():
                raise Missing(f'There is no batch {label}.')
            try:
                self.manager.remove(label)
                self._pages.pop(label, None)
            except NotKept as error:
                raise Problem(f'The change was not kept: {error}') from None

    def counts(self) -> BatchCounts:
        """
        The rows of each batch by status, in the words of the page of each
        batch (:attr:`~ess.apps.batch.RowState.text`), and the files that
        each batch names (:meth:`~ess.apps.batch.Batch.files`), which the Runs
        view joins to its runs. Every page and Follow wait while it counts.

        The state of each batch is read as its page reads it, without asking
        its source, and not compared while its settings are refused
        (:meth:`~ess.apps.batch.Batch.shown`). A batch that
        cannot be made, such as one whose table cannot be read, has its
        reason in ``failed``, and names no files.
        """
        with self.lock:
            manager = self.manager
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


def taken(
    first: str | None, last: str | None, later: bool
) -> Callable[[pd.DataFrame], pd.DataFrame]:
    """
    Which rows a new batch takes of those that its source has filled now: the
    rows from ``first`` to ``last`` (:func:`~ess.apps.batch.source.between`),
    or, if ``later``, none, so that it gets only the rows added later, by
    Follow or by a person. Raises ``ValueError`` for ``later`` with an end.
    """
    if not later:
        return lambda arrived: between(arrived, first, last)
    if first is not None or last is not None:
        raise ValueError('a batch of the rows that arrive later has no first or last')
    return lambda arrived: arrived.iloc[:0]


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

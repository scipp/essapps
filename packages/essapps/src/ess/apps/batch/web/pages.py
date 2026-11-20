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
shows each with its metadata now.

A person makes a batch from the list of batches, of the sources and specs
that :meth:`Pages.sources` offers, in one of three ways: from a source, of a
range of the rows that it makes now (:meth:`Pages.rows_of`,
:meth:`Pages.create`); from a table file (:meth:`Pages.create_from_table`);
or without rows. Made from the page of a batch, it may start with the
settings of that batch. The manager decides what can be made. With no batch
at all, the page is served without one: it shows the list of batches and the
runs. So is the page of a batch that cannot
be made (:meth:`Pages.page`), with the reason, and that of a batch removed
(:meth:`Pages.remove`): a :class:`~.page.Page` handed out before answers
:class:`~.page.Missing` from then on.

The manager keeps which batch follows
(:attr:`~ess.apps.batch.BatchManager.following`). :meth:`Pages.tick` is one
step of it (:meth:`~ess.apps.batch.BatchManager.step`), with or without a
page open, plus what the page shows of the step: why rows were refused, how
many rows wait, and what the step raised; then the page of the batch that
follows shows what the step changed. :func:`~.api.create_app` owns the timer
that calls it, every :attr:`Pages.interval` seconds.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import suppress
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
from ..source import Source, filled
from ..state import RowState, not_reduced
from ..table import read_table
from . import forms
from .models import (
    BatchCounts,
    Batches,
    BatchRow,
    CellEdit,
    Cells,
    Column,
    Made,
    NewBatch,
    Notice,
    Param,
    RunCell,
    RunRow,
    Runs,
    SourceChoice,
    SourceRow,
    SourceRows,
    SpecChoice,
)
from .page import (
    NAME,
    Draw,
    Missing,
    Page,
    Problem,
    named_file,
    names,
    read,
    why,
)


class Pages:
    """
    What the pages of the batches of ``manager`` share. See the module.

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
        follows, and emptied when its page applies settings.
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

    def unsubmitted(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row of ``states`` that its next step
        would try again, by key; none unless the batch of ``label`` follows.
        """
        if self.last is None or self.manager.following != label:
            return {}
        of, done = self.last
        if of != label:
            return {}
        return {
            key: f'Follow could not reduce it: {forms.message(done.refused[key])}'
            for key in not_reduced(states)
            if key in done.refused
        }

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

    def rows_of(self, source: str) -> SourceRows:
        """
        The table that the source registered as ``source`` makes now
        (:meth:`~ess.apps.batch.BatchManager.rows_of`), with the rows that
        wait marked (:func:`~ess.apps.batch.source.filled`), for a person to
        choose the rows of a new batch among them. Raises
        :class:`~.page.Problem` if no source is registered so, or with what
        the source raises.
        """
        with self.lock:
            try:
                given = self.manager.rows_of(source)
            except Exception as error:  # a source is code of the application
                raise Problem(
                    f'The rows of {source} cannot be listed: {why(error)}.'
                ) from None
            fills = self.manager.sources[source].fills
            return _listed(given, ~filled(given, fills), fills)

    def offered(self, label: str) -> SourceRows:
        """
        The rows of the source of the batch of ``label`` that its table
        lacks, those that wait marked
        (:meth:`~ess.apps.batch.BatchManager.offered`), for "Add rows from the
        runs" on its page (:meth:`Page.take <.page.Page.take>`). Raises
        :class:`~.page.Missing` if no batch has the label, and
        :class:`~.page.Problem` if it has no source, or with what the source
        raises.
        """
        with self.lock:
            if label not in self.manager.headers():
                raise Missing(f'There is no batch {label}.')
            try:
                given = self.manager.offered(label)
            except Exception as error:  # a source is code of the application
                raise Problem(
                    f'The rows of the source cannot be listed: {why(error)}.'
                ) from None
            fills = self.manager.sources[self.manager.source(label)].fills
            return _listed(given.drop(columns='waits'), given['waits'], fills)

    def create(self, new: NewBatch) -> Made:
        """
        Make the batch of ``new`` with the manager
        (:meth:`~ess.apps.batch.BatchManager.create`): with a source, of the
        rows from ``new.first`` to ``new.last`` among those that the source
        has filled now, with an empty column for each other of
        ``new.columns``; without, of a table of ``new.columns`` without rows.
        It starts with the settings of the batch ``new.settings_from``, else
        with those that the application gives its spec. Raises
        :class:`~.page.Problem` with what the manager refuses, or what the
        source raises.
        """
        with self.lock:
            try:
                settings = self._settings(new.spec, new.settings_from)
                if new.source is None:
                    rows = pd.DataFrame(columns=new.columns)
                    label = self.manager.create(
                        new.spec,
                        title=new.title,
                        rows=rows,
                        description=new.description,
                        settings=settings,
                    )
                else:

                    def choose(given: pd.DataFrame) -> pd.DataFrame:
                        for end in (new.first, new.last):
                            if end is not None and end not in given.index:
                                raise ValueError(
                                    f'{new.source} has no filled row {end}'
                                )
                        taken = given.loc[new.first : new.last]
                        empty = [c for c in new.columns if c not in taken.columns]
                        return taken.assign(**dict.fromkeys(empty))

                    label = self.manager.create(
                        new.spec,
                        title=new.title,
                        source=new.source,
                        choose=choose,
                        description=new.description,
                        settings=settings,
                    )
            except Exception as error:  # a source is code of the application
                raise Problem(f'The batch was not made: {why(error)}.') from None
            return Made(label=label)

    def create_from_table(
        self,
        spec: str,
        title: str,
        name: str,
        content: bytes,
        *,
        description: str = '',
        settings_from: str | None = None,
    ) -> Made:
        """
        Make a batch of ``spec`` without a source, of the table of the table
        file ``name`` that holds ``content``
        (:func:`~ess.apps.batch.table.read_table`), with settings as
        :meth:`create` gives them. A column of the file named as no parameter
        is a note, also one misspelt. Raises :class:`~.page.Problem` with what
        the file or the manager refuses.
        """
        with self.lock:
            try:
                settings = self._settings(spec, settings_from)
                with named_file(name, content) as path:
                    rows = read_table(path, self.manager.client.spec_named(spec).params)
                label = self.manager.create(
                    spec,
                    title=title,
                    rows=rows,
                    description=description,
                    settings=settings,
                )
            except (OSError, ValueError, KeyError) as error:
                raise Problem(f'The batch was not made: {why(error)}.') from None
            return Made(label=label)

    def _settings(self, spec: str, label: str | None) -> Mapping[str, Any] | None:
        """
        The settings of the batch of ``label``, for a new batch of ``spec``;
        ``None``, those that the application gives ``spec``, if no label.
        Raises ``ValueError`` if no batch has the label, or if its spec is
        another.
        """
        if label is None:
            return None
        if label not in self.manager.headers():
            raise ValueError(f'there is no batch {label}')
        batch = self.manager.batch(label)
        if batch.spec.name != spec:
            raise ValueError(f'the settings of {label} are of another workflow')
        return batch.settings

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
        batch (:attr:`~ess.apps.batch.RowState.text`). Every page and Follow
        wait while it counts.

        The state of each batch is read as its page reads it, without asking
        its source, and not compared while its settings are refused
        (:meth:`~ess.apps.batch.Batch.shown`). A batch that
        cannot be made, such as one whose table cannot be read, has its
        reason in ``failed``.
        """
        with self.lock:
            manager = self.manager
            rows: dict[str, dict[str, int]] = {}
            failed: dict[str, str] = {}
            for label in manager.headers():
                try:
                    batch = manager.batch(label)
                    states, _ = batch.shown()
                except (ValueError, OSError) as error:
                    failed[label] = why(error)
                    continue
                rows[label] = dict(Counter(s.text for s in states.values()))
            return BatchCounts(rows=rows, failed=failed)


def _listed(
    frame: pd.DataFrame, waits: pd.Series, fills: Collection[str]
) -> SourceRows:
    """
    The rows of ``frame``, a table that a source makes, as a dialog lists
    them: each named as :func:`~.page.names` names it, with the text of its
    notes, the columns but ``fills``, and marked if it ``waits``.
    """
    named = names(frame, ())
    shown = [c for c in frame.columns if c != NAME and c not in fills]

    def text(value: Any) -> str:
        return '' if pd.api.types.is_scalar(value) and pd.isna(value) else str(value)

    return SourceRows(
        columns=[Column(name=c, title=c.replace('_', ' ').capitalize()) for c in shown],
        rows=[
            SourceRow(
                key=key,
                name=named[key],
                cells={c: text(row[c]) for c in shown},
                waits=bool(waits[key]),
            )
            for key, row in frame[shown].to_dict('index').items()
        ],
    )


def _spec_choice(
    spec: WorkflowSpec[Any, Any], fills: Sequence[str], listing: Listing
) -> SpecChoice:
    """
    ``spec`` as a choice of the source that fills ``fills``: those first,
    then each other parameter whose cell the page edits.
    """
    form = Fields(spec.params, listing)
    edited = [n for n, f in form.fields.items() if forms.editor(f) is not None]
    names = [*fills, *(n for n in edited if n not in fills)]
    return SpecChoice(
        name=spec.name,
        title=spec.title,
        params=[Param(name=n, title=form.titles[n]) for n in names],
    )

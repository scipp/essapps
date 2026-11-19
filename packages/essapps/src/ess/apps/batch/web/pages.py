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
the counts (:meth:`Pages.counts`), which scan every batch.

The runs are the same on every page (:meth:`Pages.runs`), and a person
corrects them on any page, with or without a batch (:meth:`Pages.correct`).
The page of each batch scans its batch at its next poll after a correction
(:attr:`Pages.corrected`), and says once if that scan fails; the page the
correction was made on polls at once.

A person makes a batch from the list of batches, of the sources and specs
that :meth:`Pages.sources` offers (:meth:`Pages.create`); the manager decides
what can be made. With no batch at all, the page is served without one: it
shows the list of batches and the runs. So is the page of a batch that cannot
be shown (:meth:`Pages.page`), with the reason, and that of a batch removed
(:meth:`Pages.remove`): a :class:`~.page.Page` handed out before answers
:class:`~.page.Missing` from then on.

Follow is the one loop of the application
(:class:`~ess.apps.batch.auto.Follow`). :meth:`Pages.tick` steps it, with or
without a page open, keeps what the step raised, and has the page of the batch
that follows show what the step changed. :func:`~.api.create_app` owns the
timer that calls it.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from typing import Any

from pydantic import BaseModel

from ess.spec import FileRef, WorkflowSpec

from ...fields import UNSET, Fields
from ...files import Selector
from ...listing import Listing
from .. import (
    BatchManager,
    CorrectedFiles,
    NotKept,
    RowState,
    Scope,
    Source,
    Words,
    bound_spec,
    fits,
    refused_again,
)
from ..auto import Follow
from . import forms
from .models import (
    BatchCounts,
    Batches,
    BatchRow,
    Cell,
    CellEdit,
    Cells,
    Column,
    Made,
    NewBatch,
    Notice,
    Param,
    RunRow,
    Runs,
    SourceChoice,
    SpecChoice,
)
from .page import (
    Draw,
    Missing,
    Page,
    Problem,
    page_statuses,
    read,
    scoped,
    why,
)


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
        # The batches and the label that follows, as listed at the last version.
        self._batches: tuple[object, ...] = ()
        self._batches_version = 0
        self.corrected = 0
        self.failure: str | None = None
        self._pages: dict[str, Page] = {}

    def page(self, label: str) -> Page:
        """
        The page of ``label``, made at the first call. Raises
        :class:`Missing` if no batch has the label, and
        :class:`~.page.Problem` if its batch cannot be made, such as when
        its source raises.
        """
        with self.lock:
            if label not in self.manager.headers():
                self._pages.pop(label, None)
                raise Missing(f'There is no batch {label}.')
            if (page := self._pages.get(label)) is None:
                try:
                    page = Page(self, label)
                    # The rows can be read: an end of a scope kept that does
                    # not compare with the index of the source raises here.
                    len(page.batch.rows)
                except Exception as error:  # a source is code of the application
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
            headers = self.manager.headers()
            if self.follow.label in headers:
                return self.follow.label
            return next(iter(headers)) if len(headers) == 1 else None

    def tick(self) -> None:
        """
        A step of Follow, then :meth:`Page.stepped <.page.Page.stepped>`
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
        every page shows them, and that of the page without a batch. Raises
        :class:`~.page.Problem` if the application corrects no runs.
        """
        with self.lock:
            marks, files, listing = self._marked()

            def text(value: Any) -> str:
                return '' if value is UNSET else listing.text(value)

            rows = []
            for ref in files.list(Selector()):
                metadata = files.metadata(ref)
                original = files.source.metadata(ref)
                corrected = files.corrections(ref)
                cells = {
                    name: Cell(
                        value=forms.cell(field, metadata.get(name, UNSET)),
                        text=text(metadata.get(name, UNSET)),
                        edited=name in corrected,
                        scanned=text(original.get(name, UNSET)),
                    )
                    for name, field in marks.fields.items()
                }
                label = listing.file_label(ref)
                rows.append(RunRow(key=ref.file, file=label, cells=cells))
            return Runs(
                version=self.runs_version,
                columns=[
                    Column(name=n, title=marks.titles[n], editor=forms.editor(f))
                    for n, f in marks.fields.items()
                ],
                rows=rows,
            )

    def correct(self, edit: CellEdit) -> Notice:
        """
        Correct a field of the runs ``edit.keys``, as its field reads the
        value. The page of each batch scans its batch at its next poll, and
        Follow the batch that follows at its next step.
        """
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
        Make ``correction``, then have the pages scan their batches and show
        the runs again. Raises :class:`~.page.Problem` if the definitions do
        not keep it, or if the runs cannot be listed after it.
        """
        try:
            correction()
        except NotKept as error:
            raise Problem(f'The change was not kept: {error}') from None
        self.corrected += 1
        try:
            runs = self._relisted().listed(Selector())
        except Exception as error:  # the file source of the application
            raise Problem(
                f'Corrected, but the runs cannot be listed: {error}'
            ) from None
        self.runs_listed(runs, changed=True)

    def batches_version(self) -> int:
        """
        The version of the list of batches, so that the Batches view of each
        page asks for it again. It moves when the headers of the batches or
        the label that follows differ from those of the last version, whoever
        changed them.
        """
        with self.lock:
            listed = (tuple(self.manager.headers().items()), self.follow.label)
            if listed != self._batches:
                self._batches = listed
                self._batches_version += 1
            return self._batches_version

    def waiting(self, label: str, states: Mapping[str, RowState]) -> dict[str, str]:
        """
        Why Follow could not submit each row of ``states`` that its next step
        would submit again, by key; none unless the batch of ``label`` follows.
        """
        source = self.manager.batch(label).source
        if source is None or self.follow.label != label:
            return {}
        reasons = {
            key: f'Follow could not reduce it: {forms.message(reason)}'
            for key, reason in self.follow.refused.items()
        }
        return refused_again(states, source.fills, reasons)

    def batches(self) -> Batches:
        """The batches, as the Batches view of each page lists them."""
        with self.lock:
            rows = []
            for label, header in self.manager.headers().items():
                name, sources = header.source, self.manager.sources
                # listed even if its source is no longer registered
                source = None if name is None else sources.get(name)
                words = Words() if source is None else source.words
                index = '' if source is None else source.index_title
                rows.append(
                    BatchRow(
                        label=label,
                        title=header.title,
                        description=header.description,
                        words=words,
                        scope=_scope_text(header.scope, index, words),
                        follows=label == self.follow.label,
                    )
                )
            return Batches(
                version=self.batches_version(),
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
            specs = [bound_spec(client, name) for name in names]
            listing = self._relisted()

            def choice(name: str | None, source: Source | None) -> SourceChoice:
                fills = () if source is None else source.fills
                return SourceChoice(
                    name=name,
                    fills=list(fills),
                    words=Words() if source is None else source.words,
                    index_title='' if source is None else source.index_title,
                    specs=[
                        _spec_choice(spec, fills, listing)
                        for spec in specs
                        if fits(spec, source)
                    ],
                )

            given = [choice(n, s) for n, s in self.manager.sources.items()]
            return [c for c in given if c.specs] + [choice(None, None)]

    def create(self, new: NewBatch) -> Made:
        """
        Make the batch of ``new`` with the manager
        (:meth:`~ess.apps.batch.BatchManager.create`), with the settings that
        the application gives its spec, and its scope with the ends converted
        to the type of the index of its source
        (:func:`~ess.apps.batch.scope_of`). Raises :class:`~.page.Problem`
        with what the manager refuses.
        """
        with self.lock:
            refused = 'The batch was not made'
            try:
                source = (
                    None if new.source is None else self.manager.sources[new.source]
                )
                label = self.manager.create(
                    new.spec,
                    title=new.title,
                    source=new.source,
                    columns=new.columns,
                    description=new.description,
                    scope=scoped(source, new, refused),
                )
            except (KeyError, ValueError, NotKept) as error:
                raise Problem(f'{refused}: {why(error)}.') from None
            return Made(label=label)

    def remove(self, label: str) -> None:
        """
        Remove the batch of ``label`` with the manager
        (:meth:`~ess.apps.batch.BatchManager.remove`): its definitions go,
        its records stay. Its page goes, and a page handed out before
        answers :class:`~.page.Missing`. If it follows, Follow is turned off
        at once, rather than at its next step, so that a batch made later
        under the label does not follow. Raises :class:`~.page.Missing` if no batch
        has the label, and :class:`~.page.Problem` if the definitions refuse
        a part, saying whether the batch is still listed.
        """
        with self.lock:
            if label not in self.manager.headers():
                raise Missing(f'There is no batch {label}.')
            try:
                self.manager.remove(label)
            except NotKept as error:
                said = (
                    'not removed'
                    if label in self.manager.headers()
                    else 'removed, but part of its definitions stays'
                )
                raise Problem(f'The batch was {said}: {error}') from None
            finally:
                # The batch is gone once its header is; a refusal before
                # leaves its page and Follow as they were.
                if label not in self.manager.headers():
                    self._pages.pop(label, None)
                    if self.follow.label == label:
                        with suppress(NotKept):  # else its next step does
                            self.follow.off()

    def counts(self) -> BatchCounts:
        """
        The counts of the manager (:meth:`~ess.apps.batch.BatchManager.counts`)
        in the words of the page: the statuses are those of the page of each
        batch (:func:`~.page.page_statuses`). Every page and Follow wait
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


def _scope_text(scope: Scope, index: str, words: Words) -> str:
    """
    A scope as the Batches view shows it: ``Run 60339 \N{EN DASH} 60421``, with an open
    end left out, or ``2 rows named``; empty for every row.
    """
    if scope.keys is not None:
        return f'{words.count(len(scope.keys))} named'
    if scope.start is None and scope.stop is None:
        return ''
    start, stop = ('' if end is None else str(end) for end in (scope.start, scope.stop))
    return ' '.join(filter(None, [index, start, '\N{EN DASH}', stop]))

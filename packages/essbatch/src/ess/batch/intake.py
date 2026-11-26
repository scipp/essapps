# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rows of a row source into a batch: :class:`Intake` makes a batch from a
source, adds rows of its source by hand (:meth:`Intake.take`), and keeps
which batch follows and steps it. All three write ``considered``. The guide
is the section "Follow" of ``packages/essbatch/docs/batch.md``; the decision
is ``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.

``Intake`` makes and reads the batches through its
:class:`~.manager.BatchManager`, and writes one entry of the definitions
(:mod:`.definition`) for each batch made from a source::

    source      the source, the keys considered, and whether the batch follows

Whether a batch follows is kept with its label, so a removal through the
manager, which deletes every entry of the label, ends Follow, and a batch
made again with the label does not follow unless made so.
:meth:`Intake.create` makes the batch with the manager, which writes its
settings, rows and header; with ``follow``, it then ends Follow on the batch
that followed; then it writes ``source``. :meth:`Intake.follow` ends
Follow on the batch that followed, then writes that its batch follows. So no
two batches follow, also when a process stops in between. A batch follows
from the time it is made with ``follow`` or :meth:`Intake.follow` is called,
until :meth:`Intake.stop`, its removal, or another batch that starts to
follow. :meth:`Intake.step` is one step of Follow on the batch that
follows::

    made = SOURCE.load(definitions, label)                   # source.json
    done = step(batch(label), sources[made.source], made.considered)  # put, reduce
    source.json = replace(made, considered=done.considered)  # after rows.json;
    # if the step raised: made.considered | the keys of the batch table

Whoever serves owns the timer that calls it, and the lock. ``Intake`` has
neither.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

import pandas as pd

from ess.apps.errors import said
from ess.spec import WorkflowSpec

from . import auto
from .auto import SourceError, Step
from .definition import SOURCE, FromSource, NotKept
from .manager import BatchManager
from .reduction import Batch
from .source import Range, RowSource, filled, rows_of


def fits(spec: WorkflowSpec[Any, Any], source: RowSource | None) -> bool:
    """
    Whether a batch of ``spec`` can be made with ``source``: whether each
    parameter that the source fills is a parameter of the spec. With no
    source, every spec fits. :meth:`Intake.create` refuses a spec that does
    not fit; the page offers only those that fit.
    """
    return not _lacked(spec, source)


def _lacked(spec: WorkflowSpec[Any, Any], source: RowSource | None) -> list[str]:
    """The parameters that ``source`` fills and ``spec`` lacks."""
    fills = () if source is None else source.fills
    return [f for f in fills if f not in spec.params.model_fields]


def not_registered(name: str, registered: Iterable[str]) -> str:
    """Why the source ``name`` is refused: it is none of ``registered``."""
    names = ', '.join(registered) or 'none'
    return f'the source {name} is not registered; registered: {names}'


def step_failure(error: Exception) -> str:
    """
    What a person reads of a step of Follow (:meth:`Intake.step`) that raised
    ``error``: ``Follow could not step: `` and what
    :func:`~ess.apps.errors.said` says of it.
    """
    return f'Follow could not step: {said(error)}'


def _check_chosen(chosen: pd.DataFrame, arrived: pd.DataFrame) -> None:
    """
    Raise ``ValueError`` unless ``chosen`` is a frame whose keys are keys of
    ``arrived``. Its columns are the person's to choose.
    """
    if not isinstance(chosen, pd.DataFrame):
        raise ValueError(f'choose gives a {type(chosen).__name__}, not a frame')
    if rows := [k for k in chosen.index if k not in arrived.index]:
        raise ValueError(f'choose gives rows {rows} that the source does not')


class Intake:
    """
    Brings the rows of row sources into the batches of ``manager``: makes a
    batch from a source, offers and takes the rows that its batch table lacks, and
    keeps which batch follows. See ``packages/essbatch/docs/batch.md``.

    Refusals are as those of :class:`~.manager.BatchManager`: ``ValueError``
    for what is asked, such as a source that is not registered,
    ``KeyError`` for a label that no batch has, and
    :class:`~.definition.NotKept` for a write that the definitions refuse.
    Anything else is an error of the code of the application, such as a
    source that raises.

    :attr:`version` moves whenever :attr:`following` may give something else
    by a call of this object. A change through the manager, such as a
    removal, moves :attr:`.manager.BatchManager.version` instead, and
    :attr:`following` is read again from the definitions after it.

    Parameters
    ----------
    manager:
        The batches.
    sources:
        The row sources that a batch can be made with, by name.
    """

    def __init__(
        self, manager: BatchManager, *, sources: Mapping[str, RowSource]
    ) -> None:
        self.manager = manager
        self.sources = sources
        self._version = 0
        # The batch that follows, as read when the manager had this version.
        self._read: tuple[int, str | None] | None = None

    @property
    def version(self) -> int:
        """Counts the changes of which batch follows, made through this object."""
        return self._version

    @property
    def following(self) -> str | None:
        """
        The label of the batch whose ``source.json`` says that it follows,
        ``None`` if none does. It is read from the definitions at the first
        call and whenever :attr:`.manager.BatchManager.version` moved since.
        If several say so, which no call of ``Intake`` makes, it is the first
        in the order of :meth:`~.manager.BatchManager.headers`.
        """
        version = self.manager.version
        if self._read is None or self._read[0] != version:
            self._read = (version, self._kept_following())
        return self._read[1]

    def _kept_following(self) -> str | None:
        """
        The first label whose ``source.json`` says that its batch follows. A
        ``source.json`` that cannot be read is passed over: :meth:`source`
        says why at the page of its batch, and the other pages still work.
        """
        for label in self.manager.headers():
            try:
                made = self._from(label)
            except ValueError:
                continue
            if made is not None and made.follows:
                return label
        return None

    def _set_following(self, label: str | None) -> None:
        """Hold ``label`` as the batch that follows, as now written."""
        self._read = (self.manager.version, label)
        self._version += 1

    def _write_follows(self, label: str, follows: bool) -> None:
        """Write whether the batch of ``label`` follows."""
        made = replace(self._made(label), follows=follows)
        SOURCE.save(self.manager.definitions, label, made)

    def stop(self) -> None:
        """
        End Follow: no batch follows from then on, until :meth:`follow` or
        :meth:`create` with ``follow``. Nothing is done if no batch follows.
        """
        if (label := self.following) is None:
            return
        self._write_follows(label, False)
        self._set_following(None)

    def follow(self, label: str) -> None:
        """
        Let the batch of ``label`` follow from then on, in place of the batch
        that follows now. Nothing is done if it follows already.

        Its first step adds every row of its source that is filled and that
        the batch has not considered, also a row that arrived while it did
        not follow and a row that another batch took meanwhile. A row left
        out when the batch was made, or removed by a person, is considered
        and stays out.

        ``source.json`` of the batch that follows now is written to say that
        it follows no more, then that of ``label`` to say that it follows. A
        process that stops in between leaves no batch that follows. If the
        definitions refuse the second write, the batch that followed is
        written to follow again.

        Raises ``KeyError`` if no batch has the label, and ``ValueError`` if
        it was not made from a source or its source is not registered.
        """
        self._source(self._made(label).source)
        if (before := self.following) == label:
            return
        self._follow_instead(before, lambda: self._write_follows(label, True))
        self._set_following(label)

    def _follow_instead(self, before: str | None, write: Callable[[], None]) -> None:
        """
        End Follow on the batch of ``before``, if given, then call ``write``,
        which writes the ``source.json`` of the batch that follows in its
        place. If the definitions refuse either, ``before`` follows again.
        """
        ended = None  # the batch whose Follow was ended, to follow again
        try:
            if before is not None:
                self._write_follows(before, False)
                ended = before
            write()
        except NotKept:
            if ended is not None:
                with suppress(NotKept):  # the first error is what to say
                    self._write_follows(ended, True)
            raise

    def _source(self, name: str) -> RowSource:
        """
        The source registered as ``name``; ``ValueError`` if none is, naming
        those that are.
        """
        if name not in self.sources:
            raise ValueError(not_registered(name, self.sources))
        return self.sources[name]

    def rows_of(self, source: str) -> pd.DataFrame:
        """
        The rows that the source registered as ``source`` makes from the
        files now (:func:`~.source.rows_of`). Raises ``ValueError`` if no
        source is registered so.
        """
        return rows_of(self.manager.client, self._source(source))

    def _given(self, source: RowSource, fills: Collection[str]) -> pd.DataFrame:
        """
        What ``source`` makes now, with a column ``waits``: whether a cell of
        ``fills`` is empty (:func:`~.source.filled`). A column of the source
        named ``waits`` is replaced by it.
        """
        given = rows_of(self.manager.client, source)
        return given.assign(waits=~filled(given, fills))

    def marked(
        self,
        source: str,
        choose: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
    ) -> pd.DataFrame:
        """
        The rows that the source registered as ``source`` makes now, with a
        column ``waits``, whether the row waits, and a column ``taken``,
        whether :meth:`create` takes it with ``choose``, such as
        ``Range(first='60339')`` (:class:`~.source.Range`); every row that
        does not wait if not given. Raises ``ValueError`` if no source is
        registered so, and what ``choose`` raises.
        """
        found = self._source(source)
        given = rows_of(self.manager.client, found)
        waits = ~filled(given, found.fills)
        arrived = given[~waits]
        taken = arrived.index if choose is None else choose(arrived).index
        return given.assign(waits=waits, taken=given.index.isin(taken))

    def create(
        self,
        spec: str,
        *,
        title: str,
        source: str,
        choose: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
        follow: bool = False,
        parameter_columns: Collection[str] = (),
        description: str = '',
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> Batch:
        """
        Make a batch from ``source`` with the manager
        (:meth:`~.manager.BatchManager.create`) and keep what it considered;
        its ``Batch``. Nothing is written if a check fails.

        The source is listed once, and the rows that are filled
        (:func:`~.source.filled`) are taken and considered::

            given = rows_of(client, source)
            arrived = given[filled]          # a value in each of source.fills
            rows = arrived if choose is None else choose(arrived)
            considered = arrived.index       # the rows taken and those left out

        A row that waits is neither taken nor considered: Follow adds it once
        it is filled, if the batch follows. The batch keeps ``source.fills``
        (:attr:`.reduction.Batch.fills`): its batch table keeps a column for each,
        an empty cell there means that the row waits for its file, and the
        settings given are left out there.

        The label, the spec, the source and whether the spec fits it are
        checked before the source is listed. The manager writes the settings,
        the rows and the header; then, with ``follow``, the ``source.json`` of
        the batch that followed says that it follows no more; then the
        ``source.json`` of the batch is written.
        If the definitions refuse one of these writes, the batch that
        followed is written to follow again, if its Follow was ended, and the
        batch is removed again (:meth:`~.manager.BatchManager.remove`). A process
        that stops between the header and the batch's ``source.json`` leaves
        a batch made without a source, which neither offers rows nor
        follows, and no batch that follows.

        Parameters
        ----------
        spec, title, parameter_columns, description, settings, label:
            As :meth:`~.manager.BatchManager.create` takes them.
        source:
            The name of a registered source to take the rows from. The batch
            keeps it, to offer the rows that its batch table lacks and to follow.
        choose:
            Makes the batch table from the rows of the source that are
            filled: some of its rows, with any columns, such as
            ``lambda given: given[given.run >= 60339].assign(thickness=pd.NA)``,
            or a range of them as a person picks it, such as
            ``Range(first='60339')`` (:class:`~.source.Range`). All of them,
            as they are, if not given.
        follow:
            Whether the batch follows its source from the time it is made, in
            place of the batch that followed before. A batch made without
            ``follow`` follows only after :meth:`follow`. A
            :class:`~.source.Range` with a ``last`` is refused with
            ``follow``; a function may choose any rows, and the filled rows
            that it leaves out are considered, so Follow adds only the rows
            that arrive later.

        Raises
        ------
        ValueError
            If ``follow`` is given with a ``last`` of a
            :class:`~.source.Range`; if no source ``source`` is registered;
            if the source fills a parameter that the spec lacks
            (:func:`fits`); if ``choose`` gives a row that the source did
            not; and as :meth:`~.manager.BatchManager.create` refuses.
        """
        if follow and isinstance(choose, Range) and choose.last is not None:
            raise ValueError(
                'a batch that follows takes every row that arrives later, '
                'so it has no last row'
            )
        # Checked before the source is listed, which may be slow or raise.
        self.manager.check(spec, title=title, label=label)
        found = self._source(source)
        if not fits(bound := self.manager.spec_named(spec), found):
            lacked = _lacked(bound, found)
            named = f'{"parameter" if len(lacked) == 1 else "parameters"} '
            raise ValueError(
                f'the spec {spec} has no {named}{", ".join(lacked)}, which the '
                f'source {source} fills'
            )
        given = rows_of(self.manager.client, found)
        arrived = given[filled(given, found.fills)]
        rows = arrived if choose is None else choose(arrived)
        _check_chosen(rows, arrived)
        before = self.following
        batch = self.manager.create(
            spec,
            title=title,
            rows=rows,
            fills=found.fills,
            parameter_columns=parameter_columns,
            description=description,
            settings=settings,
            label=label,
        )
        made = FromSource(source, frozenset(arrived.index), follows=follow)
        try:
            self._follow_instead(
                before if follow else None,
                lambda: SOURCE.save(self.manager.definitions, batch.label, made),
            )
        except NotKept:
            with suppress(NotKept):  # the first error is what to say
                self.manager.remove(batch.label)
            raise
        if follow:
            self._set_following(batch.label)
        return batch

    def _from(self, label: str) -> FromSource | None:
        """
        What the batch of ``label`` keeps of its source, ``None`` if it was
        not made from one. Raises ``KeyError`` if no batch has the label.
        """
        if label not in self.manager.headers():
            raise KeyError(label)
        return SOURCE.load(self.manager.definitions, label)

    def _made(self, label: str) -> FromSource:
        """
        What the batch of ``label`` keeps of its source. Raises ``KeyError``
        if no batch has the label, and ``ValueError`` if it was not made from
        a source.
        """
        if (made := self._from(label)) is None:
            raise ValueError(f'{label} was not made from a row source')
        return made

    def source(self, label: str) -> str | None:
        """
        The name of the source that the batch of ``label`` was made from,
        ``None`` if it was made without one and cannot follow. Raises
        ``KeyError`` if no batch has the label.
        """
        made = self._from(label)
        return None if made is None else made.source

    def step(self) -> Step | None:
        """
        One step of Follow on the batch that follows, ``None`` if none does:
        add the rows of its source that are new and filled, reduce the rows
        that are ready and have no result (:func:`~.auto.step`), then keep the
        keys considered. The step is returned, to read what it refused and
        which rows wait.

        If the step raises, such as when a submission is cut off after the
        rows were added, the keys of the batch table are kept as considered:
        otherwise a row that it added and a person removes would come back.

        Raises ``ValueError`` if the source of the batch is not registered,
        if the source raises ``ValueError``, or if the batch refuses a row
        that the source makes, naming the source, the row and the column;
        and what the step raises otherwise, also if the definitions then
        refuse the keys considered.
        """
        if (label := self.following) is None:
            return None
        made = self._made(label)
        batch = self.manager.batch(label)
        source = self._source(made.source)
        try:
            done = auto.step(batch, source, made.considered)
        except SourceError as error:  # a defect of the code of the source
            raise ValueError(f'the source {made.source}: {error}') from error
        except BaseException:
            with suppress(NotKept):  # the exception of the step is what to say
                kept = made.considered | set(batch.frame().index)
                self._consider(label, made, kept)
            raise
        self._consider(label, made, done.considered)
        return done

    def offered(self, label: str) -> pd.DataFrame:
        """
        The rows that the source of the batch of ``label`` makes now and its
        batch table lacks, with the columns of the source and a column ``waits``:
        whether a cell of the batch's :attr:`~.reduction.Batch.fills` is
        empty, so that Follow adds the row once it is filled, unless it is
        considered.
        A row that does not wait was left out when the batch was made, or
        removed from it, and is considered, so Follow does not add it; or it
        was filled after the batch was made and is not considered, so Follow
        adds it at its next step if the batch follows. A column of the source
        named ``waits`` is replaced by it.

        Raises ``KeyError`` if no batch has the label, and ``ValueError`` if
        it was not made from a source, if its source is not registered, or
        raises ``ValueError``, or no longer gives a column of the batch's
        fills (:func:`~.source.filled`).
        """
        made = self._made(label)
        source = self._source(made.source)
        batch = self.manager.batch(label)
        try:
            given = self._given(source, batch.fills)
        except ValueError as error:  # as step says it
            raise ValueError(f'the source {made.source}: {said(error)}') from error
        return given[~given.index.isin(batch.frame().index)]

    def take(self, label: str, keys: Iterable[str]) -> None:
        """
        Add the rows of ``keys`` to the batch of ``label``, as its source
        makes them now, with the columns of its batch table; then consider them, so
        that Follow does not add them again once removed.

        Raises ``KeyError`` if no batch has the label; ``ValueError`` if it
        was not made from a source, if its source is not registered or makes
        no row of a key, or if its batch table has a row of a key.
        """
        made = self._made(label)
        given = self.rows_of(made.source)
        keys = list(keys)
        if unknown := [k for k in keys if k not in given.index]:
            raise ValueError(f'the source {made.source} makes no rows {unknown}')
        batch = self.manager.batch(label)
        table = batch.frame()
        if held := [k for k in keys if k in table.index]:
            raise ValueError(f'rows {held} are in the batch already')
        batch.put(pd.concat([table, given.loc[keys].reindex(columns=table.columns)]))
        self._consider(label, made, made.considered | set(keys))

    def _consider(
        self, label: str, made: FromSource, considered: frozenset[str]
    ) -> None:
        """Keep ``considered`` as the keys of the source of ``label``, if changed."""
        if considered != made.considered:
            kept = replace(made, considered=considered)
            SOURCE.save(self.manager.definitions, label, kept)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rows of a row source into a batch: :class:`Intake` makes a batch from a
source, adds rows of its source by hand (:meth:`Intake.take`), and keeps
which batch follows and steps it. All three write ``considered``. The guide
is the section "Follow" of ``packages/essapps/docs/batch.md``; the decision
is ``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.

``Intake`` makes and reads the batches through its
:class:`~.manager.BatchManager`, and writes two tables of the definitions
(:mod:`.definition`)::

    source      the source and the keys considered      of a batch made from a source
    following   the label of the batch that follows     of the workspace

:meth:`Intake.create` makes the batch with the manager, which writes its
settings, rows and header, then writes ``source``, then ``following`` if the
batch follows. A batch follows from the time it is made, if made so, until
:meth:`Intake.stop` or its removal; nothing turns Follow on for a batch made
before. The manager removes a batch without writing ``following``, so a
label of ``following`` without a header counts as none, at each read
(:attr:`Intake.following`). :meth:`Intake.step` is one step of Follow on the
batch that follows::

    made = source_of(label)                                  # source.json
    done = step(batch(label), sources[made.source], made.considered)  # put, reduce
    source.json = replace(made, considered=done.considered)  # after the table;
    # if the step raised: made.considered | the keys of the table

Whoever serves owns the timer that calls it, and the lock. ``Intake`` has
neither.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

import pandas as pd

from ess.spec import WorkflowSpec

from . import auto
from .auto import SourceError, Step
from .definition import (
    FOLLOWING,
    FromSource,
    NotKept,
    following_data,
    following_of,
    source_data,
    source_key,
    source_of,
)
from .manager import BatchManager
from .reduction import Batch
from .source import Range, RowSource, filled, rows_of


def fits(spec: WorkflowSpec[Any, Any], source: RowSource | None) -> bool:
    """
    Whether a batch of ``spec`` can be made with ``source``: whether each
    parameter that the source fills is a parameter of the spec. With no
    source, every spec fits.
    """
    fills = () if source is None else source.fills
    return set(fills) <= spec.params.model_fields.keys()


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
    batch from a source, offers and takes the rows that its table lacks, and
    keeps which batch follows. See ``packages/essapps/docs/batch.md``.

    Refusals are as those of :class:`~.manager.BatchManager`: ``ValueError``
    for what is asked, such as a source that is not registered,
    ``KeyError`` for a label that no batch has, and
    :class:`~.definition.NotKept` for a write that the definitions refuse.
    Anything else is an error of the code of the application, such as a
    source that raises.

    :attr:`version` moves whenever :attr:`following` may give something else
    by a call of this object; a removal through the manager moves
    :attr:`.manager.BatchManager.version`.

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
        self._following = manager.definitions.load(FOLLOWING, following_of)
        self._version = 0

    @property
    def version(self) -> int:
        """Counts the changes of which batch follows, made through this object."""
        return self._version

    @property
    def following(self) -> str | None:
        """
        The label of the batch that follows, ``None`` if none does. It is
        kept in the definitions and read when the intake is made; a label
        without a header counts as none.
        """
        if self._following not in self.manager.headers():
            return None
        return self._following

    def stop(self) -> None:
        """
        End Follow: no batch follows from then on, and the batch that followed
        cannot follow again. Nothing is done if no batch follows.
        """
        if self.following is None:
            return
        self.manager.definitions.save(FOLLOWING, following_data(None))
        self._following = None
        self._version += 1

    def _source(self, name: str) -> RowSource:
        """The source registered as ``name``; ``ValueError`` if none is."""
        if name not in self.sources:
            raise ValueError(f'the source {name} is not registered')
        return self.sources[name]

    def rows_of(self, source: str) -> pd.DataFrame:
        """
        The table that the source registered as ``source`` makes from the
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
        The table that the source registered as ``source`` makes now, with a
        column ``waits``, whether the row waits, and a column ``taken``,
        whether :meth:`create` takes it with ``choose``, such as
        ``Range(first='60339')`` (:class:`~.source.Range`); every row that
        does not wait if not given. Raises ``ValueError`` if no source is
        registered so, and what ``choose`` raises.
        """
        found = self._source(source)
        given = self._given(found, found.fills)
        arrived = given[~given['waits']].drop(columns='waits')
        taken = arrived.index if choose is None else choose(arrived).index
        return given.assign(taken=given.index.isin(taken))

    def create(
        self,
        spec: str,
        *,
        title: str,
        source: str,
        choose: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
        follow: bool = False,
        columns: Collection[str] = (),
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
            table = arrived if choose is None else choose(arrived)
            considered = arrived.index       # the rows taken and those left out

        A row that waits is neither taken nor considered: Follow adds it once
        it is filled, if the batch follows. The batch keeps ``source.fills``
        (:attr:`.reduction.Batch.fills`): its table keeps a column for each,
        an empty cell there means that the row waits for its file, and the
        settings given are left out there.

        The manager writes the settings, the rows and the header; then
        ``source.json`` is written, then, with ``follow``, ``following``. If
        the definitions refuse one of these two, the batch is removed again
        (:meth:`~.manager.BatchManager.remove`), and which batch follows
        stays. A process that stops between the header and ``source.json``
        leaves a batch made without a source, which neither offers rows nor
        follows.

        Parameters
        ----------
        spec, title, columns, description, settings, label:
            As :meth:`~.manager.BatchManager.create` takes them.
        source:
            The name of a registered source to take the rows from. The batch
            keeps it, to offer the rows that its table lacks and to follow.
        choose:
            Makes the table of the batch from the rows of the source that are
            filled: some of its rows, with any columns, such as
            ``lambda given: given[given.run >= 60339].assign(thickness=pd.NA)``,
            or a range of them as a person picks it, such as
            ``Range(first='60339')`` (:class:`~.source.Range`). All of them,
            as they are, if not given.
        follow:
            Whether the batch follows its source from the time it is made, in
            place of the batch that followed before. A batch made without
            ``follow`` never follows. A :class:`~.source.Range` with a
            ``last`` is refused with ``follow``; a function may choose any
            rows, and the filled rows that it leaves out are considered, so
            Follow adds only the rows that arrive later.

        Raises
        ------
        ValueError
            If ``follow`` is given with a ``last`` of a
            :class:`~.source.Range`; if no source ``source`` is registered;
            if ``choose`` gives a row that the source did not; and as
            :meth:`~.manager.BatchManager.create` refuses.
        """
        if follow and isinstance(choose, Range) and choose.last is not None:
            raise ValueError(
                'a batch that follows takes every row that arrives later, '
                'so it has no last row'
            )
        found = self._source(source)
        given = self._given(found, found.fills)
        arrived = given[~given['waits']].drop(columns='waits')
        rows = arrived if choose is None else choose(arrived)
        _check_chosen(rows, arrived)
        batch = self.manager.create(
            spec,
            title=title,
            rows=rows,
            fills=found.fills,
            columns=columns,
            description=description,
            settings=settings,
            label=label,
        )
        definitions = self.manager.definitions
        try:
            made = FromSource(source, frozenset(arrived.index))
            definitions.save(source_key(batch.label), source_data(made))
            if follow:
                definitions.save(FOLLOWING, following_data(batch.label))
        except NotKept:
            with suppress(NotKept):  # the first error is what to say
                self.manager.remove(batch.label)
            raise
        if follow:
            self._following = batch.label
            self._version += 1
        return batch

    def _from(self, label: str) -> FromSource | None:
        """
        What the batch of ``label`` keeps of its source, ``None`` if it was
        not made from one. Raises ``KeyError`` if no batch has the label.
        """
        if label not in self.manager.headers():
            raise KeyError(label)
        return self.manager.definitions.load(source_key(label), source_of)

    def _made(self, label: str) -> FromSource:
        """
        What the batch of ``label`` keeps of its source. Raises ``KeyError``
        if no batch has the label, and ``ValueError`` if it was not made from
        a source.
        """
        if (made := self._from(label)) is None:
            raise ValueError(f'{label} was not made from a source of rows')
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
        rows were added, the keys of the table are kept as considered:
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
        table lacks, with the columns of the source and a column ``waits``:
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
            raise ValueError(f'the source {made.source}: {error}') from error
        return given[~given.index.isin(batch.frame().index)]

    def take(self, label: str, keys: Iterable[str]) -> None:
        """
        Add the rows of ``keys`` to the batch of ``label``, as its source
        makes them now, with the columns of its table; then consider them, so
        that Follow does not add them again once removed.

        Raises ``KeyError`` if no batch has the label; ``ValueError`` if it
        was not made from a source, if its source is not registered or makes
        no row of a key, or if its table has a row of a key.
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
            self.manager.definitions.save(source_key(label), source_data(kept))

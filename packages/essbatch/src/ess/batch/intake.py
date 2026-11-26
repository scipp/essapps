# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Rows of a row source into a batch, and which batch follows.

:class:`Intake` keeps one entry of the definitions (:mod:`.definition`) for
each batch made from a source::

    source      the source, the keys considered, and whether the batch follows

:meth:`Intake.step` is one step of Follow on the batch that follows::

    made = SOURCE.load(definitions, label)                   # source.json
    done = step(batch(label), sources[made.source], made.considered)
    source.json = replace(made, considered=done.considered)  # after rows.json
    # if the step raised: made.considered | the keys of the batch table

A process that stops between ``rows.json`` and ``source.json`` leaves rows
whose keys are not considered yet. The next step considers them: it
considers every key of the source that the batch table has. The order of the
writes of :meth:`Intake.create` and :meth:`Intake.follow` is in the guide,
"What is kept on disk". ``Intake`` has no timer and no lock: whoever serves
owns both.

Guide: ``packages/essbatch/docs/batch.md``, "From a row source" and "Follow".
Decision: ``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import suppress
from dataclasses import replace
from typing import Any

import pandas as pd

from ess.apps.errors import error_text
from ess.spec import WorkflowSpec

from . import auto
from .auto import SourceError, Step
from .definition import SOURCE, DefinitionWriteError, FromSource
from .manager import BatchManager
from .reduction import Batch
from .source import Range, RowSource, filled, rows_of


def fits(spec: WorkflowSpec[Any, Any], source: RowSource | None) -> bool:
    """
    Whether a batch of ``spec`` can be made with ``source``: whether each
    parameter that the source fills is a parameter of the spec. With no
    source, every spec fits.
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
    :func:`~ess.apps.errors.error_text` says of it.
    """
    return f'Follow could not step: {error_text(error)}'


def _check_chosen(chosen: pd.DataFrame, arrived: pd.DataFrame) -> None:
    """Raise ``ValueError`` unless ``chosen`` is a frame of keys of ``arrived``."""
    if not isinstance(chosen, pd.DataFrame):
        raise ValueError(f'choose gives a {type(chosen).__name__}, not a frame')
    if rows := [k for k in chosen.index if k not in arrived.index]:
        raise ValueError(f'choose gives rows {rows} that the source does not')


class Intake:
    """
    Brings the rows of row sources into the batches of ``manager``, and keeps
    which batch follows. It raises as ``batch.md``, "Errors", says.

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
        # The batch that follows, as read when the manager had this version.
        self._read: tuple[int, str | None] | None = None

    @property
    def following(self) -> str | None:
        """
        The label of the batch that follows, ``None`` if none does. It is read
        from the definitions again only when
        :attr:`.manager.BatchManager.version` moved.
        """
        version = self.manager.version
        if self._read is None or self._read[0] != version:
            self._read = (version, self._kept_following())
        return self._read[1]

    def _kept_following(self) -> str | None:
        """The first label whose readable ``source.json`` says that it follows."""
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

    def _write_follows(self, label: str, follows: bool) -> None:
        """Write whether the batch of ``label`` follows."""
        made = replace(self._made(label), follows=follows)
        SOURCE.save(self.manager.definitions, label, made)

    def stop(self) -> None:
        """End Follow. Nothing is done if no batch follows."""
        if (label := self.following) is None:
            return
        self._write_follows(label, False)
        self._set_following(None)

    def follow(self, label: str) -> None:
        """
        Let the batch of ``label`` follow, in place of the batch that follows
        now. Nothing is done if it follows already.

        Raises ``KeyError`` if no batch has the label, and ``ValueError`` if
        it was not made from a source or its source is not registered.
        Raises :class:`~.definition.DefinitionWriteError` if the definitions refuse a
        write; the batch that followed then follows again.
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
        place. In this order no two batches follow, also if the process stops
        in between. If the definitions refuse either write, ``before`` is
        written to follow again.
        """
        ended = None  # the batch whose Follow was ended, to follow again
        try:
            if before is not None:
                self._write_follows(before, False)
                ended = before
            write()
        except DefinitionWriteError:
            if ended is not None:
                with suppress(DefinitionWriteError):  # the first error is what to say
                    self._write_follows(ended, True)
            raise

    def _source(self, name: str) -> RowSource:
        """The source registered as ``name``; ``ValueError`` if none is."""
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
        Make a batch from the rows of ``source`` with the manager
        (:meth:`~.manager.BatchManager.create`), and keep what it considered;
        its ``Batch``. The source is listed once::

            given = rows_of(client, source)
            arrived = given[filled]          # a value in each of source.fills
            rows = arrived if choose is None else choose(arrived)
            considered = arrived.index       # the rows taken and those left out

        The label, the spec, the source and whether the spec fits it are
        checked before the source is listed. Nothing is written if a check
        fails. The order of the writes: ``batch.md``, "What is kept on disk".

        Parameters
        ----------
        spec, title, parameter_columns, description, settings, label:
            As :meth:`~.manager.BatchManager.create` takes them.
        source:
            The name of a registered source.
        choose:
            Makes the batch table from ``arrived``: a function that gives some
            of its rows, with any columns, such as
            ``lambda arrived: arrived[arrived.run >= 60339]``, or a
            :class:`~.source.Range`. All of ``arrived`` if not given.
        follow:
            Whether the batch follows from its making, in place of the batch
            that follows now.

        Raises
        ------
        ValueError
            If ``follow`` is given with a ``last`` of a
            :class:`~.source.Range`; if no source ``source`` is registered;
            if the source fills a parameter that the spec lacks
            (:func:`fits`); if ``choose`` gives a row that the source did
            not; and as :meth:`~.manager.BatchManager.create` refuses.
        DefinitionWriteError
            If the definitions refuse a write. The batch is removed again,
            and the batch that followed follows again.
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
        except DefinitionWriteError:
            with suppress(DefinitionWriteError):  # the first error is what to say
                self.manager.remove(batch.label)
            raise
        if follow:
            self._set_following(batch.label)
        return batch

    def _from(self, label: str) -> FromSource | None:
        """The ``source.json`` of ``label``, or ``None``; ``KeyError`` if no batch."""
        if label not in self.manager.headers():
            raise KeyError(label)
        return SOURCE.load(self.manager.definitions, label)

    def _made(self, label: str) -> FromSource:
        """:meth:`_from`, with ``ValueError`` for a batch made without a source."""
        if (made := self._from(label)) is None:
            raise ValueError(f'{label} was not made from a row source')
        return made

    def source(self, label: str) -> str | None:
        """
        The name of the source that the batch of ``label`` was made from,
        ``None`` if it was made without one. Raises ``KeyError`` if no batch
        has the label.
        """
        made = self._from(label)
        return None if made is None else made.source

    def step(self) -> Step | None:
        """
        One step of Follow (:func:`~.auto.step`) on the batch that follows,
        then keep the keys considered; ``None`` if no batch follows.

        Raises ``ValueError`` if the source of the batch is not registered,
        if the source raises ``ValueError``, or if the batch refuses a row
        that the source makes; the message names the source, the row and the
        column. Raises what the step raises otherwise.
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
            with suppress(
                DefinitionWriteError
            ):  # the exception of the step is what to say
                kept = made.considered | set(batch.frame().index)
                self._consider(label, made, kept)
            raise
        self._consider(label, made, done.considered)
        return done

    def offered(self, label: str) -> pd.DataFrame:
        """
        The rows that the source of the batch of ``label`` makes now and its
        batch table lacks, with the columns of the source and a boolean column
        ``waits``, true if a cell of :attr:`~.reduction.Batch.fills` is
        empty. A column of the source named ``waits`` is replaced.

        Raises ``KeyError`` if no batch has the label, and ``ValueError`` if
        it was not made from a source, if its source is not registered, or
        raises ``ValueError``, or no longer gives a column of the batch's
        fills (:func:`~.source.filled`).
        """
        made = self._made(label)
        source = self._source(made.source)
        batch = self.manager.batch(label)
        try:
            given = rows_of(self.manager.client, source)
            waits = ~filled(given, batch.fills)
        except ValueError as error:  # as step says it
            raise ValueError(
                f'the source {made.source}: {error_text(error)}'
            ) from error
        return given.assign(waits=waits)[~given.index.isin(batch.frame().index)]

    def take(self, label: str, keys: Iterable[str]) -> None:
        """
        Add the rows of ``keys`` to the batch of ``label``, as its source
        makes them now, with the columns of its batch table; then consider
        them. A considered key may be taken.

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

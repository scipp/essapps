# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them, keeps which batch follows, and steps it. The guide is the
section "The batches of a workspace" of ``packages/essapps/docs/batch.md``;
the decisions are ``docs/developer/adr/0011``, ``0012`` and ``0013``.

A batch is a label with a header. A :class:`~.reduction.Batch` is the object
that holds its template and its table; the manager makes the one ``Batch`` of
each batch. A batch is kept as these tables of the definitions
(:mod:`.definition`), each with one writer::

    header      title, description                      BatchManager
    settings    the template                            the Batch
    rows        the table                               the Batch
    source      the source and the keys considered      BatchManager; only for
                                                        a batch made from a source

so a manager made again, after a restart, makes the same ``Batch`` of a label
from them, the specs that the workspace binds, and the specs that run staged.
Making a batch makes its ``Batch``, which writes the settings and the rows;
then the manager writes the source, then the header: a label without a header
is not a batch. Removing a batch deletes the header first. So a ``create`` or
a ``remove`` cut off at any write leaves a label with tables and no header,
which :meth:`BatchManager.remove` removes. After a restart, the ``Batch`` of a
label is made at the first call of :meth:`BatchManager.batch`.

The manager writes one more table, ``following``: the label of the batch that
follows, or none. :meth:`BatchManager.step` is one step of Follow on it::

    made = source_of(label)                                  # source.json
    done = step(batch(label), sources[made.source], made.considered)  # put, reduce
    source.json = FromSource(made.source, done.considered)   # after the table;
    # if the step raised: made.considered | the keys of the table

Whoever serves owns the timer that calls it, and the lock. The manager has
neither.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import suppress
from typing import Any

import pandas as pd

from ess.spec import WorkflowSpec

from ..client import Client
from . import auto
from .auto import SourceError, Step, added
from .definition import (
    FOLLOWING,
    DefinitionStore,
    FromSource,
    Header,
    Key,
    NotKept,
    following_data,
    following_of,
    header_data,
    header_key,
    header_of,
    rows_key,
    settings_key,
    source_data,
    source_key,
    source_of,
    template_of,
)
from .reduction import Batch
from .source import Source, filled, rows_of
from .table import frame_of, typed


def fits(spec: WorkflowSpec[Any, Any], source: Source | None) -> bool:
    """
    Whether a batch of ``spec`` can be made with ``source``: whether each
    parameter that the source fills is a parameter of the spec. With no
    source, every spec fits.
    """
    fills = () if source is None else source.fills
    return set(fills) <= spec.params.model_fields.keys()


def _check_label(label: str) -> None:
    """
    Raise ``ValueError`` unless ``label`` is letters, digits, ``_``, ``.``
    and ``-``, and starts with none of the last two, so that it is one
    segment of a URL path.
    """
    if re.fullmatch(r'\w[\w.-]*', label) is None:
        raise ValueError(
            f'{label!r}: a label is letters, digits, "_", "." and "-", '
            'and starts with a letter, a digit or "_"'
        )


def _parts(label: str) -> tuple[Key, ...]:
    """The tables of ``label`` but its header, in the order they are deleted."""
    return source_key(label), rows_key(label), settings_key(label)


def _check_chosen(chosen: pd.DataFrame, arrived: pd.DataFrame) -> None:
    """
    Raise ``ValueError`` unless ``chosen`` is a frame whose keys are keys of
    ``arrived``. Its columns are the person's to choose.
    """
    if not isinstance(chosen, pd.DataFrame):
        raise ValueError(f'choose gives a {type(chosen).__name__}, not a frame')
    if rows := [k for k in chosen.index if k not in arrived.index]:
        raise ValueError(f'choose gives rows {rows} that the source does not')


class BatchManager:
    """
    Makes, lists, changes and removes the batches of a workspace, holds the
    one :class:`~.reduction.Batch` of each label, and keeps which batch
    follows. See ``packages/essapps/docs/batch.md``.

    Every refusal of what is asked raises ``ValueError``, with a message for a
    person, such as for a spec that the workspace does not bind, a source that
    is not registered, or a label that is taken. ``KeyError`` says only that
    no batch has the label asked for, and :class:`~.definition.NotKept` that
    the definitions refuse a write. Anything else is an error of the code of
    the application, such as a source that raises.

    The headers are read when the manager is made. Each change of a header is
    written to the definitions before the manager holds it, so the manager
    lists the batches whose header the definitions keep. A write that they
    refuse raises :class:`~.definition.NotKept`, and what was written before
    it in the same call stays written.

    Parameters
    ----------
    client:
        The client of the workspace. Its batches persist what they submit.
    sources:
        The sources that a batch can be made with, by name.
    definitions:
        Where the batches are kept.
    settings:
        The settings that a new batch of a spec starts with, by the name of
        the spec; none for a spec not named. They are copied when a batch is
        made, so a change of them leaves the batches made before as they are.
        Those that are columns of the batch are left out.
    staged:
        The names of the specs whose batches run their rows through a stage,
        as :class:`~.reduction.Batch` takes it. It is read each time the
        manager makes the ``Batch`` of a label, so it applies to the batches
        made before too.
    """

    def __init__(
        self,
        client: Client,
        *,
        sources: Mapping[str, Source],
        definitions: DefinitionStore,
        settings: Mapping[str, Mapping[str, Any]] | None = None,
        staged: Collection[str] = (),
    ) -> None:
        self.client = client
        self.sources = sources
        self._definitions = definitions
        self._settings = {} if settings is None else settings
        self._staged = staged
        self._headers: dict[str, Header] = {}
        for label in definitions.labels():
            if (header := definitions.load(header_key(label), header_of)) is not None:
                self._headers[label] = header
        self._batches: dict[str, Batch] = {}
        following = definitions.load(FOLLOWING, following_of)
        self._following = following if following in self._headers else None

    @property
    def following(self) -> str | None:
        """
        The label of the batch that follows, ``None`` if none does. It is
        kept in the definitions and read when the manager is made; a label
        kept without a header counts as none.
        """
        return self._following

    def follow(self, label: str | None) -> None:
        """
        Make the batch of ``label`` the one that follows, so that no other
        does; with ``None``, let none follow. It makes no ``Batch``. Raises
        ``KeyError`` if no batch has the label, and ``ValueError`` if the
        batch was not made from a source, or its source is not registered.
        """
        if label is not None:
            self._source(self._made(label).source)
        self._definitions.save(FOLLOWING, following_data(label))
        self._following = label

    def rows_of(self, source: str) -> pd.DataFrame:
        """
        The table that the source registered as ``source`` makes from the
        files now (:func:`~.source.rows_of`). Raises ``ValueError`` if no
        source is registered so.
        """
        return rows_of(self.client, self._source(source))

    def create(
        self,
        spec: str,
        *,
        title: str,
        rows: pd.DataFrame | None = None,
        source: str | None = None,
        choose: Callable[[pd.DataFrame], pd.DataFrame] | None = None,
        description: str = '',
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> str:
        """
        Make a batch and keep it; its label. Nothing is written if a check
        fails. The header is written last, so a label without a header is no
        batch.

        With a source, the source is listed once, and the rows that are
        filled (:func:`~.source.filled`) are taken and considered::

            given = rows_of(client, source)
            arrived = given[filled]          # a value in each of source.fills
            table = arrived if choose is None else choose(arrived)
            considered = arrived.index       # the rows taken and those left out

        A row that waits is neither taken nor considered: Follow adds it once
        it is filled.

        Parameters
        ----------
        spec:
            The name of the spec. The batch is of the version the workspace
            binds, the highest if several.
        title:
            What the batch is called.
        rows:
            The first table of a batch without a source, as
            :meth:`~.reduction.Batch.put` takes it; its parameter columns are
            the blanks of the template. A table without rows or columns if
            not given.
        source:
            The name of a registered source to take the rows from. The batch
            keeps it, so that it can follow.
        choose:
            Makes the table of the batch from the rows of the source that are
            filled: some of its rows, with any columns, such as
            ``lambda given: given[given.run >= 60339].assign(thickness=pd.NA)``.
            All of them, as they are, if not given.
        description:
            What a person says of the batch.
        settings:
            The settings to start with, in place of those that the manager
            was given for the spec. Those that are columns are left out.
        label:
            The label of the batch. If not given, the time of making,
            ``YYYYMMDD-HHMMSS``, with ``-2``, ``-3``, ... added if that is
            taken.

        Raises
        ------
        ValueError
            If ``title`` is empty or only spaces; if ``label`` is not letters,
            digits, ``_``, ``.`` and ``-``, starting with none of the last
            two, or has definitions or records; if
            ``rows`` and ``source`` are both given, or ``choose`` without
            ``source``; if no source ``source`` is registered; if ``choose``
            gives a row that the source did not; if the workspace
            binds no spec ``spec``; or if the rows are refused, as ``put``
            refuses them.
        """
        if rows is not None and source is not None:
            raise ValueError('a batch made from a source takes its rows from it')
        if choose is not None and source is None:
            raise ValueError('choose picks rows of a source, and none is given')
        if label is None:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            label, n = stamp, 1
            while self._taken(label):
                n += 1
                label = f'{stamp}-{n}'
        else:
            _check_label(label)
            if self._taken(label):
                raise ValueError(f'{label}: the label has definitions or records')
        header = Header(title, description)
        bound = self._bound(spec)
        if settings is None:
            settings = self._settings.get(spec, {})
        made = None
        if source is not None:
            found = self._source(source)
            given = rows_of(self.client, found)
            arrived = given[filled(given, found.fills)]
            rows = arrived if choose is None else choose(arrived)
            _check_chosen(rows, arrived)
            made = FromSource(source, frozenset(arrived.index))
        # Refused here, before anything is written.
        table = typed(pd.DataFrame() if rows is None else rows, bound.params)
        batch = self._kept(label, bound, {}, None)
        try:
            batch.settings = settings
            batch.put(table)
            if made is not None:
                self._definitions.save(source_key(label), source_data(made))
            self._definitions.save(header_key(label), header_data(header))  # last
        except NotKept:
            with suppress(NotKept):  # the first error is what to say
                for key in _parts(label):
                    self._definitions.delete(key)
            raise
        self._headers[label] = header
        self._batches[label] = batch
        return label

    def _source(self, name: str) -> Source:
        """The source registered as ``name``; ``ValueError`` if none is."""
        if name not in self.sources:
            raise ValueError(f'the source {name} is not registered')
        return self.sources[name]

    def _bound(self, spec: str) -> WorkflowSpec[Any, Any]:
        """
        The spec of name ``spec`` that the workspace binds; ``ValueError`` if
        it binds none.
        """
        try:
            return self.client.spec_named(spec)
        except KeyError as error:
            raise ValueError(error.args[0]) from None

    def _taken(self, label: str) -> bool:
        """Whether ``label`` has definitions or records."""
        return label in self._definitions.labels() or bool(
            self.client.records(label=label)
        )

    def _from(self, label: str) -> FromSource | None:
        """
        What the batch of ``label`` keeps of its source, ``None`` if it was
        not made from one. Raises ``KeyError`` if no batch has the label.
        """
        if label not in self._headers:
            raise KeyError(label)
        return self._definitions.load(source_key(label), source_of)

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

    def headers(self) -> dict[str, Header]:
        """The header of each batch, by label, sorted by label."""
        return dict(sorted(self._headers.items()))

    def batch(self, label: str) -> Batch:
        """
        The batch of ``label``, made from its definitions at the first call.
        Ask for the batch each time it is used: one handed out before must not
        be used after :meth:`remove`.

        Raises ``KeyError`` if no batch has the label; ``ValueError`` if the
        workspace binds no spec of the name of its settings, or if its
        settings or rows cannot be read. A version of the spec that takes
        another kind of value at a column of the table, or that has a
        parameter of the name of a note, refuses the cells of the table, and
        the batch cannot be shown either.
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        if label not in self._headers:
            raise KeyError(label)
        template = self._definitions.load(settings_key(label), template_of)
        if template is None:
            raise ValueError(f'{label}: no settings are kept')
        spec = self._bound(template.spec.name)
        rows = self._definitions.load(
            rows_key(label), lambda data: typed(frame_of(data), spec.params)
        )
        batch = self._kept(label, spec, template.params, rows)
        self._batches[label] = batch
        return batch

    def _kept(
        self,
        label: str,
        spec: WorkflowSpec[Any, Any],
        settings: Mapping[str, Any],
        rows: pd.DataFrame | None,
    ) -> Batch:
        """The ``Batch`` of ``label``, which writes to the definitions."""
        return Batch(
            self.client,
            spec,
            label=label,
            settings=settings,
            rows=rows,
            staged=spec.name in self._staged,
            persist=True,
            definitions=self._definitions,
        )

    def change(
        self, label: str, *, title: str | None = None, description: str | None = None
    ) -> None:
        """
        Change what is given of the header of ``label``, and keep it. Raises
        ``KeyError`` if no batch has the label, and ``ValueError`` for an
        empty title.
        """
        header = self._headers[label]
        header = Header(
            header.title if title is None else title,
            header.description if description is None else description,
        )
        self._definitions.save(header_key(label), header_data(header))
        self._headers[label] = header

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
        if (label := self._following) is None:
            return None
        made = self._made(label)
        batch = self.batch(label)
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
        whether the row waits, so that Follow adds it once it is filled. A row
        that does not wait was left out when the batch was made, or removed
        from it, and is considered, so Follow does not add it; or it was
        filled while the batch did not follow, and Follow adds it at its next
        step. A column of the source named ``waits`` is replaced by it.

        Raises ``KeyError`` if no batch has the label, and ``ValueError`` if
        it was not made from a source, or its source is not registered.
        """
        made = self._made(label)
        source = self._source(made.source)
        given = rows_of(self.client, source)
        table = self.batch(label).frame()
        _, waiting = added(table, given, source.fills, made.considered)
        offered = given[~given.index.isin(table.index)]
        return offered.assign(waits=offered.index.isin(waiting))

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
        batch = self.batch(label)
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
            kept = FromSource(made.source, considered)
            self._definitions.save(source_key(label), source_data(kept))

    def remove(self, label: str) -> None:
        """
        Forget the batch of ``label`` and remove its definitions; its records
        stay, and those running keep running. If the batch follows, none
        follows from then on. A ``Batch`` of the label handed out before must
        not be used after: it would write its definitions again. A label that
        keeps tables but no header, as a ``create`` or ``remove`` cut off
        leaves it, is removed too, and can then be made again. Raises
        ``KeyError`` if no batch has the label and no table is kept of it.

        If the batch follows, ``following`` is written first, so that it
        never names a label without a header. Then the header is deleted, so
        a batch is never listed half removed.
        """
        if label not in self._headers and label not in self._definitions.labels():
            raise KeyError(label)
        if label == self._following:
            self._definitions.save(FOLLOWING, following_data(None))
            self._following = None
        self._definitions.delete(header_key(label))
        self._headers.pop(label, None)
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        for key in _parts(label):
            self._definitions.delete(key)

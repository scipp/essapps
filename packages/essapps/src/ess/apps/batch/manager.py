# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them. It knows no row source: :class:`~.intake.Intake` brings the
rows of a source into a batch. The guide is the section "The batches of a
workspace" of ``packages/essapps/docs/batch.md``; the decisions are
``docs/developer/adr/0011``, ``0012`` and ``0013``.

A batch is a label with a header. A :class:`~.reduction.Batch` is the object
that holds its template and its table; the manager makes the one ``Batch`` of
each batch. A batch is kept as these tables of the definitions
(:mod:`.definition`), each with one writer::

    header      title, description                      BatchManager
    settings    the template, with the fills as blanks  the Batch
    rows        the table                               the Batch
    source      the source, the keys considered, and    Intake; only for a
                whether the batch follows               batch made from a source

so a manager made again, after a restart, makes the same ``Batch`` of a label
from them, the specs that the workspace binds, and the specs that run staged.
Making a batch makes its ``Batch``, which writes the settings and the rows;
then the manager writes the header: a label without a header is not a batch.
Removing a batch deletes the header first, then every other table of the
label, whoever wrote it. So a ``create`` or a ``remove`` cut off at any
write leaves a label with tables and no header, which
:meth:`BatchManager.remove` removes. After a restart, the ``Batch`` of a
label is made at the first call of :meth:`BatchManager.batch`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Collection, Mapping
from contextlib import suppress
from typing import Any

import pandas as pd

from ess.spec import WorkflowSpec

from ..client import Client
from .definition import (
    DefinitionStore,
    Header,
    NotKept,
    header_data,
    header_key,
    header_of,
    label_keys,
    rows_key,
    settings_key,
    template_of,
)
from .reduction import Batch
from .table import frame_of, typed


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


class BatchManager:
    """
    Makes, lists, changes and removes the batches of a workspace, and holds
    the one :class:`~.reduction.Batch` of each label. See
    ``packages/essapps/docs/batch.md``.

    Every refusal of what is asked raises ``ValueError``, with a message for a
    person, such as for a spec that the workspace does not bind, or a label
    that is taken. ``KeyError`` says only that no batch has the label asked
    for, and :class:`~.definition.NotKept` that the definitions refuse a
    write. Anything else is an error of the code of the application.

    :attr:`version` moves whenever :meth:`headers` may give something else:
    while it is the same, they give the same. A batch counts its own changes
    (:attr:`.reduction.Batch.version`).

    The headers are read when the manager is made. Each change of a header is
    written to the definitions before the manager holds it, so the manager
    lists the batches whose header the definitions keep. A write that they
    refuse raises :class:`~.definition.NotKept`, and what was written before
    it in the same call stays written.

    Parameters
    ----------
    client:
        The client of the workspace. Its batches persist what they submit.
    definitions:
        Where the batches are kept.
    settings:
        The settings that a new batch of a spec starts with, by the name of
        the spec; none for a spec not named. They are copied when a batch is
        made, so a change of them leaves the batches made before as they are.
        :meth:`create` leaves out those at ``fills``.
    staged:
        The names of the specs whose batches run their rows through a stage,
        as :class:`~.reduction.Batch` takes it. It is read each time the
        manager makes the ``Batch`` of a label, so it applies to the batches
        made before too.

    Attributes
    ----------
    definitions:
        Where the batches are kept, also the tables that
        :class:`~.intake.Intake` writes.
    """

    def __init__(
        self,
        client: Client,
        *,
        definitions: DefinitionStore,
        settings: Mapping[str, Mapping[str, Any]] | None = None,
        staged: Collection[str] = (),
    ) -> None:
        self.client = client
        self.definitions = definitions
        self._settings = {} if settings is None else settings
        self._staged = staged
        self._headers: dict[str, Header] = {}
        for label in definitions.labels():
            if (header := definitions.load(header_key(label), header_of)) is not None:
                self._headers[label] = header
        self._batches: dict[str, Batch] = {}
        self._version = 0

    @property
    def version(self) -> int:
        """Counts the changes of the headers."""
        return self._version

    def create(
        self,
        spec: str,
        *,
        title: str,
        rows: pd.DataFrame | None = None,
        fills: Collection[str] = (),
        columns: Collection[str] = (),
        description: str = '',
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> Batch:
        """
        Make a batch and keep it; its ``Batch``, whose ``label`` names it.
        Nothing is written if a check fails; :meth:`check` comes first. The
        header is written after the
        other tables of the batch, so a label without a header is no batch.

        If the definitions refuse a write, the tables written are deleted,
        and what was kept before stays. If a delete is refused too, as when
        the process stops, what is left is a label with tables and no header,
        which :meth:`remove` removes.

        Parameters
        ----------
        spec:
            The name of the spec. The batch is of the version the workspace
            binds, the highest if several.
        title:
            What the batch is called.
        rows:
            The first table, as :meth:`~.reduction.Batch.put` takes it; its
            parameter columns are the blanks of the template. A table without
            rows or columns if not given.
        fills:
            The parameters that a row source fills
            (:attr:`.reduction.Batch.fills`), kept as the blanks of the
            settings: each is a column of ``rows``, an empty cell there means
            that the row waits for its file, and the settings given are left
            out there (:meth:`.reduction.Batch.settable`).
        columns:
            Parameters that are columns of the batch, of empty cells, besides
            those of ``rows``; a column of ``rows`` keeps its cells.
        description:
            What a person says of the batch.
        settings:
            The settings to start with, in place of those that the manager
            was given for the spec; those at ``fills`` are left out.
        label:
            The label of the batch. If not given, the time of making,
            ``YYYYMMDD-HHMMSS``, with ``-2``, ``-3``, ... added if that is
            taken.

        Raises
        ------
        ValueError
            If ``title`` is empty or only spaces; if ``label`` is not letters,
            digits, ``_``, ``.`` and ``-``, starting with none of the last
            two, or has definitions or records; if the workspace binds no
            spec ``spec``; or if the rows are refused, as ``put`` refuses
            them, also for a table without a column of ``fills``.
        """
        self.check(spec, title=title, label=label)
        if label is None:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            label, n = stamp, 1
            while self._taken(label):
                n += 1
                label = f'{stamp}-{n}'
        header = Header(title, description)
        bound = self.spec_named(spec)
        if settings is None:
            settings = self._settings.get(spec, {})
        table = pd.DataFrame() if rows is None else rows
        empty = [c for c in columns if c not in table.columns]
        # Refused here, before anything is written.
        table = typed(table.assign(**dict.fromkeys(empty)), bound.params)
        batch = self._kept(label, bound, {}, None, fills)
        _, *written = label_keys(label)  # what to delete if a write is refused
        try:
            batch.put(table)
            batch.settings = batch.settable(settings)
            self.definitions.save(header_key(label), header_data(header))
            written.insert(0, header_key(label))
        except NotKept:
            with suppress(NotKept):  # the first error is what to say
                for key in written:
                    self.definitions.delete(key)
            raise
        self._headers[label] = header
        self._batches[label] = batch
        self._version += 1
        return batch

    def check(self, spec: str, *, title: str, label: str | None) -> None:
        """
        Raise ``ValueError`` as :meth:`create` refuses ``spec``, ``title``
        and ``label``, without rows: so a caller that lists rows first, such
        as :meth:`.intake.Intake.create`, refuses in the same order.
        """
        if label is not None:
            _check_label(label)
            if self._taken(label):
                raise ValueError(f'{label}: the label has definitions or records')
        Header(title)
        self.spec_named(spec)

    def spec_named(self, spec: str) -> WorkflowSpec[Any, Any]:
        """
        The spec of name ``spec`` that the workspace binds, the highest
        version if several; ``ValueError`` if it binds none.
        """
        try:
            return self.client.spec_named(spec)
        except KeyError as error:
            raise ValueError(error.args[0]) from None

    def _taken(self, label: str) -> bool:
        """Whether ``label`` has definitions or records."""
        return label in self.definitions.labels() or bool(
            self.client.records(label=label)
        )

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

        The blanks of the settings kept are the parameters that the source of
        the batch filled when it was made (:attr:`.reduction.Batch.fills`).
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        if label not in self._headers:
            raise KeyError(label)
        template = self.definitions.load(settings_key(label), template_of)
        if template is None:
            raise ValueError(f'{label}: no settings are kept')
        spec = self.spec_named(template.spec.name)
        rows = self.definitions.load(
            rows_key(label), lambda data: typed(frame_of(data), spec.params)
        )
        batch = self._kept(label, spec, template.params, rows, template.blanks)
        self._batches[label] = batch
        return batch

    def _kept(
        self,
        label: str,
        spec: WorkflowSpec[Any, Any],
        settings: Mapping[str, Any],
        rows: pd.DataFrame | None,
        fills: Collection[str],
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
            definitions=self.definitions,
            fills=fills,
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
        self.definitions.save(header_key(label), header_data(header))
        self._headers[label] = header
        self._version += 1

    def remove(self, label: str) -> None:
        """
        Forget the batch of ``label`` and remove every table of it
        (:func:`~.definition.label_keys`); its records stay, and those running
        keep running. A ``Batch`` of the label handed out before must not be
        used after: it would write its definitions again. A label that keeps
        tables but no header, as a ``create`` or ``remove`` cut off leaves
        it, is removed too, and can then be made again. Raises ``KeyError``
        if no batch has the label and no table is kept of it.

        The header is deleted first, so a batch is never listed half removed.
        Whether the batch follows is kept with the label, so it goes too.
        """
        if label not in self._headers and label not in self.definitions.labels():
            raise KeyError(label)
        header, *parts = label_keys(label)
        self.definitions.delete(header)
        self._headers.pop(label, None)
        self._version += 1
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        for key in parts:
            self.definitions.delete(key)

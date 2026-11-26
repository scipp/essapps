# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them.

The manager knows no row source: :class:`~.intake.Intake` brings the rows of a
source into a batch. A label is a batch if and only if it has a header. The
order of the writes that keeps this true when a process stops: guide
``packages/essbatch/docs/batch.md``, "What is stored on disk".

Guide: "The batches of a workspace". Decisions: ``docs/developer/adr/0011``,
``0012`` and ``0013``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Collection, Mapping
from contextlib import suppress
from typing import Any

import pandas as pd

from ess.apps.client import Client
from ess.spec import WorkflowSpec

from .definition import (
    HEADER,
    LABEL_ENTRIES,
    SETTINGS,
    TABLE,
    DefinitionStore,
    DefinitionWriteError,
    Header,
)
from .reduction import Batch
from .table import typed


def _check_label(label: str) -> None:
    """Raise ``ValueError`` unless ``label`` can be one segment of a URL path."""
    if re.fullmatch(r'\w[\w.-]*', label) is None:
        raise ValueError(
            f'{label!r}: a label is letters, digits, "_", "." and "-", '
            'and starts with a letter, a digit or "_"'
        )


class BatchManager:
    """
    Makes, lists, changes and removes the batches of a workspace, and holds
    the one :class:`~.reduction.Batch` of each label.

    It raises the errors of ``batch.md``, "Errors". The headers are read once, when
    the manager is made.

    Parameters
    ----------
    client:
        The client of the workspace. The batches persist what they submit.
    definitions:
        Where the batches are stored.
    settings:
        The first settings of a new batch, by spec name. They are copied when
        a batch is made.
    staged:
        The names of the specs whose batches run their rows through a stage.
        Read each time the manager makes a ``Batch``, so it applies to batches
        made before too.
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
            if (header := HEADER.load(definitions, label)) is not None:
                self._headers[label] = header
        self._batches: dict[str, Batch] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        """Changes whenever :meth:`headers` may return something else."""
        return self._revision

    def create(
        self,
        spec: str,
        *,
        title: str,
        table: pd.DataFrame | None = None,
        source_columns: Collection[str] = (),
        parameter_columns: Collection[str] = (),
        description: str = '',
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> Batch:
        """
        Make a batch, write it to the definitions, and return its ``Batch``.

        Nothing is written if a check fails. If the definitions reject a
        write, the entries written are deleted.

        Parameters
        ----------
        spec:
            The name of the spec. The batch is of the version that the
            workspace binds, the highest if several.
        table:
            The first batch table, as :meth:`~.reduction.Batch.set_table`
            accepts it. Empty if not given.
        source_columns:
            The parameters that a row source fills
            (:attr:`.reduction.Batch.source_columns`). Settings there are
            dropped.
        parameter_columns:
            Parameter columns of empty cells to add to ``table``.
        settings:
            The first settings, in place of those of the manager for the spec.
        label:
            The label of the batch. If not given, the time of making,
            ``YYYYMMDD-HHMMSS``, with ``-2``, ``-3``, ... added if taken.

        Raises
        ------
        ValueError
            As :meth:`check` and :meth:`~.reduction.Batch.set_table` raise.
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
        table = pd.DataFrame() if table is None else table
        empty = [c for c in parameter_columns if c not in table.columns]
        # Refused here, before anything is written.
        table = typed(table.assign(**dict.fromkeys(empty)), bound.params)
        batch = self._new_batch(label, bound, {}, None, source_columns)
        # what to delete if a write is rejected
        written = [entry.key(label) for entry in LABEL_ENTRIES[1:]]
        try:
            batch.set_table(table)
            batch.settings = batch.settable(settings)
            HEADER.save(self.definitions, label, header)
            written.insert(0, HEADER.key(label))
        except DefinitionWriteError:
            with suppress(DefinitionWriteError):  # the first error is what to say
                for key in written:
                    self.definitions.delete(key)
            raise
        self._headers[label] = header
        self._batches[label] = batch
        self._revision += 1
        return batch

    def check(self, spec: str, *, title: str, label: str | None) -> None:
        """
        Raise ``ValueError`` if :meth:`create` would reject ``spec``, ``title``
        or ``label``.

        A label is taken if it has definitions or records. For a caller that
        lists rows first, such as :meth:`.intake.Intake.create`, so that it
        raises in the same order as :meth:`create`.
        """
        if label is not None:
            _check_label(label)
            if self._taken(label):
                raise ValueError(f'{label}: the label has definitions or records')
        Header(title)
        self.spec_named(spec)

    def spec_named(self, spec: str) -> WorkflowSpec[Any, Any]:
        """
        The spec named ``spec`` that the workspace binds, the highest version
        if several. Raises ``ValueError`` if it binds none.
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
        The ``Batch`` of ``label``, made from its definitions at the first
        call.

        Ask for the batch each time: one returned before :meth:`remove` must
        not be used after.

        Raises ``KeyError`` if no batch has the label. Raises ``ValueError`` if
        the workspace binds no spec of that name, or the settings or the batch
        table cannot be read. A spec version that accepts another kind of value
        at a column, or has a parameter named like a note column, rejects the
        batch table.
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        if label not in self._headers:
            raise KeyError(label)
        template = SETTINGS.load(self.definitions, label)
        if template is None:
            raise ValueError(f'{label}: no settings are stored')
        spec = self.spec_named(template.spec.name)
        table = self.definitions.load(
            TABLE.key(label), lambda data: typed(TABLE.read(data), spec.params)
        )
        batch = self._new_batch(label, spec, template.params, table, template.blanks)
        self._batches[label] = batch
        return batch

    def _new_batch(
        self,
        label: str,
        spec: WorkflowSpec[Any, Any],
        settings: Mapping[str, Any],
        table: pd.DataFrame | None,
        source_columns: Collection[str],
    ) -> Batch:
        """The ``Batch`` of ``label``, which writes to the definitions."""
        return Batch(
            self.client,
            spec,
            label=label,
            settings=settings,
            table=table,
            staged=spec.name in self._staged,
            persist=True,
            definitions=self.definitions,
            source_columns=source_columns,
        )

    def change(
        self, label: str, *, title: str | None = None, description: str | None = None
    ) -> None:
        """Change the title or the description of ``label``, and write the header."""
        header = self._headers[label]
        header = Header(
            header.title if title is None else title,
            header.description if description is None else description,
        )
        HEADER.save(self.definitions, label, header)
        self._headers[label] = header
        self._revision += 1

    def remove(self, label: str) -> None:
        """
        Remove the batch of ``label`` and every entry of the label. Its records
        stay, and running ones keep running.

        A ``Batch`` of the label returned before must not be used after: it
        would write its definitions again. A label with entries and no header,
        as an interrupted :meth:`create` or :meth:`remove` leaves it, is
        removed too. Raises ``KeyError`` if the label has neither a batch nor
        an entry.
        """
        if label not in self._headers and label not in self.definitions.labels():
            raise KeyError(label)
        header, *parts = LABEL_ENTRIES
        self.definitions.delete(header.key(label))
        self._headers.pop(label, None)
        self._revision += 1
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        for entry in parts:
            self.definitions.delete(entry.key(label))

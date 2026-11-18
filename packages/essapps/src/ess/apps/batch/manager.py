# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them. The guide is the section "The batches of a workspace" of
``packages/essapps/docs/batch.md``; the decision is
``docs/developer/adr/0011-a-person-makes-the-batches-of-a-workspace.md``.

A batch is kept as three tables of the definitions (:mod:`.definition`)::

    header      title, description, the name of its registered source, scope
    settings    a template: the spec by name, the columns as its blanks
    edits       what a person set on its rows

so a manager made again, after a restart, makes the same ``Batch`` of a label
from them, the registered sources and the defaults. Making a batch writes its
settings, then its header: a label without a header is not a batch. The
``Batch`` is made at the first call of :meth:`BatchManager.batch`, and writes
the settings and the edits from then on.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from ess.spec import WorkflowSpec

from ..client import Client
from ..errors import SubmitError
from ..files import Selector
from ..records import Template
from .definition import (
    DefinitionStore,
    Header,
    NotKept,
    edits_key,
    header_data,
    header_key,
    header_of,
    settings_data,
    settings_key,
    template_of,
)
from .reduction import Batch, RowSource, file_infos
from .state import RowState
from .table import Scope


class Words(BaseModel):
    """What a row is called, as ``1 sample``, ``2 samples``."""

    one: str = 'row'
    many: str = 'rows'

    def count(self, n: int) -> str:
        return f'{n} {self.one if n == 1 else self.many}'


@dataclass(frozen=True)
class Source:
    """
    A source of rows that an application registers by name.

    Parameters
    ----------
    rows:
        Makes rows from files.
    columns:
        The parameters that its rows fill. They are columns of each batch of
        it, and Follow reduces a row again for a change at them.
    selector:
        The files that ``rows`` is given; raw files if not given.
    words:
        What a row is called on the page of a batch.
    name:
        The entry of :attr:`~.table.Row.info` that names a row on the page;
        its key if ``None``.
    info:
        Other entries of :attr:`~.table.Row.info` that the page shows as
        columns.
    index:
        What the page calls the :attr:`~.table.Row.index` that ``rows``
        gives, such as ``'Run'``.
    """

    rows: RowSource
    columns: tuple[str, ...]
    selector: Selector | None = None
    words: Words = field(default_factory=Words)
    name: str | None = None
    info: tuple[str, ...] = ()
    index: str = 'Index'


@dataclass(frozen=True)
class Defaults:
    """
    What an application gives a new batch of one spec.

    Parameters
    ----------
    settings:
        The settings that a new batch starts with; those that are columns of
        the batch are left out.
    staged:
        Run the rows through a stage, as :class:`~.reduction.Batch` takes it.
    """

    settings: Mapping[str, Any] = field(default_factory=dict)
    staged: bool = False


@dataclass(frozen=True)
class Counts:
    """
    What :meth:`BatchManager.counts` gives: where the rows of each batch
    stand, and how many rows of each source no batch shows.

    Parameters
    ----------
    states:
        The state of each row, by key, of each batch that could be made and
        scanned, by label. Count them with :func:`~.state.status_texts`.
    failed:
        Why each other batch could not be made or scanned, by label.
    outside:
        The rows that each registered source makes and no batch of it shows,
        by its name. A row counts as shown if any batch of the source shows
        it, so a row removed by hand, or outside a scope, counts here only if
        every batch of the source removed it or leaves it out. A source is
        left out if one of its batches failed, or if it raised.
    """

    states: dict[str, dict[str, RowState]]
    failed: dict[str, str]
    outside: dict[str, int]


def _said(error: Exception) -> str:
    """What an exception says, without the quotes of a ``KeyError``."""
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error) or repr(error)


def bound_spec(client: Client, name: str) -> WorkflowSpec[Any, Any]:
    """
    The spec of ``name`` that the workspace binds, the highest version if it
    binds several; raises ``KeyError`` if it binds none.
    """
    specs = [spec for spec in client.specs() if spec.name == name]
    if not specs:
        raise KeyError(f'the workspace binds no spec {name}')
    return max(specs, key=lambda spec: spec.version)


def check_label(label: str) -> None:
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

    The headers are read when the manager is made. Each change of a header is
    written to the definitions before the manager holds it, and one that
    they refuse raises :class:`~.definition.NotKept`, so the manager lists
    the batches whose header the definitions keep. :meth:`create` and
    :meth:`remove` say what they leave when a write is refused.

    Parameters
    ----------
    client:
        The client of the workspace. Its batches persist what they submit.
    sources:
        The sources that a batch can be made with, by name.
    definitions:
        Where the batches are kept.
    defaults:
        What a new batch of a spec gets, by the spec's name; ``Defaults()``
        for a spec not named.
    """

    def __init__(
        self,
        client: Client,
        *,
        sources: Mapping[str, Source],
        definitions: DefinitionStore,
        defaults: Mapping[str, Defaults] | None = None,
    ) -> None:
        self._client = client
        self.sources = sources
        self._definitions = definitions
        self._defaults = {} if defaults is None else defaults
        self._headers: dict[str, Header] = {}
        for label in definitions.labels():
            if (header := definitions.load(header_key(label), header_of)) is not None:
                self._headers[label] = header
        self._batches: dict[str, Batch] = {}

    def create(
        self,
        spec: str,
        *,
        title: str,
        source: str | None = None,
        columns: Sequence[str] = (),
        description: str = '',
        scope: Scope | None = None,
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> str:
        """
        Make a batch and keep it; its label. If saving the header is refused,
        the settings written before it are deleted, and ``NotKept`` raised.

        Parameters
        ----------
        spec:
            The name of the spec. The batch is of the version the workspace
            binds, the highest if several.
        title:
            What the batch is called.
        source:
            The name of a registered source; without one, a batch has only
            rows added by hand.
        columns:
            Parameters made columns, besides those the source fills.
        description:
            What a person says of the batch.
        scope:
            The rows of the source that the batch holds; every row if not
            given.
        settings:
            The settings to start with, in place of the defaults of the spec.
            Those that are columns are left out.
        label:
            The label of the batch. If not given, the time of making,
            ``YYYYMMDD-HHMMSS``, with ``-2``, ``-3``, ... added if that is
            taken.

        Raises
        ------
        KeyError
            If the workspace binds no spec ``spec``, or no source ``source``
            is registered.
        ValueError
            If ``label`` is not one that :func:`check_label` takes, or has
            definitions or records; or if a column is not a parameter of the
            spec.
        """
        if label is None:
            stamp = time.strftime('%Y%m%d-%H%M%S')
            label, n = stamp, 1
            while self._taken(label):
                n += 1
                label = f'{stamp}-{n}'
        check_label(label)
        if self._taken(label):
            raise ValueError(f'{label}: the label has definitions or records')
        bound = bound_spec(self._client, spec)
        if source is not None:
            columns = (*self.sources[source].columns, *columns)
        columns = tuple(dict.fromkeys(columns))
        if unknown := sorted(set(columns) - bound.params.model_fields.keys()):
            raise ValueError(f'{bound.title} has no parameters {unknown}')
        if settings is None:
            settings = self._defaults.get(spec, Defaults()).settings
        # The template leaves out the settings that are columns.
        template = Template(bound, params=settings, blanks=columns)
        self._definitions.save(settings_key(label), settings_data(template))
        scope = Scope() if scope is None else scope
        header = Header(title, description, source, scope)
        try:
            self._definitions.save(header_key(label), header_data(header))
        except NotKept:
            # so that the label is not left taken by settings alone
            with suppress(NotKept):
                self._definitions.delete(settings_key(label))
            raise
        self._headers[label] = header
        return label

    def _taken(self, label: str) -> bool:
        """Whether ``label`` has definitions or records."""
        return label in self._definitions.labels() or bool(
            self._client.records(label=label)
        )

    def batches(self) -> dict[str, Header]:
        """The header of each batch, by label, sorted by label."""
        return dict(sorted(self._headers.items()))

    def batch(self, label: str) -> Batch:
        """
        The batch of ``label``, made from its definitions at the first call
        and scanned once if it has a source. If the scan raises, the batch is
        not kept, and the next call makes it again. Ask for the batch each
        time it is used: one handed out before must not be used after
        :meth:`remove`.

        Raises ``KeyError`` if no batch has the label, or the workspace binds
        no spec of its name, or its source is not registered; ``ValueError``
        if its definitions cannot be read, or its source fills a parameter
        that is not a column of the batch.
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        source = self.source(label)
        template = self._definitions.load(settings_key(label), template_of)
        if template is None:
            raise ValueError(f'{label}: the definitions hold no settings')
        if source is not None and (
            missing := sorted(set(source.columns) - set(template.blanks))
        ):
            raise ValueError(
                f'{label}: its source fills {missing}, which are not columns of '
                'the batch; the source registered changed since it was made'
            )
        spec = bound_spec(self._client, template.spec.name)
        batch = Batch(
            self._client,
            spec,
            columns=template.blanks,
            label=label,
            source=None if source is None else source.rows,
            selector=None if source is None else source.selector,
            staged=self._defaults.get(spec.name, Defaults()).staged,
            persist=True,
            definitions=self._definitions,
            scope=self._headers[label].scope,
        )
        if source is not None:
            batch.scan()
        self._batches[label] = batch
        return batch

    def source(self, label: str) -> Source | None:
        """
        The registered source of the batch of ``label``, ``None`` if it has
        none. Raises ``KeyError`` if no batch has the label.
        """
        name = self._headers[label].source
        if name is not None and name not in self.sources:
            raise KeyError(f'{label}: the source {name} is not registered')
        return None if name is None else self.sources[name]

    def counts(self) -> Counts:
        """
        Where the rows of every batch stand, and the rows of each registered
        source that no batch shows (see :class:`Counts`). It makes every batch
        not made yet, which scans it, and scans every other batch with a
        source, so the rows of each batch are those of the files now. While
        the settings of a batch are refused, its rows are as
        :meth:`~.reduction.Batch.results` gives them. A source is called once
        more only if no batch of it was scanned.
        """
        states: dict[str, dict[str, RowState]] = {}
        failed: dict[str, str] = {}
        made: dict[str, set[str]] = {}  # the keys of the rows each source made
        shown: dict[str, set[str]] = {name: set() for name in self.sources}
        for label, header in self.batches().items():
            try:
                new = label not in self._batches
                batch = self.batch(label)  # scans a batch that it makes
                if not new and batch.source is not None:
                    batch.scan()
                try:
                    states[label] = batch.state()
                except SubmitError:
                    states[label] = batch.results()
            except Exception as error:  # a source is code of the application
                failed[label] = _said(error)
                if header.source is not None:  # its rows shown are not known
                    shown.pop(header.source, None)
                continue
            if header.source is not None and header.source in shown:
                made[header.source] = {row.key for row in batch.rows.scan}
                shown[header.source] |= {row.key for row in batch.rows}
        outside = {}
        for name, keys in shown.items():
            source = self.sources[name]
            if name not in made:
                with suppress(Exception):  # a source is code of the application
                    infos = file_infos(self._client, source.selector)
                    made[name] = {row.key for row in source.rows(infos)}
            if name in made:
                outside[name] = len(made[name] - keys)
        return Counts(states, failed, outside)

    def edit(
        self,
        label: str,
        *,
        title: str | None = None,
        description: str | None = None,
        scope: Scope | None = None,
    ) -> Header:
        """
        The header of ``label`` with what is given changed, as kept. A scope
        changes the rows of the batch at once, without a scan.
        """
        header = self._headers[label]
        header = replace(
            header,
            title=header.title if title is None else title,
            description=header.description if description is None else description,
            scope=header.scope if scope is None else scope,
        )
        self._definitions.save(header_key(label), header_data(header))
        self._headers[label] = header
        if (batch := self._batches.get(label)) is not None:
            batch.scope = header.scope
        return header

    def remove(self, label: str) -> None:
        """
        Forget the batch of ``label`` and remove its definitions; its records
        stay, and those running keep running. A ``Batch`` of the label handed
        out before must not be used after: it would write its definitions
        again. Raises ``KeyError`` if no batch has the label.

        The header is deleted first, so a batch is never listed half removed.
        If the definitions refuse a later delete, ``NotKept`` is raised, and
        the label keeps that part and cannot be made again.
        """
        if label not in self._headers:
            raise KeyError(label)
        self._definitions.delete(header_key(label))
        del self._headers[label]
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        self._definitions.delete(edits_key(label))
        self._definitions.delete(settings_key(label))

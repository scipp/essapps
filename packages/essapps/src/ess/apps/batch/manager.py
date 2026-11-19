# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them, and ends one at a row to start the next. The guide is the
section "The batches of a workspace" of ``packages/essapps/docs/batch.md``;
the decision is
``docs/developer/adr/0011-a-person-makes-the-batches-of-a-workspace.md``.

A batch is kept as three tables of the definitions (:mod:`.definition`), each
with one writer::

    header      the spec by name, the columns, the name     BatchManager
                of its source, title, description, scope
    settings    a template of the settings, its spec and    Batch.settings; the
                blanks a copy of the header's               first by the manager
    edits       what a person set on its rows               Batch.rows

so a manager made again, after a restart, makes the same ``Batch`` of a label
from them, the registered sources and the defaults. Making a batch writes its
first settings, before any ``Batch`` of the label exists, then its header: a
label without a header is not a batch, and a header without settings is a
batch with the default settings of its spec. The ``Batch`` of a label is made
at the first call of :meth:`BatchManager.batch`, and writes the settings and
the edits from then on.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ess.spec import WorkflowSpec

from ..client import Client
from ..records import Template
from .definition import (
    DefinitionStore,
    Header,
    edits_key,
    header_data,
    header_key,
    header_of,
    settings_key,
    template_data,
)
from .reduction import Batch, Source, check_fills
from .table import Index, Scope


@dataclass(frozen=True)
class Defaults:
    """
    What an application gives a new batch of one spec.

    Parameters
    ----------
    settings:
        The settings that a new batch starts with, copied once when it is
        made, so a change of them leaves the batches made before as they are.
        Those that are columns of the batch are left out. A batch whose
        settings are not kept starts with them too.
    staged:
        Run the rows through a stage, as :class:`~.reduction.Batch` takes it.
        It is read at every start, and so applies to the batches made before
        too.
    """

    settings: Mapping[str, Any] = field(default_factory=dict)
    staged: bool = False


def fits(spec: WorkflowSpec[Any, Any], source: Source | None) -> bool:
    """
    Whether a batch of ``spec`` can be made with ``source``: whether each
    parameter that the source fills is a parameter of the spec. Every spec
    fits no source.
    """
    fills = () if source is None else source.fills
    return set(fills) <= spec.params.model_fields.keys()


_KINDS = {int: 'a whole number', float: 'a number', str: 'text'}


def _scope_of(source: Source | None, scope: Scope) -> Scope:
    """
    ``scope`` as a slice of the rows of ``source``: each end converted to the
    type of its index (:attr:`~.reduction.Source.index_type`), such as
    ``'60339'`` to ``60339``. Raises ``ValueError`` for an end that does not
    convert, naming it; for an end without a source, since a batch without
    one has only rows added by hand, which have no index; and for a ``stop``
    below the ``start``.
    """
    if scope == Scope():
        return scope
    if source is None:
        raise ValueError(
            'a batch without a source has only rows added by hand, which have '
            'no index, so its scope has no start or stop'
        )
    adapter = TypeAdapter(source.index_type)

    def end(value: Any) -> Any:
        if value is None:
            return None
        try:
            return adapter.validate_python(value)
        except ValidationError:
            kind = _KINDS[source.index_type]
            raise ValueError(f'{source.index_title} {value!r} is not {kind}') from None

    scope = Scope(start=end(scope.start), stop=end(scope.stop))
    start, stop = scope.start, scope.stop
    if start is not None and stop is not None and stop < start:  # type: ignore[operator]
        raise ValueError(
            f'the stop of a scope, {scope.stop!r}, is below its start, {scope.start!r}'
        )
    return scope


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
        self.client = client
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
        columns: Sequence[str] | None = None,
        description: str = '',
        scope: Scope | None = None,
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> str:
        """
        Make a batch and keep it; its label. Nothing is written if a check
        fails. The first settings are written before the header, so a label
        without a header is no batch.

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
            All parameters that each row gives; those that the source fills if
            not given.
        description:
            What a person says of the batch.
        scope:
            The rows of the source that the batch holds; every row if not
            given. Its ends are converted to the type of the index of the
            source, such as ``'60339'`` to ``60339``.
        settings:
            The settings to start with, in place of the defaults of the spec.
            Those that are columns are left out.
        label:
            The label of the batch. If not given, the time of making,
            ``YYYYMMDD-HHMMSS``, with ``-2``, ``-3``, ... added if that is
            taken.

        Raises
        ------
        ValueError
            If ``title`` is empty or only spaces; if ``label`` is not one that
            :func:`check_label` takes, or has definitions or records; if no
            source ``source`` is registered; if the workspace binds no spec
            ``spec``, or it does not fit the source (:func:`fits`); if a column
            is not a parameter of the spec, or is named twice; if the source
            fills a parameter that is not among the columns; or for an end
            of the scope that does not convert, an end without a source, or a
            stop below the start.
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
        registered = self._source(source)
        fills = () if registered is None else registered.fills
        columns = fills if columns is None else tuple(columns)
        bound = self._bound(spec, columns)
        if not fits(bound, registered):
            lacks = sorted(set(fills) - bound.params.model_fields.keys())
            raise ValueError(
                f'{bound.title} has no parameters {lacks}, which the source '
                f'{source} fills'
            )
        if len(set(columns)) != len(columns):
            raise ValueError(f'a column is named twice: {list(columns)}')
        check_fills(label, registered, columns)
        header = Header(
            spec=spec,
            columns=columns,
            source=source,
            title=title,
            description=description,
            scope=_scope_of(registered, Scope() if scope is None else scope),
        )
        if settings is None:
            settings = self._defaults.get(spec, Defaults()).settings
        # The template leaves out the settings that are columns.
        template = Template(bound, params=settings, blanks=columns)
        self._definitions.save(settings_key(label), template_data(template))
        self._definitions.save(header_key(label), header_data(header))
        self._headers[label] = header
        return label

    def _source(self, name: str | None) -> Source | None:
        """
        The source registered as ``name``, ``None`` for none; ``ValueError``
        if none is registered so.
        """
        if name is None:
            return None
        if name not in self.sources:
            raise ValueError(f'the source {name} is not registered')
        return self.sources[name]

    def _bound(self, spec: str, columns: Sequence[str]) -> WorkflowSpec[Any, Any]:
        """
        The spec of name ``spec`` that the workspace binds; ``ValueError`` if
        it binds none, or if one of ``columns`` is not a parameter of it.
        """
        try:
            bound = self.client.spec_named(spec)
        except KeyError as error:
            raise ValueError(error.args[0]) from None
        if unknown := sorted(set(columns) - bound.params.model_fields.keys()):
            raise ValueError(f'{bound.title} has no parameters {unknown}')
        return bound

    def _taken(self, label: str) -> bool:
        """Whether ``label`` has definitions or records."""
        return label in self._definitions.labels() or bool(
            self.client.records(label=label)
        )

    def headers(self) -> dict[str, Header]:
        """The header of each batch, by label, sorted by label."""
        return dict(sorted(self._headers.items()))

    def batch(self, label: str) -> Batch:
        """
        The batch of ``label``, made from its definitions at the first call
        and scanned once if it has a source. If the scan raises, the batch is
        not kept, and the next call makes it again. Ask for the batch each
        time it is used: one handed out before must not be used after
        :meth:`remove`.

        Raises ``KeyError`` if no batch has the label; ``ValueError`` if its
        source is not registered, if the workspace binds no spec of its name,
        or one that lacks a column, if its settings or edits cannot be read,
        or if its source fills a parameter that is not a column of the batch.
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        header = self._headers[label]
        source = self._source(header.source)
        defaults = self._defaults.get(header.spec, Defaults())
        batch = Batch(
            self.client,
            self._bound(header.spec, header.columns),
            columns=header.columns,
            label=label,
            source=source,
            staged=defaults.staged,
            persist=True,
            settings={
                n: v for n, v in defaults.settings.items() if n not in header.columns
            },
            definitions=self._definitions,
            scope=header.scope,
        )
        if batch.source is not None:
            batch.scan()
        self._batches[label] = batch
        return batch

    def change(
        self,
        label: str,
        *,
        title: str | None = None,
        description: str | None = None,
        scope: Scope | None = None,
    ) -> Header:
        """
        The header of ``label`` with what is given changed, as kept. A scope
        changes the rows of the batch at once, without a scan; its ends are
        converted as :meth:`create` converts them. Raises ``KeyError`` if no
        batch has the label; ``ValueError`` for an empty title, and, if a
        scope is given, if the source of the batch is not registered, or for
        an end of the scope as :meth:`create` refuses it.
        """
        header = self._headers[label]
        if scope is not None:
            scope = _scope_of(self._source(header.source), scope)
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

    def start_next(self, label: str, index: Index, *, title: str) -> str:
        """
        End the batch of ``label`` before ``index``, and make the next batch,
        called ``title``, from ``index`` up to the stop of the batch, with the
        same spec, source, columns and settings; its label. The cells set by
        hand and the removals of the rows that move go with them; the rows
        added by hand stay, and so do the results, under ``label``.

        It raises what :meth:`batch` and :meth:`create` raise, and
        ``ValueError`` if ``index`` is below the start or above the stop of
        the scope of the batch, or if the batch holds no row of the scan
        before ``index``, which would leave it none; with the batch of
        ``label`` as it was. The next batch is made first; if its scan
        raises, or it cannot take what was set by hand, it is removed again
        (:meth:`remove`) and the error raised, since a batch that cannot be
        shown cannot be removed on a page.
        """
        batch = self.batch(label)
        header = self._headers[label]
        ended = _scope_of(batch.source, Scope(start=header.scope.start, stop=index))
        if not any(ended.holds(row) for row in batch.rows.scan):
            raise ValueError(
                f'the batch holds no row before {index}, so it would then hold none'
            )
        scope = _scope_of(batch.source, Scope(start=index, stop=header.scope.stop))
        rows = batch.rows
        moving = {row.key for row in rows.scan if scope.holds(row)}
        made = self.create(
            header.spec,
            title=title,
            source=header.source,
            columns=header.columns,
            scope=scope,
            settings=batch.settings,
        )
        try:
            next_batch = self.batch(made)  # scanned
            next_batch.rows = replace(
                next_batch.rows,
                edits={k: v for k, v in rows.edits.items() if k in moving},
                removed=rows.removed & moving,
            )
        except Exception:
            self.remove(made)
            raise
        self.change(label, scope=ended)
        return made

    def remove(self, label: str) -> None:
        """
        Forget the batch of ``label`` and remove its definitions; its records
        stay, and those running keep running. A ``Batch`` of the label handed
        out before must not be used after: it would write its definitions
        again. Raises ``KeyError`` if no batch has the label.

        The header is deleted first, so a batch is never listed half removed.
        """
        if label not in self._headers:
            raise KeyError(label)
        self._definitions.delete(header_key(label))
        del self._headers[label]
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        self._definitions.delete(edits_key(label))
        self._definitions.delete(settings_key(label))

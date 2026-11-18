# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace: :class:`BatchManager` makes, lists, changes and
removes them. The guide is the section "The batches of a workspace" of
``packages/essapps/docs/batch.md``; the decision is
``docs/developer/adr/0011-a-person-makes-the-batches-of-a-workspace.md``.

A batch is kept as three tables of the definitions (:mod:`.definition`)::

    header      title, description, and the name of its registered source
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
from dataclasses import dataclass, field, replace
from typing import Any

from ess.spec import WorkflowSpec

from ..client import Client
from ..files import Selector
from ..records import Template
from .definition import (
    DefinitionStore,
    Header,
    edits_key,
    header_data,
    header_key,
    header_of,
    settings_data,
    settings_key,
    template_of,
)
from .reduction import Batch, RowSource


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
    """

    rows: RowSource
    columns: tuple[str, ...]
    selector: Selector | None = None


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

    The headers are read when the manager is made. Each change is written to
    the definitions first, and one that they refuse raises
    :class:`~.definition.NotKept`, so the manager lists the batches whose
    header the definitions keep.

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
        settings: Mapping[str, Any] | None = None,
        label: str | None = None,
    ) -> str:
        """
        Make a batch and keep it; its label.

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
        settings:
            The settings to start with, in place of the defaults of the spec.
            Those that are columns are left out.
        label:
            The label of the batch; the time of making, ``YYYYMMDD-HHMMSS``,
            if not given.

        Raises
        ------
        KeyError
            If the workspace binds no spec ``spec``, or no source ``source``
            is registered.
        ValueError
            If the label is not one that :func:`check_label` takes, has
            definitions or records, or a column is not a parameter of the spec.
        """
        label = time.strftime('%Y%m%d-%H%M%S') if label is None else label
        check_label(label)
        if label in self._definitions.labels() or self._client.records(label=label):
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
        header = Header(title, description, source)
        self._definitions.save(header_key(label), header_data(header))
        self._headers[label] = header
        return label

    def batches(self) -> dict[str, Header]:
        """The header of each batch, by label, sorted by label."""
        return dict(sorted(self._headers.items()))

    def batch(self, label: str) -> Batch:
        """
        The batch of ``label``, made from its definitions at the first call
        and scanned once if it has a source. If the scan raises, the batch is
        not kept, and the next call makes it again. Raises ``KeyError`` if no
        batch has the label, or the workspace binds no spec of its name, or
        its source is not registered; ``ValueError`` if its definitions
        cannot be read.
        """
        if (batch := self._batches.get(label)) is not None:
            return batch
        source = self.source(label)
        template = self._definitions.load(settings_key(label), template_of)
        if template is None:
            raise ValueError(f'{label}: the definitions hold no settings')
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
        return None if name is None else self.sources[name]

    def edit(
        self, label: str, *, title: str | None = None, description: str | None = None
    ) -> Header:
        """The header of ``label`` with what is given changed, as kept."""
        header = self._headers[label]
        header = replace(
            header,
            title=header.title if title is None else title,
            description=header.description if description is None else description,
        )
        self._definitions.save(header_key(label), header_data(header))
        self._headers[label] = header
        return header

    def remove(self, label: str) -> None:
        """
        Forget the batch of ``label`` and remove its definitions; its records
        stay, and those running keep running. Raises ``KeyError`` if no batch
        has the label.
        """
        if label not in self._headers:
            raise KeyError(label)
        self._definitions.delete(header_key(label))
        del self._headers[label]
        if (batch := self._batches.pop(label, None)) is not None:
            batch.close()
        self._definitions.delete(edits_key(label))
        self._definitions.delete(settings_key(label))

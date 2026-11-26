# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Results of a batch into the catalogue: :class:`Publisher` publishes the result of each
row as one derived dataset (:func:`ess.apps.scicat.publish`), with the files that
:class:`~.writing.Writing` writes by the output options of the batch. It needs the extra
``scicat``.

A batch keeps one entry of the definitions for this, ``publish``, which only
the publisher writes (:class:`~.definition.Publishing`): each upload::

    {'published': [{'key': '60339', 'record': '7f3a...', 'pid': 'pid:...'}]}

A result is published if its row reads ``done`` or ``settings changed``: completed,
and not outdated (:func:`~.state.publishable`). A record is published at
most once; a newer result of the row is published as a new dataset. Each
upload is kept as soon as it succeeds, so that one that fails after others
keeps them. Removing the batch
removes the entry, not the datasets in the catalogue.

Publishing runs in the caller's thread, uploads included: a page holds its
lock until the last upload ends.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from ess.apps.errors import error_text
from ess.apps.records import Record
from ess.apps.scicat import Catalogue, publish
from ess.apps.writers import DatasetMetadata

from .definition import PUBLISH, DefinitionWriteError, Published, Publishing, published
from .export import RowWriter
from .state import publishable
from .writing import Writing


class UploadFailed(RuntimeError):
    """An upload failed; the rows published before it stay published."""


class Publisher:
    """
    Publishes the results of the batches of ``writing`` to ``catalogue``.

    Every refusal of what is asked raises ``ValueError``, and a label that no
    batch has ``KeyError``, as the manager does.
    """

    def __init__(self, writing: Writing, catalogue: Catalogue) -> None:
        self.writing = writing
        self.catalogue = catalogue

    def publish(self, label: str, keys: Sequence[str]) -> dict[str, str]:
        """
        Publish the result of the row of each of ``keys`` as the output options
        of ``label`` say, and return the catalogue identifier of each, by key.

        Raises ``ValueError``, before any upload, if the output options name no
        owner or no contact email, or a format that this process lacks, or a row is
        not in the batch, has no result to publish, or one published already.
        If an upload fails, raises :class:`UploadFailed` that names the rows
        published before it, which are kept.
        """
        write, metadata = self.writing.by_options(label)
        options = self.writing.options(label)
        records = self._results(label, keys)
        if missing := options.lacking:
            raise ValueError(f'the output options name no {", no ".join(missing)}')
        pids: dict[str, str] = {}
        for key, record in records.items():
            try:
                pids[key] = self._publish(
                    label, key, record, write, metadata[key], options.format
                )
            except DefinitionWriteError:  # noqa: PERF203
                raise
            except Exception as error:  # the catalogue's client may raise anything
                done = ', '.join(f'{k} as {p}' for k, p in pids.items())
                before = f'published {done}; then ' if pids else ''
                raise UploadFailed(
                    f'{before}{key} failed: {error_text(error)}'
                ) from error
        return pids

    def _results(self, label: str, keys: Sequence[str]) -> dict[str, Record]:
        """
        The result of the row of each key, which may be published and is not;
        else ``ValueError``.
        """
        batch = self.writing.manager.batch(label)
        if unknown := sorted(set(keys) - set(batch.table().index)):
            raise ValueError(f'the batch has no row {", ".join(unknown)}')
        states, _ = batch.shown(list(keys))
        may = set(publishable(states))
        if lacking := [k for k in keys if k not in may]:
            raise ValueError(
                f'no result to publish, done and not outdated: {", ".join(lacking)}'
            )
        records = {k: s.record for k, s in states.items() if s.record is not None}
        done = published(self.writing.manager.definitions, label)
        if again := [k for k, r in records.items() if r.id in done]:
            raise ValueError(f'the result is published already: {", ".join(again)}')
        return records

    def _publish(
        self,
        label: str,
        key: str,
        record: Record,
        write: RowWriter,
        metadata: DatasetMetadata,
        format: str,
    ) -> str:
        """Upload one result and keep that it was; its catalogue identifier."""
        client = self.writing.manager.client
        with tempfile.TemporaryDirectory() as folder:
            written = Path(folder) / key
            write(key, record, written)
            files = sorted(written.iterdir())
            pid = publish(self.catalogue, client, record, files, metadata, format)
        publishing = self._load_publishing(label)
        upload = Published(key=key, record=record.id, pid=pid)
        try:
            self._save_publishing(
                label,
                publishing.model_copy(
                    update={'published': (*publishing.published, upload)}
                ),
            )
        except DefinitionWriteError as error:
            raise DefinitionWriteError(
                f'{key} was published as {pid}, but that is not kept: {error}'
            ) from error
        return pid

    def _load_publishing(self, label: str) -> Publishing:
        kept = PUBLISH.load(self.writing.manager.definitions, label)
        return Publishing() if kept is None else kept

    def _save_publishing(self, label: str, publishing: Publishing) -> None:
        PUBLISH.save(self.writing.manager.definitions, label, publishing)

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Publishing the results of a batch to the catalogue, one derived dataset per
row (:func:`ess.apps.scicat.publish`). Needs the extra ``scicat``.

The definitions of a batch store each upload. Removing the batch removes them
and leaves the datasets in the catalogue.

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
    """An upload failed. The rows published before it stay published."""


class Publisher:
    """
    Publishes the results of the batches of ``writing`` to ``catalogue``, with
    the files that ``writing`` writes.
    """

    def __init__(self, writing: Writing, catalogue: Catalogue) -> None:
        self.writing = writing
        self.catalogue = catalogue

    def publish(self, label: str, keys: Sequence[str]) -> dict[str, str]:
        """
        Publishes the result of the row of each of ``keys``, by the output
        options of ``label``. Returns the catalogue identifier of each, by key.

        A record is published at most once. A newer result of a row is
        published as a new dataset.

        Raises
        ------
        KeyError
            If no batch has ``label``.
        ValueError
            Before any upload, if the output options cannot be used, or a row
            has no result to publish (:func:`~.state.publishable`) or was
            published already.
        UploadFailed
            If an upload fails. It names the rows published before it, which
            stay published.
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
        The result of the row of each key; ``ValueError`` if one is not
        publishable or is published already.
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
        """Uploads one result, stores the upload, and returns its identifier."""
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
                f'{key} was published as {pid}, but the definitions could not '
                f'record it: {error}'
            ) from error
        return pid

    def _load_publishing(self, label: str) -> Publishing:
        stored = PUBLISH.load(self.writing.manager.definitions, label)
        return Publishing() if stored is None else stored

    def _save_publishing(self, label: str, publishing: Publishing) -> None:
        PUBLISH.save(self.writing.manager.definitions, label, publishing)

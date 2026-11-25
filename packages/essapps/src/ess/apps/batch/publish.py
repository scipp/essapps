# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Results of a batch into the catalogue: :class:`Publisher` publishes the result
of each row as one derived dataset (:func:`ess.apps.scicat.publish`), with the
files that :class:`~.writing.Writing` writes by the form of the batch. It needs
the extra ``scicat``.

A batch keeps one entry of the definitions for this, ``publish``, which only
the publisher writes (:class:`~.definition.Publishing`): each upload::

    {'published': [{'key': '60339', 'record': '7f3a...', 'pid': 'pid:...'}]}

A result is published if its row reads ``done`` or ``earlier``: completed,
and not outdated. A record is published at most once; a newer result of the
row is published as a new dataset. Each upload is kept as soon as it
succeeds, so that one that fails after others keeps them. Removing the batch
removes the entry, not the datasets in the catalogue.

Publishing runs in the caller's thread, uploads included: a page holds its
lock until the last upload ends.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

from ..errors import said
from ..records import Record
from ..scicat import Catalogue, publish
from ..writers import About
from .definition import NotKept, Published, Publishing, publish_key, publishing_data
from .export import RowWriter
from .writing import Writing


class UploadFailed(RuntimeError):
    """An upload failed; the rows published before it stay published."""


PUBLISHABLE = ('done', 'earlier')
"""What the row of a result that may be published reads (``RowState.text``)."""


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
        Publish the result of the row of each of ``keys`` as the form of
        ``label`` says, and return the catalogue identifier of each, by key.

        Raises ``ValueError``, before any upload, if the form names no owner
        or no contact email, or a format that this process lacks, or a row is
        not in the batch, has no result to publish, or one published already.
        If an upload fails, raises :class:`UploadFailed` that names the rows
        published before it, which are kept.
        """
        write, abouts = self.writing.by_form(label)
        format = self.writing.form(label).format
        records = self._results(label, keys)
        for about in (abouts[key] for key in records):
            given = [('owner', about.owner), ('contact email', about.contact_email)]
            if missing := [what for what, text in given if not text]:
                raise ValueError(f'the form names no {", no ".join(missing)}')
        pids: dict[str, str] = {}
        for key, record in records.items():
            try:
                pids[key] = self._publish(
                    label, key, record, write, abouts[key], format
                )
            except NotKept:  # noqa: PERF203
                raise
            except Exception as error:  # the catalogue's client may raise anything
                done = ', '.join(f'{k} as {p}' for k, p in pids.items())
                before = f'published {done}; then ' if pids else ''
                raise UploadFailed(f'{before}{key} failed: {said(error)}') from error
        return pids

    def _results(self, label: str, keys: Sequence[str]) -> dict[str, Record]:
        """
        The result of the row of each key, which may be published and is not;
        else ``ValueError``.
        """
        batch = self.writing.manager.batch(label)
        if unknown := sorted(set(keys) - set(batch.frame().index)):
            raise ValueError(f'the batch has no row {", ".join(unknown)}')
        states, _ = batch.shown(list(keys))
        records = {
            k: s.record
            for k, s in states.items()
            if s.text in PUBLISHABLE and s.record is not None
        }
        if lacking := [k for k in keys if k not in records]:
            raise ValueError(
                f'no result to publish, done and not outdated: {", ".join(lacking)}'
            )
        done = batch.published()
        if again := [k for k, r in records.items() if r.id in done]:
            raise ValueError(f'the result is published already: {", ".join(again)}')
        return records

    def _publish(
        self,
        label: str,
        key: str,
        record: Record,
        write: RowWriter,
        about: About,
        format: str,
    ) -> str:
        """Upload one result and keep that it was; its catalogue identifier."""
        client = self.writing.manager.client
        with tempfile.TemporaryDirectory() as folder:
            written = Path(folder) / key
            write(key, record, written)
            files = sorted(written.iterdir())
            pid = publish(self.catalogue, client, record, files, about, format)
        kept = self._kept(label)
        upload = Published(key=key, record=record.id, pid=pid)
        try:
            self._keep(
                label, kept.model_copy(update={'published': (*kept.published, upload)})
            )
        except NotKept as error:
            raise NotKept(
                f'{key} was published as {pid}, but that is not kept: {error}'
            ) from error
        return pid

    def _kept(self, label: str) -> Publishing:
        return self.writing.manager.batch(label).publishing()

    def _keep(self, label: str, publishing: Publishing) -> None:
        self.writing.manager.definitions.save(
            publish_key(label), publishing_data(publishing)
        )

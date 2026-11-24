# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Results of a batch into the catalogue: :class:`Publisher` publishes the result
of each row as one derived dataset (:func:`ess.apps.scicat.publish`). It needs
the extra ``scicat``.

A batch keeps one entry of the definitions for this, ``publish``, which only
the publisher writes (:class:`~.definition.Publishing`): the form that a
person fills once for the batch, and each upload::

    {'form': {'outputs': ['iofq'], 'name': '{title} {name}', ...},
     'published': [{'key': '60339', 'record': '7f3a...', 'pid': 'pid:...'}]}

A record is published at most once; a newer result of the row is published as
a new dataset. Each upload is kept as soon as it succeeds, so that one that
fails after others keeps them. Removing the batch removes the entry, not the
datasets in the catalogue.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path

from ..records import Record, Status
from ..scicat import About, Catalogue, Person, principal_investigator, publish
from .definition import (
    Form,
    Published,
    Publishing,
    publish_key,
    publishing_data,
    publishing_of,
)
from .export import RECORD_FILE, write_outputs
from .manager import BatchManager

_NAME_FIELDS = {'title': '', 'name': '', 'key': ''}


class Publisher:
    """
    Publishes the results of the batches of ``manager`` to ``catalogue``.

    Every refusal of what is asked raises ``ValueError``, and a label that no
    batch has ``KeyError``, as the manager does.
    """

    def __init__(self, manager: BatchManager, catalogue: Catalogue) -> None:
        self.manager = manager
        self.catalogue = catalogue

    def form(self, label: str) -> Form:
        """
        The form of ``label``. Until set, its description is the batch's, and
        its owner and contact email are those of the principal investigator
        of the proposal, as for a raw dataset; empty if the catalogue cannot
        say who that is.
        """
        form = self._kept(label).form
        if form is not None:
            return form
        pi = self._principal_investigator
        return Form(
            description=self.manager.headers()[label].description,
            owner='' if pi is None else pi.name,
            contact_email='' if pi is None else pi.email,
        )

    @cached_property
    def _principal_investigator(self) -> Person | None:
        """Asked of the catalogue once: a page reads the form at each poll."""
        return principal_investigator(self.catalogue, self.manager.client.proposal)

    def set_form(self, label: str, form: Form) -> None:
        """
        Keep ``form`` for ``label``. Raises ``ValueError`` for an output that
        the spec of the batch lacks, or a name with a field in braces other
        than ``title``, ``name`` and ``key``.
        """
        spec = self.manager.batch(label).spec
        if unknown := sorted(set(form.outputs) - spec.outputs.model_fields.keys()):
            raise ValueError(f'{spec.name} has no output {", ".join(unknown)}')
        try:
            form.name.format(**_NAME_FIELDS)
        except (KeyError, IndexError, ValueError):
            raise ValueError(
                f'{form.name!r}: a name may hold {{title}}, {{name}} and {{key}}, '
                'and no other field in braces'
            ) from None
        self._keep(label, self._kept(label).model_copy(update={'form': form}))

    def published(self, label: str) -> dict[str, str]:
        """The catalogue identifier of each published record of ``label``, by ID."""
        return {p.record: p.pid for p in self._kept(label).published}

    def publish(self, label: str, rows: Mapping[str, str]) -> dict[str, str]:
        """
        Publish the result of each row of ``rows``, a key with the name of the
        row, as the form of ``label`` says, and return the catalogue identifier
        of each, by key.

        Raises ``ValueError``, before any upload, if the form names no output,
        no owner or no contact email, or a row has no completed result or one
        published already. Raises what the catalogue raises, after keeping the
        uploads that succeeded before.
        """
        form = self.form(label)
        if missing := [
            what
            for what, given in [
                ('output', form.outputs),
                ('owner', form.owner),
                ('contact email', form.contact_email),
            ]
            if not given
        ]:
            raise ValueError(f'the form names no {", no ".join(missing)}')
        records = self._results(label, rows)
        title = self.manager.headers()[label].title
        return {
            key: self._publish(label, key, record, form, title, rows[key])
            for key, record in records.items()
        }

    def _results(self, label: str, rows: Mapping[str, str]) -> dict[str, Record]:
        """The completed result of each row, not published; else ``ValueError``."""
        states = self.manager.batch(label).state(list(rows), compare=False)
        if unknown := sorted(rows.keys() - states.keys()):
            raise ValueError(f'the batch has no row {", ".join(unknown)}')
        records = {
            k: s.record
            for k, s in states.items()
            if s.status is Status.COMPLETED and s.record is not None
        }
        if lacking := sorted(states.keys() - records.keys()):
            raise ValueError(f'no completed result: {", ".join(lacking)}')
        done = self.published(label)
        if again := sorted(k for k, r in records.items() if r.id in done):
            raise ValueError(f'the result is published already: {", ".join(again)}')
        return records

    def _publish(
        self, label: str, key: str, record: Record, form: Form, title: str, name: str
    ) -> str:
        """Upload one result and keep that it was; its catalogue identifier."""
        client = self.manager.client
        about = About(
            name=form.name.format(title=title, name=name, key=key),
            owner=form.owner,
            contact_email=form.contact_email,
            description=form.description,
            keywords=form.keywords,
        )
        with tempfile.TemporaryDirectory() as folder:
            written = write_outputs(client, record, Path(folder), form.outputs)
            files = [*written.values(), Path(folder) / RECORD_FILE]
            pid = publish(self.catalogue, client, record, files, about)
        kept = self._kept(label)
        upload = Published(key=key, record=record.id, pid=pid)
        self._keep(
            label, kept.model_copy(update={'published': (*kept.published, upload)})
        )
        return pid

    def _kept(self, label: str) -> Publishing:
        self.manager.batch(label)  # KeyError for a label without a batch
        kept = self.manager.definitions.load(publish_key(label), publishing_of)
        return Publishing() if kept is None else kept

    def _keep(self, label: str, publishing: Publishing) -> None:
        self.manager.definitions.save(publish_key(label), publishing_data(publishing))

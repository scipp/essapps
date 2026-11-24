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

A result is published if its row reads ``done`` or ``earlier``: completed,
and not outdated. A record is published at most once; a newer result of the
row is published as a new dataset. Each upload is kept as soon as it
succeeds, so that one that fails after others keeps them. Removing the batch
removes the entry, not the datasets in the catalogue.

Publishing runs in the caller's thread, uploads included: a page holds its
lock until the last upload ends.

The command line gives the form as a YAML file of its fields, which
:meth:`Publisher.form_text` writes and :func:`load_form` reads.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..errors import said
from ..records import Record
from ..scicat import About, Catalogue, principal_investigator, publish
from .definition import (
    Form,
    NotKept,
    Published,
    Publishing,
    publish_key,
    publishing_data,
)
from .export import RECORD_FILE, write_outputs
from .manager import BatchManager
from .table import names


class UploadFailed(RuntimeError):
    """An upload failed; the rows published before it stay published."""


PUBLISHABLE = ('done', 'earlier')
"""What the row of a result that may be published reads (``RowState.text``)."""


class Publisher:
    """
    Publishes the results of the batches of ``manager`` to ``catalogue``.

    Every refusal of what is asked raises ``ValueError``, and a label that no
    batch has ``KeyError``, as the manager does. The principal investigator
    of the proposal, the default owner and contact, is asked of the catalogue
    once, here, so that reading a form asks nothing of it.
    """

    def __init__(self, manager: BatchManager, catalogue: Catalogue) -> None:
        self.manager = manager
        self.catalogue = catalogue
        self.principal_investigator = principal_investigator(
            catalogue, manager.client.proposal
        )

    def form(self, label: str) -> Form:
        """
        The form of ``label``. Until set, its description is the batch's, and
        its owner and contact email are those of the principal investigator
        of the proposal, as for a raw dataset; empty if the catalogue could
        not say who that is.
        """
        form = self._kept(label).form
        if form is not None:
            return form
        pi = self.principal_investigator
        return Form(
            description=self.manager.headers()[label].description,
            owner='' if pi is None else pi.name,
            contact_email='' if pi is None else pi.email,
        )

    def form_text(self, label: str) -> str:
        """
        The form of ``label`` as YAML (:meth:`form`), with a comment on what
        its outputs and its name may hold; :func:`load_form` reads it back.
        """
        outputs = ', '.join(self.manager.batch(label).spec.outputs.model_fields)
        fields = self.form(label).model_dump(mode='json')
        return (
            f'# The datasets that the results of {label} are published as.\n'
            f'# outputs: some of {outputs}\n'
            '# name: may hold {title}, {name} and {key}: the title of the batch,\n'
            '#   the name of the row, and its key\n'
            + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
        )

    def set_form(self, label: str, form: Form) -> None:
        """
        Keep ``form`` for ``label``. Raises ``ValueError`` for an output that
        the spec of the batch lacks.
        """
        spec = self.manager.batch(label).spec
        if unknown := sorted(set(form.outputs) - spec.outputs.model_fields.keys()):
            raise ValueError(f'{spec.name} has no output {", ".join(unknown)}')
        self._keep(label, self._kept(label).model_copy(update={'form': form}))

    def publish(self, label: str, keys: Sequence[str]) -> dict[str, str]:
        """
        Publish the result of the row of each of ``keys`` as the form of
        ``label`` says, and return the catalogue identifier of each, by key.

        Raises ``ValueError``, before any upload, if the form names no output,
        no owner or no contact email, or a row is not in the batch, has no
        result to publish, or one published already. If an upload fails,
        raises :class:`UploadFailed` that names the rows published before it,
        which are kept.
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
        records = self._results(label, keys)
        frame = self.manager.batch(label).frame()
        named = names(frame, self.manager.batch(label).parameter_columns)
        title = self.manager.headers()[label].title
        pids: dict[str, str] = {}
        for key, record in records.items():
            about = About(
                name=form.name.format(title=title, name=named[key], key=key),
                owner=form.owner,
                contact_email=form.contact_email,
                description=form.description,
                keywords=form.keywords,
            )
            try:
                pids[key] = self._publish(label, key, record, form.outputs, about)
            except NotKept:
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
        batch = self.manager.batch(label)
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
        self, label: str, key: str, record: Record, outputs: Sequence[str], about: About
    ) -> str:
        """Upload one result and keep that it was; its catalogue identifier."""
        client = self.manager.client
        with tempfile.TemporaryDirectory() as folder:
            written = write_outputs(client, record, Path(folder), outputs)
            files = [*written.values(), Path(folder) / RECORD_FILE]
            pid = publish(self.catalogue, client, record, files, about)
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
        return self.manager.batch(label).publishing()

    def _keep(self, label: str, publishing: Publishing) -> None:
        self.manager.definitions.save(publish_key(label), publishing_data(publishing))


def load_form(path: Path) -> Form:
    """
    The form in a YAML file, as :meth:`Publisher.form_text` writes it.
    Raises ``ValueError`` if the file is not a form.
    """
    try:
        return Form.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValidationError) as error:
        raise ValueError(f'{path}: {said(error)}') from None

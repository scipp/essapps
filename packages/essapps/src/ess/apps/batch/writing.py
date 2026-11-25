# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
How the results of a batch are written: :class:`Writing` keeps the form of
each batch (:class:`~.definition.Form`), and writes the result of a row by it
with :func:`ess.apps.writers.write`. The download of the page, ``export -l``
and publishing (:mod:`.publish`) all write so, so that they give the same
files.

The form picks a writer by the name of its format, among those the application gives for
the spec of the batch and :data:`~ess.apps.writers.SCIPP_HDF5`, which every
spec has, and names what a person says of the results
(:class:`~ess.apps.writers.About`). A batch keeps its form in the entry
``form`` of the definitions, which only :class:`Writing` writes::

    {'format': 'nxcansas', 'outputs': ['iofq'], 'name': '{title} {name}',
     'description': '', 'keywords': [], 'owner': 'Ada Lovelace',
     'contact_email': 'ada@example.org'}

A form names its format by the writer's name, not its title, so a title may
change, but a name changed is one that a kept form lacks. A kept form whose
format this process lacks, such as a command run without the writers, is
refused when writing, never written in another format.

The command line gives the form as a YAML file of its fields, which
:meth:`Writing.form_text` writes and :func:`load_form` reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from .. import writers
from ..errors import said
from ..records import Record
from ..writers import SCIPP_HDF5, About, Person, Writer
from .definition import Form, form_data, form_key, form_of
from .export import RowWriter
from .manager import BatchManager
from .table import names


class Writing:
    """
    Writes the results of the batches of ``manager`` by the form of each.

    Every refusal of what is asked raises ``ValueError``, and a label that no
    batch has ``KeyError``, as the manager does.

    Parameters
    ----------
    manager:
        The batches.
    writers:
        The writers of each spec, by its name, the first one the format of a
        form not set. :data:`~ess.apps.writers.SCIPP_HDF5` follows them, and
        is the only writer of a spec not named. ``ValueError`` if the
        workspace binds no spec of a name, a writer reads an output that its
        spec lacks, or two writers of a spec have one format.
    contact:
        The owner and contact of a form not set, such as the principal
        investigator of the proposal (:func:`ess.apps.scicat.principal_investigator`);
        none if not given.
    """

    def __init__(
        self,
        manager: BatchManager,
        writers: Mapping[str, Sequence[Writer]],
        contact: Person | None = None,
    ) -> None:
        for name, given in writers.items():
            outputs = manager.spec_named(name).outputs.model_fields
            for writer in given:
                if unknown := sorted(set(writer.outputs or ()) - outputs.keys()):
                    raise ValueError(
                        f'{writer.name} reads {", ".join(unknown)}, '
                        f'which {name} does not output'
                    )
            formats = [w.name for w in (*given, SCIPP_HDF5)]
            if len(set(formats)) < len(formats):
                raise ValueError(f'two writers of {name} have one format')
        self.manager = manager
        self._writers = writers
        self._contact = contact

    def writers(self, label: str) -> list[Writer]:
        """The writers that the form of ``label`` may pick, the default first."""
        spec = self.manager.batch(label).spec
        return [*self._writers.get(spec.name, ()), SCIPP_HDF5]

    def form(self, label: str) -> Form:
        """
        The form of ``label``. Until set, its format is the first of
        :meth:`writers`, it names every output of the spec, its description
        is the batch's, and its owner and contact are the ``contact`` given.
        """
        spec = self.manager.batch(label).spec
        kept = self.manager.definitions.load(form_key(label), form_of)
        if kept is not None:
            return kept
        contact = self._contact
        return Form(
            format=self.writers(label)[0].name,
            outputs=tuple(spec.outputs.model_fields),
            description=self.manager.headers()[label].description,
            owner='' if contact is None else contact.name,
            contact_email='' if contact is None else contact.email,
        )

    def set_form(self, label: str, form: Form) -> None:
        """
        Keep ``form`` for ``label``. Raises ``ValueError`` for a format that
        :meth:`writers` lacks, an output that the spec of the batch lacks, or
        no output for a writer that writes the outputs chosen.
        """
        writer = self._writer(label, form.format)
        spec = self.manager.batch(label).spec
        if unknown := sorted(set(form.outputs) - spec.outputs.model_fields.keys()):
            raise ValueError(f'{spec.name} has no output {", ".join(unknown)}')
        if writer.outputs is None and not form.outputs:
            raise ValueError(f'{writer.title} writes the outputs chosen: choose one')
        self.manager.definitions.save(form_key(label), form_data(form))

    def by_form(self, label: str) -> tuple[RowWriter, dict[str, About]]:
        """
        What writes the result of a row of ``label`` into a folder, as the
        form says (:func:`ess.apps.writers.write`), and what the form says of
        the result of each row, by key, which the publisher also gives the
        catalogue. Raises ``ValueError`` if this process lacks the format of
        the form.
        """
        form = self.form(label)
        writer = self._writer(label, form.format)
        abouts = self._abouts(label, form)
        client = self.manager.client

        def write(key: str, record: Record, directory: Path) -> None:
            writers.write(client, record, writer, abouts[key], directory, form.outputs)

        return write, abouts

    def form_text(self, label: str) -> str:
        """
        The form of ``label`` as YAML (:meth:`form`), with a comment on what
        its format, its outputs and its name may hold; :func:`load_form` reads
        it back.
        """
        formats = ', '.join(w.name for w in self.writers(label))
        outputs = ', '.join(self.manager.batch(label).spec.outputs.model_fields)
        fields = self.form(label).model_dump(mode='json')
        return (
            f'# How the results of {label} are written, and what is said of them.\n'
            f'# format: one of {formats}\n'
            f'# outputs: some of {outputs}, for a format that writes those chosen\n'
            '# name: may hold {title}, {name} and {key}: the title of the batch,\n'
            '#   the name of the row, and its key\n'
            + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
        )

    def _abouts(self, label: str, form: Form) -> dict[str, About]:
        """What ``form`` says of the result of each row of ``label``, by key."""
        batch = self.manager.batch(label)
        title = self.manager.headers()[label].title
        return {
            key: About(
                name=form.name.format(title=title, name=name, key=key),
                owner=form.owner,
                contact_email=form.contact_email,
                description=form.description,
                keywords=form.keywords,
            )
            for key, name in names(batch.frame(), batch.parameter_columns).items()
        }

    def _writer(self, label: str, format: str) -> Writer:
        """The writer named ``format`` for ``label``; ``ValueError`` if none."""
        formats = self.writers(label)
        for writer in formats:
            if writer.name == format:
                return writer
        known = ', '.join(w.name for w in formats)
        raise ValueError(f'{label} has no writer of the format {format}, only {known}')


def load_form(path: Path) -> Form:
    """
    The form in a YAML file, as :meth:`Writing.form_text` writes it.
    Raises ``ValueError`` if the file is not a form.
    """
    try:
        return Form.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValidationError) as error:
        raise ValueError(f'{path}: {said(error)}') from None

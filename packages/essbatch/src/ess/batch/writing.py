# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The output options of each batch, and writing the result of a row by them.

Output options refer to their writer by its name, so a writer's title may
change. Stored output options whose format this process lacks, such as in a
command run without the application's writers, raise ``ValueError`` when
writing. Nothing is written in another format then.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from ess.apps import writers
from ess.apps.errors import error_text
from ess.apps.records import Record
from ess.apps.writers import SCIPP_HDF5, DatasetMetadata, Person, Writer

from .definition import OUTPUT_OPTIONS, OutputOptions
from .export import RowWriter
from .manager import BatchManager
from .table import row_names


class Writing:
    """
    Writes the results of the batches of ``manager`` by the output options of each.

    A method raises ``KeyError`` for a label that no batch has.

    Parameters
    ----------
    writers:
        The writers of each spec, by spec name. The first is the default
        format. :data:`~ess.apps.writers.SCIPP_HDF5` follows them, and is the
        only writer of a spec not listed.
    contact:
        The default owner and contact, such as the principal investigator of
        the proposal (:func:`ess.apps.scicat.principal_investigator`).

    Raises ``ValueError`` if ``writers`` do not fit the specs that the
    workspace binds.
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
        """The writers that the output options of ``label`` may pick, default first."""
        spec = self.manager.batch(label).spec
        return [*self._writers.get(spec.name, ()), SCIPP_HDF5]

    def options(self, label: str) -> OutputOptions:
        """
        The output options of ``label``.

        Until set: the first format of :meth:`writers`, every output of the
        spec, the description of the batch, and ``contact`` as owner and
        contact.
        """
        spec = self.manager.batch(label).spec
        stored = OUTPUT_OPTIONS.load(self.manager.definitions, label)
        if stored is not None:
            return stored
        contact = self._contact
        return OutputOptions(
            format=self.writers(label)[0].name,
            outputs=tuple(spec.outputs.model_fields),
            description=self.manager.headers()[label].description,
            owner='' if contact is None else contact.name,
            contact_email='' if contact is None else contact.email,
        )

    def set_options(self, label: str, options: OutputOptions) -> None:
        """
        Stores ``options`` for ``label``.

        Raises ``ValueError`` if ``options`` do not fit the writers or the spec.
        """
        writer = self._writer(label, options.format)
        spec = self.manager.batch(label).spec
        if unknown := sorted(set(options.outputs) - spec.outputs.model_fields.keys()):
            raise ValueError(f'{spec.name} has no output {", ".join(unknown)}')
        if writer.outputs is None and not options.outputs:
            raise ValueError(f'{writer.title} writes the outputs chosen: choose one')
        OUTPUT_OPTIONS.save(self.manager.definitions, label, options)

    def by_options(self, label: str) -> tuple[RowWriter, dict[str, DatasetMetadata]]:
        """
        A :data:`~.export.RowWriter` by the output options of ``label``, and
        the dataset metadata of each row, by key.

        Raises ``ValueError`` if this process lacks the format of the output
        options.
        """
        options = self.options(label)
        writer = self._writer(label, options.format)
        metadata = self._metadata(label, options)
        client = self.manager.client

        def write(key: str, record: Record, directory: Path) -> None:
            writers.write(
                client, record, writer, metadata[key], directory, options.outputs
            )

        return write, metadata

    def options_text(self, label: str) -> str:
        """
        The output options of ``label`` as YAML, with a comment on what their
        format, outputs and name may hold. :func:`load_options` reads it back.
        """
        formats = ', '.join(w.name for w in self.writers(label))
        outputs = ', '.join(self.manager.batch(label).spec.outputs.model_fields)
        fields = self.options(label).model_dump(mode='json')
        return (
            f'# How the results of {label} are written, and what is said of them.\n'
            f'# format: one of {formats}\n'
            f'# outputs: some of {outputs}, for a format that writes those chosen\n'
            '# name: may hold {title}, {name} and {key}: the title of the batch,\n'
            '#   the name of the row, and its key\n'
            + yaml.safe_dump(fields, sort_keys=False, allow_unicode=True)
        )

    def _metadata(
        self, label: str, options: OutputOptions
    ) -> dict[str, DatasetMetadata]:
        """The dataset metadata of each row of ``label`` by ``options``, by key."""
        batch = self.manager.batch(label)
        title = self.manager.headers()[label].title
        return {
            key: DatasetMetadata(
                name=options.name.format(title=title, name=name, key=key),
                owner=options.owner,
                contact_email=options.contact_email,
                description=options.description,
                keywords=options.keywords,
            )
            for key, name in row_names(batch.table(), batch.parameter_columns).items()
        }

    def _writer(self, label: str, format: str) -> Writer:
        """The writer named ``format`` for ``label``; ``ValueError`` if none."""
        formats = self.writers(label)
        for writer in formats:
            if writer.name == format:
                return writer
        known = ', '.join(w.name for w in formats)
        raise ValueError(f'{label} has no writer of the format {format}, only {known}')


def load_options(path: Path) -> OutputOptions:
    """
    The output options in a YAML file, as :meth:`Writing.options_text` writes
    them. Raises ``ValueError`` if the file holds no output options.
    """
    try:
        return OutputOptions.model_validate(yaml.safe_load(path.read_text()))
    except (yaml.YAMLError, ValidationError) as error:
        raise ValueError(f'{path}: {error_text(error)}') from None

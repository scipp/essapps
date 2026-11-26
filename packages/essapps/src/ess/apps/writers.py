# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Writers of the outputs of a record to files. It needs scipp and h5py.

A :class:`Writer` writes the files of one format, such as NXcanSAS, from the
outputs of one completed record and what a person says of them,
:class:`DatasetMetadata`. :func:`write` writes a record into a new folder with a writer,
and next to the writer's files the record itself, ``_record.json``, which names
the values the outputs were computed from::

    iofq.h5            the files of the format
    _record.json       the record, the same for every format

The writers of a technique call the save functions of its workflow package.
The application provides them as a mapping of spec name to writers, as it
provides bindings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import hdf5
from .client import Client
from .records import Record

RECORD_FILE = '_record.json'
"""The file of the record in the folder of a record; no output is named so."""


class DatasetMetadata(BaseModel, frozen=True):
    """
    What a person says of the data of one record. A format such as ORSO holds
    it in the file, and the catalogue holds it with a dataset.
    """

    name: str
    owner: str = ''
    contact_email: str = ''
    description: str = ''
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    """A person as the catalogue names one: a name and an email."""

    name: str
    email: str


@dataclass(frozen=True)
class Writer:
    """
    Writes the files of one format.

    Parameters
    ----------
    name:
        The name of the format, kept in a form, such as ``'nxcansas'``. A form
        names its writer by it, so a title may change and a kept form still
        finds its writer.
    title:
        What the format is called, shown to a person, such as ``'NXcanSAS'``.
    description:
        What files the writer writes, shown to a person.
    outputs:
        The outputs of the spec that the writer reads, or ``None`` if a person
        picks them.
    write:
        Writes the files from the outputs, by name, and what a person says of
        them, directly into a folder, with no folder inside it: the publisher
        uploads the files of the folder.
    """

    name: str
    title: str
    description: str
    outputs: tuple[str, ...] | None
    write: Callable[[Mapping[str, Any], DatasetMetadata, Path], None]


def _write_hdf5(
    outputs: Mapping[str, Any], metadata: DatasetMetadata, directory: Path
) -> None:
    for name, value in outputs.items():
        hdf5.write(value, directory / f'{name}.h5')


SCIPP_HDF5 = Writer(
    name='scipp-hdf5',
    title='scipp HDF5',
    description='Each output as <name>.h5, which scipp.io.load_hdf5 reads.',
    outputs=None,
    write=_write_hdf5,
)


def write(
    client: Client,
    record: Record,
    writer: Writer,
    metadata: DatasetMetadata,
    directory: Path,
    chosen: Sequence[str] = (),
) -> None:
    """
    Write the outputs of a completed ``record`` with ``writer`` into
    ``directory``, a folder that does not exist, and the record as
    ``_record.json``.
    The outputs are those the writer reads, or ``chosen`` if it reads those
    a person picks; then ``ValueError`` if none is chosen.
    """
    names = writer.outputs if writer.outputs is not None else tuple(chosen)
    if not names:
        raise ValueError(f'{writer.title}: no output to write')
    outputs = {name: client.output(record, name) for name in names}
    directory.mkdir(parents=True)
    (directory / RECORD_FILE).write_text(record.model_dump_json(indent=2))
    writer.write(outputs, metadata, directory)

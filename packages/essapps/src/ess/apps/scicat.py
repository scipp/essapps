# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Publish a record to SciCat as a derived dataset. It needs the extra ``scicat``
(scitacean).

:func:`publish` makes one derived dataset of one completed record: its files
are the outputs written beforehand, and the rest is read from the record and
the workspace::

    input_datasets      the catalogue identifiers of the files the record read,
                        also through upstream records (ADR 0010); other files,
                        such as masks, are listed in scientific_metadata
    proposal_ids        the workspace's proposal, which is also the owner group
    instrument_ids      the catalogue's instrument
    used_software       the Python packages installed now (installed_software)
    scientific_metadata the spec, the request and the record's ID

A person gives the rest as :class:`~ess.apps.writers.About`. :class:`Catalogue`
says where to publish: a ``scitacean.Client``, real or
``scitacean.testing.client.FakeClient``, with its file transfer. Whether the
files are copied on a shared file system or sent over SFTP is the choice of
that transfer, not of this module.

The software is that of the process that publishes, not of the one that
reduced, since a record names no software versions: correct while one
install reduces and publishes.
"""

from __future__ import annotations

import importlib.metadata
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

try:
    from scitacean import PID, Dataset, RemotePath
    from scitacean import Client as ScicatClient
except ModuleNotFoundError as error:
    raise ImportError(
        'ess.apps.scicat needs scitacean: install essapps[scicat]'
    ) from error

from .client import Client
from .records import Record
from .writers import About, Person

_PID_PREFIX = 'pid:'


@dataclass(frozen=True, kw_only=True)
class Catalogue:
    """
    Where to publish.

    Parameters
    ----------
    client:
        The SciCat client, with the file transfer that uploads the files.
    instrument:
        The instrument of the workspace, such as ``'loki'``.
    folder:
        The folder on the file server that holds the files of a dataset, as a
        pattern of ``proposal``, ``instrument`` and ``record``. The default is
        that of ESS. A file transfer made with a ``source_folder`` of its own
        puts the files there instead, so that the datasets of one proposal may
        share a folder; give the client a transfer without one.
    """

    client: ScicatClient
    instrument: str
    folder: str = '/ess/data/{proposal}/{instrument}/upload/{record}'


def principal_investigator(catalogue: Catalogue, proposal: str) -> Person | None:
    """
    The principal investigator of ``proposal``, as the catalogue says, whom
    the ESS ingestor makes owner and contact of each raw dataset; ``None`` if
    the catalogue cannot say, such as without a login or for a proposal it
    lacks, or if it cannot be reached; that is logged.
    """
    try:
        found = catalogue.client.get_proposal(proposal)
    except Exception as error:  # httpx raises its own errors, such as unreachable
        logging.getLogger(__name__).warning(
            'The principal investigator of proposal %s is not known: %s',
            proposal,
            error,
        )
        return None
    name = ' '.join(n for n in (found.pi_firstname, found.pi_lastname) if n)
    return Person(name=name, email=found.pi_email or '')


def installed_software() -> list[str]:
    """The installed Python distributions as ``name==version``, sorted."""
    found = {
        d.name.lower(): d.version
        for d in importlib.metadata.distributions()
        if d.name  # a dist-info folder left without its metadata has none
    }
    return [f'{name}=={version}' for name, version in sorted(found.items())]


def publish(
    catalogue: Catalogue,
    client: Client,
    record: Record,
    files: Sequence[Path],
    about: About,
    format: str,
) -> str:
    """
    Upload ``files``, written from ``record`` in ``format``, the name of a
    writer (:class:`ess.apps.writers.Writer`), as a new derived dataset, and
    return its catalogue identifier, as ``pid:<pid>`` like the files a record
    names (ADR 0010). The dataset keeps the format, since files of different
    formats may have the same names, such as ``iofq.h5``.

    Raises what the SciCat client raises, such as ``pydantic.ValidationError``
    for a dataset that lacks a required field.
    """
    provenance = client.provenance(record)
    identities = [f.file for f in provenance.files()]
    inputs = [i.removeprefix(_PID_PREFIX) for i in identities if _is_pid(i)]
    dataset = Dataset(
        type='derived',
        name=about.name,
        description=about.description or None,
        keywords=list(about.keywords),
        owner=about.owner,
        contact_email=about.contact_email,
        owner_group=record.proposal,
        proposal_ids=[record.proposal],
        instrument_ids=[catalogue.instrument],
        creation_location=f'ESS:{catalogue.instrument.upper()}',
        creation_time=datetime.now(tz=UTC),
        input_datasets=[PID.parse(i) for i in inputs],
        used_software=installed_software(),
        source_folder=RemotePath(
            catalogue.folder.format(
                proposal=record.proposal,
                instrument=catalogue.instrument,
                record=record.id,
            )
        ),
        meta={
            'spec': str(record.spec),
            'record': record.id,
            'format': format,
            'request': record.request.model_dump(mode='json', by_alias=True),
            'other_inputs': [i for i in identities if not _is_pid(i)],
        },
    )
    dataset.add_local_files(*files)
    finalized = catalogue.client.upload_new_dataset_now(dataset)
    return f'{_PID_PREFIX}{finalized.pid}'


def _is_pid(identity: str) -> bool:
    return identity.startswith(_PID_PREFIX)

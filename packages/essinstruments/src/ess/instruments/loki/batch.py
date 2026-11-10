# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch of I(Q) reductions of LoKI, on the toolbox of :mod:`ess.apps.batch`. It
needs the extra ``[loki-app]``.

A sample run is a row. The ``sample`` key of the metadata of its file names the
sample, and pairs the run with the transmission run of the same sample::

    {'file': 'run-1.nxs', 'run': 1, 'role': 'sample', 'sample': 'water'}
    {'file': 'run-2.nxs', 'run': 2, 'role': 'transmission', 'sample': 'water'}
"""

from collections.abc import Sequence

from ess.apps import Client
from ess.apps.batch import Batch, FileInfo, Row
from ess.spec import FileRef

from .specs import IOFQ

COLUMNS = ('sample', 'sample_transmission')
"""The parameters of I(Q) that a row gives; the others are the settings."""


def pair_by_sample(files: Sequence[FileInfo]) -> list[Row]:
    """
    A row for each sample run, in the order measured, keyed by its run, so that
    a sample measured twice has two rows. Where several transmission runs have the
    sample's name, the last measured is the row's. A sample run without a name
    has an empty transmission cell.
    """
    transmissions: dict[object, FileRef] = {}
    for f in files:
        if f.metadata.get('role') == 'transmission' and 'sample' in f.metadata:
            transmissions[f.metadata['sample']] = f.ref
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        name = f.metadata.get('sample')
        values = {'sample': f.ref}
        if name in transmissions:
            values['sample_transmission'] = transmissions[name]
        info = {} if name is None else {'name': str(name)}
        rows.append(Row(str(f.metadata.get('run', f.ref.file)), values, info=info))
    return rows


def iofq_batch(client: Client, *, label: str = 'iofq') -> Batch:
    """The batch of I(Q), through one stage, so that the can is reduced once."""
    return Batch(
        client,
        IOFQ,
        columns=COLUMNS,
        label=label,
        source=pair_by_sample,
        staged=True,
    )

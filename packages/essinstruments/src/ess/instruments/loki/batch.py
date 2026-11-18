# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch of I(Q) reductions of LoKI, on the toolbox of :mod:`ess.apps.batch`. It
needs the extra ``[loki-app]``.

A sample run is a row. The ``sample`` key of the metadata of its file names the
sample, and pairs the run with the transmission run of the same sample::

    {'file': 'run-1.nxs', 'run': 1, 'role': 'sample', 'sample': 'water'}
    {'file': 'run-2.nxs', 'run': 2, 'role': 'transmission', 'sample': 'water'}

:data:`SOURCES` and :data:`DEFAULTS` give a
:class:`~ess.apps.batch.BatchManager` the batches of I(Q);
:func:`iofq_batch` makes one batch without a manager, and the command takes
the source of both as ``--source ess.instruments.loki.batch:SAMPLE_RUNS``.
"""

import sys
from collections.abc import Mapping, Sequence
from typing import Any

from ess.apps import Client
from ess.apps.batch import (
    Batch,
    Defaults,
    DefinitionStore,
    FileInfo,
    Row,
    Source,
    Words,
)
from ess.spec import FileRef

from .specs import IOFQ

COLUMNS = ('sample', 'sample_transmission')
"""The parameters of I(Q) that a row gives; the others are the settings."""


def pair_by_sample(files: Sequence[FileInfo]) -> list[Row]:
    """
    A row for each sample run, in the order measured, keyed by its run, so that
    a sample measured twice has two rows, and indexed by its run number, none
    if the run is not a number. Where several transmission runs have the
    sample's name, the last measured is the row's. A sample run without a name
    has an empty transmission cell.

    The files are sorted by run number, since a file source lists them in no
    promised order, and a row whose transmission run changed from one scan to
    the next would be reduced again.
    """
    files = sorted(files, key=_measured)
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
        key = str(f.metadata.get('run', f.ref.file))
        rows.append(Row(key, values, info=info, index=_run_number(f)))
    return rows


def _run_number(f: FileInfo) -> int | None:
    """The run number of a file, ``None`` if its run is not a number."""
    run = str(f.metadata.get('run', ''))
    return int(run) if run.isdigit() else None


def _measured(f: FileInfo) -> tuple[int, str]:
    """The order measured: by run number; a file without one last, by identity."""
    run = _run_number(f)
    return (sys.maxsize, f.ref.file) if run is None else (run, '')


SAMPLE_RUNS = Source(
    pair_by_sample,
    fills=COLUMNS,
    words=Words(one='sample run', many='sample runs'),
    row_name='name',
    index_title='Run',
)
"""A row per sample run, paired by :func:`pair_by_sample`."""
SOURCES = {'sample-runs': SAMPLE_RUNS}
"""The sources of LoKI's batches, by name."""
DEFAULTS = {IOFQ.name: Defaults(staged=True)}
"""A batch of I(Q) runs through one stage, so that the can is reduced once."""


def iofq_batch(
    client: Client,
    *,
    label: str = 'iofq',
    persist: bool = False,
    settings: Mapping[str, Any] | None = None,
    definitions: DefinitionStore | None = None,
) -> Batch:
    """
    The batch of I(Q), through one stage, so that the can is reduced once.
    ``persist``, ``settings`` and ``definitions`` are those of :class:`Batch`.
    """
    return Batch(
        client,
        IOFQ,
        columns=COLUMNS,
        label=label,
        source=SAMPLE_RUNS,
        staged=True,
        persist=persist,
        settings=settings,
        definitions=definitions,
    )

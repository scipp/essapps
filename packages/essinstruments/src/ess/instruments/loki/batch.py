# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of I(Q) of LoKI, on the toolbox of :mod:`ess.apps.batch`. It
needs the extra ``[loki-app]``.

A sample run is a row. The ``sample`` key of the metadata of its file names the
sample, and pairs the run with the transmission run of the same sample::

    {'file': 'run-1.nxs', 'run': 1, 'role': 'sample', 'sample': 'water'}
    {'file': 'run-2.nxs', 'run': 2, 'role': 'transmission', 'sample': 'water'}

:data:`SOURCES` gives an :class:`~ess.apps.batch.Intake`, and :data:`STAGED`
a :class:`~ess.apps.batch.BatchManager`, the batches of I(Q). A batch without
a manager keeps nothing::

    Batch(client, IOFQ, label='iofq', rows=rows_of(client, SAMPLE_RUNS), staged=True)

The command takes the sources as ``essapps-batch -s
ess.instruments.loki.batch:SOURCES``, and a source by its name, such as
``essapps-batch -s ... rows sample-runs``.
"""

import sys
from collections.abc import Sequence

import pandas as pd

from ess.apps.batch import FileInfo, RowSource
from ess.apps.batch.table import KEY, cell_of

from .specs import IOFQ


def pair_by_sample(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    A row for each sample run, in the order measured, keyed by its run, so that
    a sample measured twice has two rows. The cells ``sample`` and
    ``sample_transmission`` name the files; the notes are ``run``, the run
    number, empty if the run is not a number, and ``name``, the sample's name.
    Where several transmission runs have the sample's name, the last measured
    is the row's. A sample run without a name has an empty transmission cell.

    The files are sorted by run number, since a file source lists them in no
    promised order, and "the last measured" transmission run must not depend
    on it.
    """
    files = sorted(files, key=_measured)
    transmissions: dict[object, str] = {}
    for f in files:
        if f.metadata.get('role') == 'transmission' and 'sample' in f.metadata:
            transmissions[f.metadata['sample']] = cell_of(f.ref)
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        name = f.metadata.get('sample')
        rows.append(
            {
                KEY: str(f.metadata.get('run', f.ref.file)),
                'run': _run_number(f),
                'name': None if name is None else str(name),
                'sample': cell_of(f.ref),
                'sample_transmission': transmissions.get(name),
            }
        )
    columns = [KEY, 'run', 'name', 'sample', 'sample_transmission']
    frame = pd.DataFrame.from_records(rows, columns=columns).set_index(KEY)
    return frame.astype({'run': 'Int64', 'name': 'string'})


def _run_number(f: FileInfo) -> int | None:
    """The run number of a file, ``None`` if its run is not a number."""
    run = str(f.metadata.get('run', ''))
    return int(run) if run.isdigit() else None


def _measured(f: FileInfo) -> tuple[int, str]:
    """The order measured: by run number; a file without one last, by identity."""
    run = _run_number(f)
    return (sys.maxsize, f.ref.file) if run is None else (run, '')


SAMPLE_RUNS = RowSource(
    pair_by_sample,
    fills=('sample', 'sample_transmission'),
    description='A row per run of role sample in files.csv, keyed by its run '
    'number. Its sample_transmission is the run of role transmission whose '
    'sample in files.csv is that of the sample run; of several, the one with '
    'the highest run number, whether measured before or after the sample run. '
    'A sample run without a sample name has no transmission run.',
)
"""A row per sample run, paired by :func:`pair_by_sample`."""
SOURCES = {'sample-runs': SAMPLE_RUNS}
"""The sources of LoKI's batches, by name."""
STAGED = frozenset({IOFQ.name})
"""A batch of I(Q) runs through one stage, so that the can is reduced once."""

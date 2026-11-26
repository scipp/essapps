# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
LoKI's row sources for :mod:`ess.batch`. It needs the extra ``[loki-app]``.

``essbatch`` accepts them as ``-s ess.instruments.loki.batch:SOURCES``, and one
source by its name, such as ``essbatch -s ... rows sample-runs``.
"""

import sys
from collections import Counter
from collections.abc import Sequence

import pandas as pd
from ess.batch import FileInfo, RowSource
from ess.batch.table import KEY, NAME, cell_of

from .specs import IOFQ


def pair_by_sample(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    Returns a row per sample run, with the last transmission run of its sample.

    Files with this metadata::

        {'run': 1, 'role': 'sample', 'sample': 'water'}
        {'run': 2, 'role': 'transmission', 'sample': 'water'}
        {'run': 3, 'role': 'transmission', 'sample': 'water'}

    give one row, keyed by the run::

        {'1': {'run': 1, 'name': 'water', 'sample': <file of run 1>,
               'sample_transmission': <file of run 3>}}

    ``run`` is empty if the run is not a number.
    """
    # A file source lists files in no promised order, and the last transmission
    # run must not depend on it.
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


def per_run(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    Returns a row per raw run, in the order measured, with no role needed.

    A row is keyed by its run, or by the file where two files have the same run,
    such as a run ingested twice. ``NAME``, shown on the page and in the plot,
    is the file's ``title``.
    """
    runs = Counter(str(f.metadata['run']) for f in files if 'run' in f.metadata)

    def key(f: FileInfo) -> str:
        run = str(f.metadata.get('run'))
        return run if runs[run] == 1 else f.ref.file

    rows = [
        {
            KEY: key(f),
            NAME: f.metadata.get('title'),
            'sample': f.metadata.get('sample'),
            'run': cell_of(f.ref),
        }
        for f in sorted(files, key=_measured)
    ]
    columns = [KEY, NAME, 'sample', 'run']
    frame = pd.DataFrame.from_records(rows, columns=columns).set_index(KEY)
    return frame.astype({NAME: 'string', 'sample': 'string'})


def _run_number(f: FileInfo) -> int | None:
    """The run number of a file, ``None`` if its run is not a number."""
    run = str(f.metadata.get('run', ''))
    return int(run) if run.isdigit() else None


def _measured(f: FileInfo) -> tuple[int, str]:
    """The order measured: by run number, then files without one, by identity."""
    run = _run_number(f)
    return (sys.maxsize, f.ref.file) if run is None else (run, '')


SAMPLE_RUNS = RowSource(
    pair_by_sample,
    source_columns=('sample', 'sample_transmission'),
    description='A row per run of role sample in files.csv, keyed by its run '
    'number. Its sample_transmission is the run of role transmission whose '
    'sample in files.csv is that of the sample run; of several, the one with '
    'the highest run number, whether measured before or after the sample run. '
    'A sample run without a sample name has no transmission run.',
)
"""A row per sample run, paired by :func:`pair_by_sample`."""
RAW_RUNS = RowSource(
    per_run,
    source_columns=('run',),
    description='A row per raw run, keyed by its run number, with no pairing '
    'and no role needed; for the bank histogram.',
)
"""A row per raw run, made by :func:`per_run`."""
SOURCES = {'sample-runs': SAMPLE_RUNS, 'raw-runs': RAW_RUNS}
"""The sources of LoKI's batches, by name."""
STAGED = frozenset({IOFQ.name})
"""The specs whose batches run through one stage, so that the can is reduced once."""

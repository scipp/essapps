# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a workspace folder that was moved."""

from collections.abc import Sequence
from pathlib import Path

import pandas as pd
from ess.batch import Batch, FileInfo, RowSource, rows_of
from ess.batch.auto import step
from ess.batch.frames import with_results
from ess.batch.table import KEY

from ess.apps import Client
from ess.apps.folder import open_folder
from ess.apps.testing import read_file
from ess.spec import function_binding

from . import toys


def _find(params: toys.FindParams) -> toys.FindOutputs:
    return toys.FindOutputs(offset=toys.Offset(value=read_file(params.sample)))


BINDINGS = [function_binding(toys.FIND, _find)]


def _runs(files: Sequence[FileInfo]) -> pd.DataFrame:
    keys = pd.Index([str(f.metadata['run']) for f in files], name=KEY)
    return pd.DataFrame({'sample': [str(f.ref) for f in files]}, index=keys)


RUNS = RowSource(_runs, source_columns=('sample',))
"""A row per run."""


def _found(client: Client) -> Batch:
    """The rows of the runs, whose offset is found under the label ``found``."""
    rows = rows_of(client, RUNS)
    return Batch(client, toys.FIND, label='found', rows=rows, persist=True)


def _table(client: Client) -> pd.DataFrame:
    batch = _found(client)
    return with_results(batch, batch.state())


def test_a_batch_on_a_moved_folder_shows_what_it_showed_and_reduces_nothing(
    tmp_path: Path,
) -> None:
    folder = tmp_path / 'workspace'
    folder.mkdir()
    lines = ['file,run,role,pid']
    for run in (1, 2, 3):
        (folder / f'run-{run}.json').write_text(str(run))
        pid = '20.500.12269/abc' if run == 3 else ''
        lines.append(f'run-{run}.json,{run},sample,{pid}')
    (folder / 'files.csv').write_text('\n'.join(lines))

    with open_folder(folder, BINDINGS) as first:
        found = list(step(_found(first), RUNS, ()).records.values())
        first.wait(found)
        table = _table(first)

    with open_folder(folder.rename(folder.with_name('moved')), BINDINGS) as second:
        assert step(_found(second), RUNS, ()).records == {}
        assert _table(second).equals(table)

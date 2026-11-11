# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch application on fake files and fake workflows, for working on the UI.

``python -m ess.apps.batch.panel.demo [DIRECTORY]`` serves it on a workspace
kept in ``DIRECTORY`` (see :mod:`ess.apps.folder`), a new temporary folder if
not given. The first start writes the fake files and ``files.csv`` there, and a
later start reuses them, with the records of earlier starts. While the app is
not running, the command works on the same folder with :data:`BINDINGS`::

    B="essapps-batch -w DIRECTORY --bind ess.apps.batch.panel.demo:BINDINGS"
    $B reduce settings.yaml rows.csv --label demo -o done.csv

Reductions take a moment, so rows show as pending. One bin fails every
reduction, a thickness of zero fails one row, and a thickness below zero is
refused. Everything but the workflows and the files is real, so a change here
shows what an application gets.

One process holds a folder, so one client serves every session, and each
session has a batch of its own. The corrections of the run sheet are shared by
the sessions, and end with the process.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import panel as pn
from pydantic import BaseModel, Field

from ess.apps import Client, Record
from ess.apps.folder import folder_files, open_folder
from ess.apps.panel import App
from ess.apps.testing import StagedFunction, read_file
from ess.spec import RunFile, WorkflowSpec

from .. import Batch, CorrectedFiles, FileInfo, Row
from . import BatchView, RunSheet


class Binning(BaseModel):
    bins: int = Field(default=10, ge=1, description='The number of bins.')


class Params(BaseModel):
    binning: Binning = Field(default_factory=Binning)
    sample: RunFile = Field(description='The sample run.')
    transmission: RunFile = Field(description='The transmission run of the sample.')
    thickness: Annotated[float, Field(ge=0, description='In mm.')] = 1.0


class Outputs(BaseModel):
    total: float


DEMO = WorkflowSpec(
    name='demo-reduction',
    version=1,
    title='Demo reduction',
    description='Divides the counts of a sample by its thickness.',
    params=Params,
    outputs=Outputs,
)


class Marks(BaseModel):
    """What a person may correct about a run."""

    role: Literal['sample', 'transmission'] | None = None
    sample: str | None = None


def _reduce(params: Params) -> Outputs:
    time.sleep(0.5)
    if params.binning.bins == 1:
        raise ZeroDivisionError('one bin')
    return Outputs(total=read_file(params.sample) / params.thickness)


BINDINGS = [StagedFunction(DEMO, _reduce)]
"""The bindings of the demo's workspace, for the command."""


def pair_by_sample(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, with the transmission run of the same sample."""
    transmission = {
        f.metadata['sample']: f.ref
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        name = f.metadata.get('sample')
        values = {'sample': f.ref}
        if name in transmission:
            values['transmission'] = transmission[name]
        rows.append(Row(str(f.metadata['run']), values, info={'name': str(name)}))
    return rows


def _measure(directory: Path, samples: int) -> None:
    """Write the files of ``samples`` samples and ``files.csv``, unless there."""
    if (directory / 'files.csv').exists():
        return
    lines = ['file,run,role,sample']
    for i in range(samples):
        runs = [(2 * i + 1, i + 1.0, 'sample')]
        if i % 7 != 3:  # some samples have no transmission run yet
            runs.append((2 * i + 2, 1.0, 'transmission'))
        for run, content, role in runs:
            (directory / f'run-{run}.json').write_text(json.dumps(content))
            lines.append(f'run-{run}.json,{run},{role},sample-{i}')
    (directory / 'files.csv').write_text('\n'.join(lines) + '\n')


def open_demo(directory: Path, samples: int = 20) -> tuple[Client, CorrectedFiles]:
    """
    A client of the demo's workspace in ``directory``, with ``samples`` samples
    on the first start, and the file source that the run sheet corrects.
    """
    _measure(directory, samples)
    files = CorrectedFiles(folder_files(directory))
    return open_folder(directory, BINDINGS, files=files), files


def build(client: Client, files: CorrectedFiles) -> App:
    """The application of one session, on what :func:`open_demo` returns."""
    batch = Batch(
        client,
        DEMO,
        columns=('sample', 'transmission', 'thickness'),
        label='demo',
        source=pair_by_sample,
        staged=True,
        persist=True,
    )

    def draw(records: Mapping[str, Record]) -> pn.pane.Markdown:
        lines = [f'{k}: {client.output(r, "total")}' for k, r in records.items()]
        return pn.pane.Markdown('\n\n'.join(lines))

    view = BatchView(batch, info=('name',), draw=draw)
    view.scan()
    sheet = RunSheet(files, client, Marks, then=view.scan)
    return App(client, tabs={'Batch': view, 'Runs': sheet})


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the batch demo.')
    parser.add_argument('directory', type=Path, nargs='?', help='the workspace')
    directory = parser.parse_args().directory
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix='essapps-batch-demo-'))
    directory.mkdir(parents=True, exist_ok=True)
    pn.extension('tabulator')
    client, files = open_demo(directory)
    with client:
        pn.serve(lambda: build(client, files).panel(), port=5006, show=False)


if __name__ == '__main__':
    main()

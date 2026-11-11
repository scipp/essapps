# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch application on fake files and fake workflows, for working on the UI.

``python -m ess.apps.batch.panel.demo`` serves it. Reductions take a moment, so
rows show as pending. One bin fails every reduction, a thickness of zero fails
one row, and a thickness below zero is refused. Everything but the workflows
and the files is real, so a change here shows what an application gets.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal

import panel as pn
from pydantic import BaseModel, Field

from ess.apps import Record, local
from ess.apps.panel import App
from ess.apps.testing import FakeFiles, StagedFunction, read_file
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


def build(directory: Path, samples: int = 20) -> App:
    """The application over ``samples`` samples, whose files go in ``directory``."""
    fake = FakeFiles('demo', directory)
    for i in range(samples):
        name = f'sample-{i}'
        fake.measure(2 * i + 1, float(i + 1), role='sample', sample=name)
        if i % 7 != 3:  # some samples have no transmission run yet
            fake.measure(2 * i + 2, 1.0, role='transmission', sample=name)
    files = CorrectedFiles(fake)
    client = local(proposal='demo', files=files, bind=[StagedFunction(DEMO, _reduce)])
    batch = Batch(
        client,
        DEMO,
        columns=('sample', 'transmission', 'thickness'),
        label='demo',
        source=pair_by_sample,
        staged=True,
    )

    def draw(records: Mapping[str, Record]) -> pn.pane.Markdown:
        lines = [f'{k}: {client.output(r, "total")}' for k, r in records.items()]
        return pn.pane.Markdown('\n\n'.join(lines))

    view = BatchView(batch, info=('name',), draw=draw)
    view.scan()
    sheet = RunSheet(files, client, Marks, then=view.scan)
    return App(client, tabs={'Batch': view, 'Runs': sheet})


def main() -> None:
    pn.extension('tabulator')
    directory = tempfile.mkdtemp(prefix='essapps-batch-demo-')
    pn.serve(lambda: build(Path(directory)).panel(), port=5006, show=False)


if __name__ == '__main__':
    main()

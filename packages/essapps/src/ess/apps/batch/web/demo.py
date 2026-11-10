# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page on fake SANS-shaped files and a fake I(Q) reduction, for
working on the page.

``python -m ess.apps.batch.web.demo [DIRECTORY] [--port N]`` serves it on
``http://localhost:N``, with the workspace kept in ``DIRECTORY`` (see
:mod:`ess.apps.folder`), a new temporary folder if not given. The first start
writes the fake runs and ``files.csv`` there; a later start reuses them, with
the results of earlier starts.

Each sample run holds the size of its particles, so samples differ visibly, and
the curve depends on the settings. Three samples have no transmission run, one
sample run holds no events and fails, and a reduction takes 0.3 to 1 s.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
import scipp as sc
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client
from ess.apps.folder import folder_files, open_folder
from ess.apps.testing import StagedFunction, read_file
from ess.spec import (
    BackgroundRunFile,
    SampleRunFile,
    TransmissionRunFile,
    WorkflowSpec,
)

from .. import Batch, CorrectedFiles, FileInfo, Row
from . import create_app, plots
from .api import STATIC
from .models import Words
from .server import Shown


class Wavelength(BaseModel):
    start: float = Field(default=2.0, gt=0, title='From (Å)')
    stop: float = Field(default=12.0, gt=0, title='To (Å)')


class LinearQ(BaseModel):
    spacing: Literal['linear'] = Field(default='linear', title='Linear')
    q_min: float = Field(default=0.004, gt=0, title='Q min (1/Å)')
    q_max: float = Field(default=0.4, gt=0, title='Q max (1/Å)')
    bins: int = Field(default=120, ge=2, title='Bins')


class LogQ(BaseModel):
    spacing: Literal['log'] = Field(default='log', title='Logarithmic')
    q_min: float = Field(default=0.004, gt=0, title='Q min (1/Å)')
    q_max: float = Field(default=0.4, gt=0, title='Q max (1/Å)')
    per_decade: int = Field(default=30, ge=2, title='Bins per decade')


class Corrections(BaseModel):
    solid_angle: bool = Field(default=True, title='Solid angle')
    normalize_by: Literal['monitor', 'proton charge'] = Field(
        default='monitor', title='Normalise by'
    )
    banks: list[int] = Field(default=[0, 1, 2, 3], title='Detector banks')


class Params(BaseModel):
    wavelength: Wavelength = Field(default_factory=Wavelength, title='Wavelength range')
    q_binning: Annotated[
        LinearQ | LogQ, Field(discriminator='spacing', title='Q binning')
    ] = LogQ()
    corrections: Corrections = Field(default_factory=Corrections)
    background: BackgroundRunFile | None = Field(
        default=None,
        title='Empty cell',
        description='Subtracted from every sample.',
    )
    sample: SampleRunFile = Field(title='Sample run')
    transmission: TransmissionRunFile = Field(title='Transmission run')
    thickness: Annotated[float, Field(gt=0, title='Thickness (mm)')] = 1.0


class Outputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    iofq: sc.DataArray = Field(title='I(Q)')


DEMO = WorkflowSpec(
    name='demo-iofq',
    version=1,
    title='I(Q) demo',
    description='A fake SANS reduction to I(Q), from the size of particles.',
    params=Params,
    outputs=Outputs,
)


class Marks(BaseModel):
    """What a person may correct about a run."""

    role: Literal['sample', 'transmission', 'background'] | None = None
    sample: str | None = Field(default=None, title='Sample name')


def _edges(binning: LinearQ | LogQ) -> np.ndarray:
    if isinstance(binning, LinearQ):
        return np.linspace(binning.q_min, binning.q_max, binning.bins + 1)
    decades = np.log10(binning.q_max / binning.q_min)
    n = max(2, round(decades * binning.per_decade))
    return np.geomspace(binning.q_min, binning.q_max, n + 1)


def _sphere(q: np.ndarray, radius: float) -> np.ndarray:
    x = q * radius
    return (3 * (np.sin(x) - x * np.cos(x)) / x**3) ** 2


def _reduce(params: Params) -> Outputs:
    sample = read_file(params.sample)
    transmission = read_file(params.transmission)['transmission']
    rng = np.random.default_rng(sample['seed'])
    time.sleep(rng.uniform(0.3, 1.0))
    if sample['events'] == 0:
        raise RuntimeError(
            f'run {sample["run"]} holds no neutron events: was the shutter closed?'
        )
    edges = _edges(params.q_binning)
    q = (edges[:-1] + edges[1:]) / 2
    corrections = params.corrections
    flux = (params.wavelength.stop - params.wavelength.start) * len(corrections.banks)
    # Few enough counts that the error bars show at high Q.
    counts = sample['events'] * flux / 80_000
    level = sample['scale'] * _sphere(q, sample['radius']) + sample['incoherent']
    if not corrections.solid_angle:
        level = level * (1 + 8 * q)
    if corrections.normalize_by == 'proton charge':
        level = level * 1.07
    if params.background is not None:
        level = level + 0.02 - read_file(params.background)['level']
    expected = np.maximum(level * counts * q**-1.2, 1.0)
    measured = rng.poisson(expected).astype(float)
    norm = counts * q**-1.2 * transmission * params.thickness / 1.0
    values = measured / norm
    variances = np.maximum(measured, 1.0) / norm**2
    return Outputs(
        iofq=sc.DataArray(
            sc.array(dims=['Q'], values=values, variances=variances, unit='1/cm'),
            coords={'Q': sc.array(dims=['Q'], values=edges, unit='1/angstrom')},
        )
    )


BINDINGS = [StagedFunction(DEMO, _reduce)]
"""The bindings of the demo's workspace, for the command."""


def pair_by_sample(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, with the transmission run of the same sample."""
    transmission = {
        f.metadata.get('sample'): f.ref
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


SAMPLES = (
    # name, particle radius in Å, contrast scale
    ('H2O buffer', 8, 0.02),
    ('D2O buffer', 8, 0.01),
    ('SDS 10 mM in D2O', 20, 0.6),
    ('SDS 50 mM in D2O', 21, 2.4),
    ('CTAB micelles', 26, 1.8),
    ('Silica 20 nm', 100, 40.0),
    ('Silica 50 nm', 250, 90.0),
    ('Polystyrene latex', 300, 120.0),
    ('Lysozyme 5 mg/ml', 16, 0.4),
    ('Lysozyme 10 mg/ml', 16, 0.8),
    ('BSA 20 mg/ml', 30, 1.5),
    ('Apoferritin', 60, 6.0),
    ('P123 at 25 °C', 45, 3.0),
    ('P123 at 40 °C', 70, 7.0),
    ('Pluronic F127', 55, 4.5),
    ('Gold nanoparticles 10 nm', 50, 12.0),
    ('DOPC liposomes', 150, 20.0),
    ('DPPC liposomes', 170, 24.0),
    ('Cellulose nanocrystals', 80, 9.0),
    ('Casein micelles', 120, 30.0),
    ('Water-in-oil microemulsion', 40, 5.0),
    ('PS-PI block copolymer', 90, 15.0),
    ('Nanodiscs MSP1D1', 45, 2.2),
    ('Detector check', 30, 1.0),
)
_NO_TRANSMISSION = {'SDS 50 mM in D2O', 'Apoferritin', 'Casein micelles'}
_NO_EVENTS = 'Detector check'
FIRST_RUN = 60301


def _measure(directory: Path) -> None:
    """Write the runs and ``files.csv``, unless there."""
    if (directory / 'files.csv').exists():
        return
    lines = ['file,run,role,sample']

    def write(run: int, content: dict[str, object], role: str, sample: str) -> None:
        (directory / f'{run}.json').write_text(json.dumps({'run': run, **content}))
        lines.append(f'{run}.json,{run},{role},{sample}')

    run = FIRST_RUN
    write(run, {'level': 0.02}, 'background', 'empty cell')
    for i, (name, radius, scale) in enumerate(SAMPLES):
        run += 1
        events = 0 if name == _NO_EVENTS else 2_000_000
        sample = {'radius': radius, 'scale': scale, 'incoherent': 0.03}
        write(run, {**sample, 'events': events, 'seed': i}, 'sample', name)
        if name not in _NO_TRANSMISSION:
            run += 1
            write(run, {'transmission': 0.95 - 0.01 * (i % 9)}, 'transmission', name)
    (directory / 'files.csv').write_text('\n'.join(lines) + '\n')


def open_demo(directory: Path) -> tuple[Client, CorrectedFiles]:
    """A client of the demo's workspace in ``directory``, and its file source."""
    _measure(directory)
    files = CorrectedFiles(folder_files(directory))
    return open_folder(directory, BINDINGS, files=files), files


def build(client: Client, files: CorrectedFiles, folder: str = '') -> FastAPI:
    """The application on what :func:`open_demo` returns, scanned."""
    batch = Batch(
        client,
        DEMO,
        columns=('sample', 'transmission', 'thickness'),
        label='demo-iofq',
        source=pair_by_sample,
        staged=True,
        persist=True,
    )
    batch.scan()

    def draw(shown: Sequence[Shown]) -> dict[str, object]:
        curves = [
            plots.Curve(s.name, client.output(s.record, 'iofq'), s.color, s.outdated)
            for s in shown
        ]
        return plots.figure(curves, quantity='I(Q)')

    return create_app(
        batch,
        words=Words(one='sample', many='samples'),
        name='name',
        draw=draw,
        files=files,
        marks=Marks,
        folder=folder,
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description='Serve the batch page demo.')
    parser.add_argument('directory', type=Path, nargs='?', help='the workspace')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    directory = args.directory
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix='essapps-web-demo-'))
    directory.mkdir(parents=True, exist_ok=True)
    client, files = open_demo(directory)
    with client:
        app = build(client, files, folder=directory.name)
        uvicorn.run(app, host='localhost', port=args.port)


if __name__ == '__main__':
    main()

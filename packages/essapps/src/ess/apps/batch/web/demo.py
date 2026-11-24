# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page on fake SANS-shaped files and a fake I(Q) reduction, for
working on the page.

``python -m ess.apps.batch.web.demo [DIRECTORY] [--port N]`` serves it on
``http://localhost:N``, with the workspace kept in ``DIRECTORY`` (see
:mod:`ess.apps.folder`), a new temporary folder if not given. The first start
writes the fake runs and ``files.csv`` there. A later start reuses them, with
the results of earlier starts, and the batches with their batch tables and
settings, the corrections, and the batch that follows kept in
``definitions/`` (:mod:`..definition`). Each start makes a batch of the
samples there are if no batch is listed; it does not follow, and a person
makes a batch that follows with "New batch", Follow ticked. The file
source reads ``files.csv`` again when it changes
(:class:`~ess.apps.folder.ArrivingFiles`).

``--samples N`` sets how many samples a new folder gets (24): beyond the 24 of
:data:`SAMPLES`, the same again as ``<name> #2``, ``#3``... With ``--arrive
SECONDS [--start M]``, a new folder starts with the empty cell and the first
``M`` samples (3), and the runs of one more sample arrive every ``SECONDS``,
until all have arrived. ``--interval SECONDS`` sets the time between two steps
of Follow (5).

Each sample run holds the size of its particles, so samples differ visibly, and
the curve depends on the settings. Three samples have no transmission run, one
sample run holds no events and fails, and a reduction takes 0.3 to 1 s.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import numpy as np
import pandas as pd
import scipp as sc
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client
from ess.apps.folder import ArrivingFiles, open_folder, write_files_table
from ess.apps.testing import StagedFunction, read_file
from ess.spec import (
    BackgroundRunFile,
    SampleRunFile,
    TransmissionRunFile,
    WorkflowSpec,
)

from .. import (
    BatchManager,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Intake,
    RowSource,
)
from ..table import KEY, cell_of
from . import Pages, create_app, plots
from .api import STATIC, every
from .page import Shown

if TYPE_CHECKING:
    from ...scicat import Catalogue


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


def pair_by_sample(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    A row per sample run, keyed by its run, with the transmission run of the
    same sample; the run and the name of the sample as notes.
    """
    transmission = {
        f.metadata.get('sample'): cell_of(f.ref)
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = [
        {
            KEY: str(f.metadata['run']),
            'run': f.metadata['run'],
            'name': str(f.metadata.get('sample')),
            'sample': cell_of(f.ref),
            'transmission': transmission.get(f.metadata.get('sample')),
        }
        for f in files
        if f.metadata.get('role') == 'sample'
    ]
    columns = [KEY, 'run', 'name', 'sample', 'transmission']
    return pd.DataFrame.from_records(rows, columns=columns).set_index(KEY)


SOURCES = {'sample-runs': RowSource(pair_by_sample, fills=('sample', 'transmission'))}
"""The sources of the demo's batches, by name."""
STAGED = frozenset({DEMO.name})
"""The specs whose batches run through a stage: the demo's."""


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
AT_START = 3
"""The samples there at the start when the others arrive (``--arrive``)."""

type Run = tuple[int, dict[str, object], str, str]
"""A run: its number, the content of its file, its role, and its sample."""


def measurements(samples: int = len(SAMPLES)) -> list[list[Run]]:
    """
    The runs, in the groups that arrive together: the empty cell, then each of
    the first ``samples`` samples with its transmission run, if it has one.
    Beyond :data:`SAMPLES`, the samples are these again, named ``#2``, ``#3``...
    """
    run = FIRST_RUN
    groups = [[(run, {'level': 0.02}, 'background', 'empty cell')]]
    for i in range(samples):
        kind, radius, scale = SAMPLES[i % len(SAMPLES)]
        repeat = i // len(SAMPLES)
        name = f'{kind} #{repeat + 1}' if repeat else kind
        run += 1
        events = 0 if kind == _NO_EVENTS else 2_000_000
        sample = {'radius': radius, 'scale': scale, 'incoherent': 0.03}
        group: list[Run] = [
            (run, {**sample, 'events': events, 'seed': i}, 'sample', name)
        ]
        if kind not in _NO_TRANSMISSION:
            run += 1
            content = {'transmission': 0.95 - 0.01 * (i % 9)}
            group.append((run, content, 'transmission', name))
        groups.append(group)
    return groups


DEMO_PID_PREFIX = '20.500.12269'
"""The prefix of the catalogue identifiers of the demo's runs, that of ESS."""


def measure(directory: Path, runs: Sequence[Run]) -> None:
    """
    Write the file of each of ``runs`` that is not there, then replace
    ``files.csv`` with a table of ``runs``, each with a catalogue identifier,
    as a raw file at ESS has.
    """
    rows = []
    for run, content, role, sample in runs:
        path = directory / f'{run}.json'
        if not path.exists():
            path.write_text(json.dumps({'run': run, **content}))
        rows.append(
            {
                'file': path.name,
                'run': run,
                'pid': f'{DEMO_PID_PREFIX}/demo-run-{run}',
                'role': role,
                'sample': sample,
            }
        )
    write_files_table(directory, rows)


def open_demo(
    directory: Path, *, samples: int = len(SAMPLES)
) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the demo's workspace in ``directory``, its file source, and the
    folder's definitions. A folder without runs gets those of ``samples``
    samples.
    """
    if not (directory / 'files.csv').exists():
        runs = [run for group in measurements(samples) for run in group]
        measure(directory, runs)
    definitions = FolderDefinitions(directory)
    files = CorrectedFiles(ArrivingFiles(directory), definitions=definitions)
    return open_folder(directory, BINDINGS, files=files), files, definitions


def build(
    client: Client,
    files: CorrectedFiles,
    definitions: FolderDefinitions,
    folder: str = '',
    *,
    interval: float = 5.0,
    catalogue: Catalogue | None = None,
) -> FastAPI:
    """
    The application on what :func:`open_demo` returns, with a step of Follow
    every ``interval`` seconds while a batch follows, and the tab "Publish"
    if given a ``catalogue``. If no batch is listed,
    a batch of the samples there are is made, with a column of thickness, so
    also at a start after a person removed the last one.
    """
    intake = Intake(
        BatchManager(client, definitions=definitions, staged=STAGED), sources=SOURCES
    )
    if not intake.manager.headers():
        intake.create(
            DEMO.name,
            title='Samples',
            source='sample-runs',
            parameter_columns=['thickness'],
        )

    def draw(shown: Sequence[Shown]) -> dict[str, object]:
        curves = [
            plots.Curve(s.name, client.output(s.record, 'iofq'), s.color, s.outdated)
            for s in shown
        ]
        return plots.figure(curves, quantity='I(Q)')

    publisher = None
    if catalogue is not None:
        from ...scicat import principal_investigator
        from ..publish import Publisher
        from ..writing import Writing

        contact = principal_investigator(catalogue, client.proposal)
        publisher = Publisher(Writing(intake.manager, {}, contact), catalogue)
    pages = Pages(
        intake,
        interval=interval,
        title=DEMO.title,
        draw={DEMO.name: draw},
        files=files,
        marks=Marks,
        folder=folder,
        publisher=publisher,
    )
    return create_app(pages)


def fake_catalogue(proposal: str) -> Catalogue:
    """
    A SciCat in memory (scitacean's fakes), which lists ``proposal``, that of
    the demo's workspace, with a principal investigator; it forgets the
    datasets published at a restart.
    """
    from scitacean import model
    from scitacean.testing.client import FakeClient
    from scitacean.testing.transfer import FakeFileTransfer

    from ...scicat import Catalogue

    scicat = FakeClient.without_login(
        url='https://scicat.invalid/api/v3', file_transfer=FakeFileTransfer()
    )
    scicat.proposals[proposal] = model.DownloadProposal(
        proposalId=proposal,
        title='Micelles and nanoparticles in solution',
        email='useroffice@example.org',
        ownerGroup=proposal,
        pi_firstname='Ada',
        pi_lastname='Lovelace',
        pi_email='ada.lovelace@example.org',
    )
    return Catalogue(client=scicat, instrument='demo')


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the batch page demo.')
    parser.add_argument('directory', type=Path, nargs='?', help='the workspace')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument(
        '--arrive', type=float, metavar='SECONDS', help='a sample every SECONDS'
    )
    parser.add_argument(
        '--samples',
        type=int,
        default=len(SAMPLES),
        metavar='N',
        help=f'the samples of a new folder ({len(SAMPLES)})',
    )
    parser.add_argument(
        '--start',
        type=int,
        metavar='M',
        help=f'with --arrive: the samples there at the start ({AT_START})',
    )
    parser.add_argument(
        '--interval',
        type=float,
        default=5.0,
        metavar='SECONDS',
        help='a step of Follow every SECONDS (5)',
    )
    parser.add_argument(
        '--fake-catalogue',
        action='store_true',
        help='publish to a SciCat in memory (needs the extra scicat)',
    )
    args = parser.parse_args()
    if args.start is not None and args.arrive is None:
        parser.error('--start goes with --arrive')
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    directory = args.directory
    if directory is None:
        directory = Path(tempfile.mkdtemp(prefix='essapps-web-demo-'))
    directory.mkdir(parents=True, exist_ok=True)
    if args.arrive is not None and (directory / 'files.csv').exists():
        parser.exit(1, f'--arrive needs a new folder; {directory} has runs\n')
    sys.stdout.write(f'folder {directory}\n')
    sys.stdout.flush()
    if args.arrive is None:
        _serve(
            directory,
            args.port,
            interval=args.interval,
            samples=args.samples,
            fake=args.fake_catalogue,
        )
        return
    groups = measurements(args.samples)
    start = 1 + (AT_START if args.start is None else args.start)  # the empty cell
    arrived = [run for group in groups[:start] for run in group]
    measure(directory, arrived)

    def arrive(group: list[Run]) -> None:
        arrived.extend(group)
        measure(directory, arrived)

    plan = [partial(arrive, g) for g in groups[start:]]
    with every(args.arrive, plan):
        _serve(
            directory,
            args.port,
            interval=args.interval,
            samples=args.samples,
            fake=args.fake_catalogue,
        )


def _serve(
    directory: Path,
    port: int,
    *,
    interval: float,
    samples: int,
    fake: bool,
) -> None:
    import uvicorn

    try:
        client, files, definitions = open_demo(directory, samples=samples)
    except ValueError as error:  # such as definitions of another format
        raise SystemExit(str(error)) from None
    with client:
        folder = directory.name
        app = build(
            client,
            files,
            definitions,
            folder=folder,
            interval=interval,
            catalogue=fake_catalogue(client.proposal) if fake else None,
        )
        uvicorn.run(app, host='localhost', port=port)


if __name__ == '__main__':
    main()

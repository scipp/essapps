# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Serves the batch page of LoKI's I(Q) (:mod:`ess.batch.web`) on a workspace folder.
It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web FOLDER [--port 8000] [--interval SECONDS]
        [--scicat PROFILE]... [--no-local]

``FOLDER`` is a workspace folder (:mod:`ess.apps.folder`). Its ``files.csv`` lists
the run, role and sample of each file, as ``essbatch files`` writes it. A row is a
sample run with the transmission run of its sample (:func:`~.batch.pair_by_sample`).
The tab "Output" writes I(Q) as NXcanSAS (:mod:`.writers`), and any output as
scipp HDF5.

The settings refer to the masks by a record. So reduce the detector and the masks
first, on the same folder, while no page runs on it. With the run of the can and a
mask file of the folder, write two templates::

    # detector.yaml
    spec: {name: loki-calibrated-detector, version: 1}
    params: {run: 70001}

    # masks.yaml
    spec: {name: loki-masks, version: 1}
    params:
      detector: {'@latest': detector, output: detector}
      mask_files: [path:calibration/mask_new_July2022.xml]

and run two commands::

    B="essbatch -w FOLDER -b ess.instruments.loki.bindings:BINDINGS"
    $B reduce detector.yaml -l detector
    $B reduce masks.yaml -l masks

``essbatch reduce`` persists the outputs, so the page then offers the masks under
"Masks", "From a result". ``path:`` refers to a file by its path in the folder, as
``files.csv`` lists it. The page lists the detector and the masks as batches of one
row each. So it makes no batch of I(Q) at the start: make it with "New batch".

The proposal of the folder and where its runs are listed are in its
``workspace.json`` (:func:`ess.batch.read_workspace_header`). A folder whose runs
are the raw datasets of its proposal in SciCat, ``"files": "scicat:staging.ess"``
(:class:`~ess.apps.scicat.ScicatFiles`), opens only with ``--scicat staging.ess``,
which logs in at the start as :mod:`.scicat` describes. ``--no-local`` refuses a
folder whose runs are in ``files.csv``. SciCat states no role, so the Runs view
shows the runs without one until a person sets it. If the workspace has no batch
at the start, it gets a batch of the bank histogram
(:data:`~.specs.BANK_HISTOGRAM`), with a row per raw run. A run is read from the
path SciCat states, such as ``/ess/raw/...``. So reducing works only where that
path is mounted.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from ess.batch import (
    BatchManager,
    CorrectedFiles,
    FolderDefinitions,
    Intake,
    file_sources,
    open_workspace,
    read_workspace_header,
)
from ess.batch.web import Pages, create_app, plots, serve
from fastapi import FastAPI
from pydantic import BaseModel, Field

from ess.apps import Client

from .batch import SOURCES, STAGED
from .bindings import BINDINGS
from .specs import BANK_HISTOGRAM, IOFQ
from .writers import WRITERS


class Marks(BaseModel):
    """What a person may correct about a run in the Runs view."""

    role: Literal['sample', 'transmission', 'background', 'empty_beam'] | None = None
    sample: str | None = Field(default=None, title='Sample name')


FIRST_BATCHES = {
    'sample-runs': (IOFQ, 'Sample runs'),
    'raw-runs': (BANK_HISTOGRAM, 'Raw runs'),
}
"""The spec and title of the batch that a start makes, by the name of its source."""


def build(
    client: Client,
    files: CorrectedFiles,
    definitions: FolderDefinitions,
    *,
    folder: str = '',
    settings: Mapping[str, Any] | None = None,
    interval: float = 5.0,
    follow: bool = False,
    first: str = 'sample-runs',
) -> FastAPI:
    """
    Returns the app of the batch pages of ``client``.

    If the workspace has no batch, such as after a person removed the last one,
    ``build`` makes one from the source ``first``, with the rows complete now.

    Parameters
    ----------
    settings:
        The settings that a new batch of I(Q) starts with.
    interval:
        The seconds between two steps of Follow.
    follow:
        Whether the batch made from ``first`` follows.
    first:
        A key of :data:`FIRST_BATCHES`.
    """
    manager = BatchManager(
        client,
        definitions=definitions,
        settings={IOFQ.name: settings or {}},
        staged=STAGED,
    )
    intake = Intake(manager, sources=SOURCES)
    if not manager.headers():
        spec, title = FIRST_BATCHES[first]
        intake.create(spec.name, title=title, source=first, follow=follow)
    pages = Pages(
        intake,
        interval=interval,
        title='LoKI I(Q)',
        draw={
            IOFQ.name: plots.draw(client, 'iofq', 'I(Q)', lambda q: q.intensity),
            BANK_HISTOGRAM.name: plots.draw(client, 'histogram', 'Counts'),
        },
        files=files,
        marks=Marks,
        folder=folder,
        writers=WRITERS,
    )
    return create_app(pages)


def main() -> None:
    parser = argparse.ArgumentParser(description='Serve the LoKI batch page.')
    parser.add_argument('folder', type=Path, help='the workspace folder')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument(
        '--interval',
        type=float,
        default=5.0,
        metavar='SECONDS',
        help='a step of Follow every SECONDS (5)',
    )
    parser.add_argument(
        '--scicat',
        metavar='PROFILE',
        action='append',
        default=[],
        help="open a folder whose runs are in the SciCat of PROFILE, such as "
        "'staging.ess' (needs the extra scicat); may be given several times",
    )
    parser.add_argument(
        '--no-local',
        action='store_true',
        help='refuse a folder whose runs are in its files.csv',
    )
    args = parser.parse_args()
    offered = [] if args.no_local else ['local']
    offered += [f'scicat:{profile}' for profile in args.scicat]
    if not offered:
        parser.error('--no-local needs --scicat')
    try:
        allowed = file_sources(offered)
        client, files, definitions = open_workspace(
            args.folder, BINDINGS, allowed=allowed
        )
    except ValueError as error:  # such as definitions of another format
        parser.exit(1, f'{error}\n')
    local = read_workspace_header(args.folder).files == 'local'
    with client:
        app = build(
            client,
            files,
            definitions,
            folder=args.folder.resolve().name,
            interval=args.interval,
            first='sample-runs' if local else 'raw-runs',
        )
        serve(app, port=args.port)


if __name__ == '__main__':
    main()

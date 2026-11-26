# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page of LoKI's I(Q) on a workspace folder, as a web page
(:mod:`ess.batch.web`). It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web FOLDER [--port 8000] [--interval SECONDS]
        [--scicat PROFILE --proposal PROPOSAL]

``FOLDER`` is a workspace folder (:mod:`ess.apps.folder`): its ``files.csv``
says the run, role and sample of each file, as ``essbatch files`` writes
it. A row is a sample run, paired with the transmission run of its sample
(:func:`~.batch.pair_by_sample`). A start at which no batch is listed makes
a batch from the source of sample runs, with the rows that have both runs
then; it does not follow (but see :mod:`.arrivals`). The
batches with their batch tables and settings, the batch that follows, and the
corrections of runs are kept in ``FOLDER/definitions/``, the results in
``FOLDER/store/``. The tab "Output" writes I(Q) as NXcanSAS, or any output
as scipp HDF5 (:mod:`.writers`).

The settings name the masks by a record, so reduce the detector and the masks
first, on the same folder, while no page runs on it. With the run of the can
and a mask file of the folder, two templates::

    # detector.yaml
    spec: {name: loki-calibrated-detector, version: 1}
    params: {run: 70001}

    # masks.yaml
    spec: {name: loki-masks, version: 1}
    params:
      detector: {'@latest': detector, output: detector}
      mask_files: [path:calibration/mask_new_July2022.xml]

and two commands::

    B="essbatch -w FOLDER -b ess.instruments.loki.bindings:BINDINGS"
    $B reduce detector.yaml -l detector
    $B reduce masks.yaml -l masks

The command persists the outputs, so the page then offers the masks in the
settings, under "Masks", "From a result". ``path:`` names a file by its path in
the folder, as ``files.csv`` lists it. The page lists the detector and the
masks as batches without a source, of one row each, and so makes no batch of
I(Q) in such a folder: a person makes it with "New batch".

With ``--scicat``, the runs are the raw datasets of ``PROPOSAL`` in SciCat
(:class:`~ess.apps.scicat.ScicatFiles`) instead of ``files.csv``, such as
``--scicat staging.ess --proposal 336349``; the folder keeps the rest. The
start logs in to SciCat (``scitacean.Client.login``): it opens a browser on
this machine, or, with ``SCITACEAN_OAUTH_FLOW=device``, prints a URL to open
on any. The catalogue gives no role, so the Runs view shows the runs
without one until a person gives it, and a start at which no batch is listed
makes a batch of the bank histogram (:data:`~.specs.BANK_HISTOGRAM`) with a row
per raw run instead of the batch of sample runs. A run is read from where
SciCat says it lies, such as ``/ess/raw/...``, so reducing works only where
that is mounted.

"List runs again" lists the files again, for the Runs view and the choices of
a cell, and changes no row. A person makes a batch that follows the source
with "New batch", Follow ticked: from then on, a step every ``SECONDS`` (5)
adds each sample run that arrived since, once both its runs are listed, and
reduces it, until a person clicks "Stop following" or makes another batch
that follows. A step never changes a row that the batch table has: a newer
transmission run of its sample does not replace the one in its cell. A row
with a result waits for the person's "Reduce". The batch that follows is
kept, so a restart resumes. Without a page, ``essbatch -s
ess.instruments.loki.batch:SOURCES ... follow -l LABEL`` runs the steps.
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
    open_workspace,
)
from ess.batch.web import Pages, create_app, plots, serve
from fastapi import FastAPI
from pydantic import BaseModel, Field

from ess.apps import Client
from ess.apps.protocol import FileSource

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
    The pages of the batches of ``client``, kept in ``definitions``, with a
    step of Follow every ``interval`` seconds while a batch follows. A new
    batch of I(Q) starts with ``settings``. If no batch is listed, a batch is
    made from the source ``first`` (:data:`FIRST_BATCHES`), with its rows that are
    complete now, so also at a start after a person removed the last one; it
    follows from the start if ``follow``. A folder with only the
    detector and the masks that the command reduced has batches, so a person
    makes the batch of I(Q) with "New batch".
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
        help="list the runs of --proposal in SciCat, such as 'staging.ess' "
        '(needs the extra scicat)',
    )
    parser.add_argument('--proposal', help='the proposal of the runs in SciCat')
    args = parser.parse_args()
    if (args.scicat is None) != (args.proposal is None):
        parser.error('give --scicat and --proposal together')
    try:
        source, proposal = _scicat(args)
        client, files, definitions = open_workspace(
            args.folder, BINDINGS, source=source, proposal=proposal
        )
    except ValueError as error:  # such as definitions of another format
        parser.exit(1, f'{error}\n')
    with client:
        app = build(
            client,
            files,
            definitions,
            folder=args.folder.resolve().name,
            interval=args.interval,
            first='sample-runs' if source is None else 'raw-runs',
        )
        serve(app, port=args.port)


def _scicat(args: argparse.Namespace) -> tuple[FileSource | None, str]:
    """The file source and the proposal that the arguments give."""
    if args.scicat is None:
        return None, 'local'
    import scitacean

    from ess.apps.scicat import ScicatFiles  # needs the extra scicat

    client = scitacean.Client.login(args.scicat)
    return ScicatFiles(client, args.proposal), args.proposal


if __name__ == '__main__':
    main()

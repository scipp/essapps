# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page of LoKI's I(Q) on a workspace folder, as a web page
(:mod:`ess.apps.batch.web`). It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web FOLDER [--port 8000] [--interval SECONDS]

``FOLDER`` is a workspace folder (:mod:`ess.apps.folder`): its ``files.csv``
says the run, role and sample of each file, as ``essapps-batch files`` writes
it. A row is a sample run, paired with the transmission run of its sample
(:func:`~.batch.pair_by_sample`). A start at which no batch is listed makes
a batch from the source of sample runs, with the rows that have both runs
then; it does not follow (but see :mod:`.arrivals`). The
batches with their tables and settings, the batch that follows, and the
corrections of runs are kept in ``FOLDER/definitions/``, the results in
``FOLDER/store/``.

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

    B="essapps-batch -w FOLDER -b ess.instruments.loki.bindings:BINDINGS"
    $B reduce detector.yaml -l detector
    $B reduce masks.yaml -l masks

The command persists the outputs, so the page then offers the masks in the
settings, under "Masks", "From a result". ``path:`` names a file by its path in
the folder, as ``files.csv`` lists it. The page lists the detector and the
masks as batches without a source, of one row each, and so makes no batch of
I(Q) in such a folder: a person makes it with "New batch".

"List runs again" lists the files again, for the Runs view and the choices of
a cell, and changes no row. A person makes a batch that follows the source
with "New batch", Follow ticked: from then on, a step every ``SECONDS`` (5)
adds each sample run that arrived since, once both its runs are listed, and
reduces it, until a person clicks "Stop following" or makes another batch
that follows. A step never changes a row that the table has: a newer
transmission run of its sample does not replace the one in its cell. A row
with a result waits for the person's "Reduce". The batch that follows is
kept, so a restart resumes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from ess.apps import Client
from ess.apps.batch import BatchManager, CorrectedFiles, FolderDefinitions, Intake
from ess.apps.batch.web import Pages, Shown, create_app, plots
from ess.apps.batch.web.api import STATIC
from ess.apps.folder import ArrivingFiles, open_folder

from .batch import SOURCES, STAGED
from .bindings import BINDINGS
from .specs import IOFQ


class Marks(BaseModel):
    """What a person may correct about a run in the Runs view."""

    role: Literal['sample', 'transmission', 'background', 'empty_beam'] | None = None
    sample: str | None = Field(default=None, title='Sample name')


def open_loki(directory: Path) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the workspace in ``directory``, its file source with the
    corrections kept in the folder, and the folder's definitions. The file
    source reads ``files.csv`` again when it is replaced
    (:class:`~ess.apps.folder.ArrivingFiles`), so a run registered while the
    page runs is listed at the next "List runs again", and its row is added by a
    step of Follow.
    """
    definitions = FolderDefinitions(directory)
    files = CorrectedFiles(ArrivingFiles(directory), definitions=definitions)
    return open_folder(directory, BINDINGS, files=files), files, definitions


def _draw(client: Client) -> Callable[[Sequence[Shown]], dict[str, object]]:
    def draw(shown: Sequence[Shown]) -> dict[str, object]:
        curves = [
            plots.Curve(
                s.name, client.output(s.record, 'iofq').intensity, s.color, s.outdated
            )
            for s in shown
        ]
        return plots.figure(curves, quantity='I(Q)')

    return draw


def build(
    client: Client,
    files: CorrectedFiles,
    definitions: FolderDefinitions,
    *,
    folder: str = '',
    settings: Mapping[str, Any] | None = None,
    interval: float = 5.0,
    follow: bool = False,
) -> FastAPI:
    """
    The pages of the batches of ``client``, kept in ``definitions``, with a
    step of Follow every ``interval`` seconds while a batch follows. A new
    batch of I(Q) starts with ``settings``. If no batch is listed, a batch is
    made from the source of sample runs, with the rows it has filled now, so also
    at a start after a person removed the last one; it follows from the start
    if ``follow``. A folder with only the
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
        intake.create(
            IOFQ.name, title='Sample runs', source='sample-runs', follow=follow
        )
    pages = Pages(
        intake,
        interval=interval,
        title='LoKI I(Q)',
        draw={IOFQ.name: _draw(client)},
        files=files,
        marks=Marks,
        folder=folder,
    )
    return create_app(pages)


def main() -> None:
    import uvicorn

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
    args = parser.parse_args()
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    client, files, definitions = open_loki(args.folder)
    with client:
        folder = args.folder.resolve().name
        app = build(client, files, definitions, folder=folder, interval=args.interval)
        uvicorn.run(app, host='localhost', port=args.port)


if __name__ == '__main__':
    main()

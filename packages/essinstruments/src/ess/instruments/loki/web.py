# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page of LoKI's I(Q) on a workspace folder, as a web page
(:mod:`ess.apps.batch.web`). It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web FOLDER [--port 8000] [--interval SECONDS]

``FOLDER`` is a workspace folder (:mod:`ess.apps.folder`): its ``files.csv``
says the run, role and sample of each file, as ``essapps-batch files`` writes
it. A row is a sample run, paired with the transmission run of its sample
(:func:`~.batch.pair_by_sample`). A folder without a batch gets one of all
sample runs. The batches with their edits and settings, the batch that
follows, and the corrections of runs are kept in ``FOLDER/definitions/``, the
results in ``FOLDER/store/``.

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

The switch "Follow" of the page of a batch makes it the batch that follows:
a step every ``SECONDS`` (5) reduces each row once both its runs are listed,
and again when one of them changes, such as a newer transmission run of its
sample (:data:`~.batch.COLUMNS`). A row that a person changed waits for the
person's "Reduce". The batch that follows is kept, so a restart resumes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from ess.apps import Client
from ess.apps.batch import BatchManager, CorrectedFiles, FolderDefinitions
from ess.apps.batch.auto import Follow
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
    page runs shows at the next "Rescan runs".
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
    batch of I(Q) starts with ``settings``. If there is no batch at all, one
    of all sample runs is made, which follows from the start if ``follow``.
    A folder with only the detector and the masks that the command reduced
    has batches, so a person makes the batch of I(Q) with "New batch"; one
    that a person removed is not made again.
    """
    manager = BatchManager(
        client,
        sources=SOURCES,
        definitions=definitions,
        settings={IOFQ.name: settings or {}},
        staged=STAGED,
    )

    loop = Follow(manager, definitions, interval=interval)
    if not manager.headers():
        label = manager.create(IOFQ.name, title='Sample runs', source='sample-runs')
        if follow:
            loop.on(label)
    pages = Pages(
        manager,
        loop,
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

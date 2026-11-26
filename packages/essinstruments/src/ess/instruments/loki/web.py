# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Serves the batch pages of LoKI's I(Q) (:mod:`ess.batch.web`) on the workspaces of a
root folder. It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web ROOT [--port 8000] [--interval SECONDS]
        [--scicat PROFILE]... [--no-local]

``http://localhost:8000/`` lists the workspace folders in ``ROOT``
(:func:`ess.batch.workspaces`), makes a new one, and opens one at
``/workspaces/<name>/``. A workspace stays open until it is removed on that page
or the server stops. Before ``essbatch`` works on it, stop the server or close
the workspace (``POST api/workspaces/<name>/close``).

The proposal and the file source of a workspace are in its ``workspace.json``
(``packages/essbatch/docs/batch.md``, "Workspaces of a root folder"). By default
the server allows the file source ``local``: the ``files.csv`` of the folder lists
the run, role and sample of each file, as ``essbatch files`` writes it. A workspace
made on the page lists no run until then::

    cd ROOT/NAME
    essbatch files ess.instruments.loki.files:read_metadata -o files.csv

``--scicat staging.ess`` allows the file source ``scicat:staging.ess``: the raw
datasets of the proposal in that SciCat (:class:`~ess.apps.scicat.ScicatFiles`).
The server logs in to each profile at its start, as :mod:`.scicat` describes, and
does not log in again once the login expires. ``--no-local`` refuses local
workspaces, for a server that allows SciCat only. SciCat states no role, so the
Runs view shows the runs without one until a person sets it. A run is read from the
path SciCat states, such as ``/ess/raw/...``. So reducing works only where that path
is mounted.

A workspace opened with no batch gets one: of I(Q) for a local workspace, with a
row per sample run and the transmission run of its sample
(:func:`~.batch.pair_by_sample`); of the bank histogram
(:data:`~.specs.BANK_HISTOGRAM`) for one of SciCat, with a row per raw run. The tab
"Output" writes I(Q) as NXcanSAS (:mod:`.writers`), and any output as scipp HDF5.

The settings of I(Q) refer to the masks by a record. So reduce the detector and the
masks first, on the workspace, before the page opens it. With the run of the can
and a mask file of the folder, write two templates::

    # detector.yaml
    spec: {name: loki-calibrated-detector, version: 1}
    params: {run: 70001}

    # masks.yaml
    spec: {name: loki-masks, version: 1}
    params:
      detector: {'@latest': detector, output: detector}
      mask_files: [path:calibration/mask_new_July2022.xml]

and run two commands::

    B="essbatch -w ROOT/NAME -b ess.instruments.loki.bindings:BINDINGS"
    $B reduce detector.yaml -l detector
    $B reduce masks.yaml -l masks

``essbatch reduce`` persists the outputs, so the page then offers the masks under
"Masks", "From a result". ``path:`` refers to a file by its path in the folder, as
``files.csv`` lists it. The page lists the detector and the masks as batches of one
row each. So it makes no batch of I(Q) when it opens the workspace: make it with
"New batch".
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from ess.batch import (
    BatchManager,
    CorrectedFiles,
    FileSources,
    FolderDefinitions,
    Intake,
    WorkspaceHeader,
    file_sources,
)
from ess.batch.web import Launcher, Pages, create_launch_app, plots, serve
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
"""
The spec and title of the batch that opening a workspace makes, by the name of
its source.
"""


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
) -> Pages:
    """
    Returns the batch pages of ``client``.

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
    return pages


def launch(root: Path, allowed: FileSources, *, interval: float = 5.0) -> FastAPI:
    """
    Returns the app of the launch page of the workspaces of ``root``, which
    opens workspaces whose file source is in ``allowed``.
    """

    def pages(
        client: Client,
        files: CorrectedFiles,
        definitions: FolderDefinitions,
        header: WorkspaceHeader,
    ) -> Pages:
        return build(
            client,
            files,
            definitions,
            folder=header.header.title,
            interval=interval,
            first='sample-runs' if header.files == 'local' else 'raw-runs',
        )

    launcher = Launcher(root, pages, bind=BINDINGS, allowed=allowed, title='LoKI I(Q)')
    return create_launch_app(launcher)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description='Serve the LoKI batch pages.')
    parser.add_argument('root', type=Path, help='the folder of the workspaces')
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
        help="allow the file source scicat:PROFILE, the raw datasets of the "
        "proposal in the SciCat of PROFILE, such as 'staging.ess' (needs the extra "
        "scicat); may be given several times",
    )
    parser.add_argument(
        '--no-local',
        action='store_true',
        help='do not allow the file source local, the files.csv of the folder',
    )
    args = parser.parse_args(argv)
    names = [] if args.no_local else ['local']
    names += [f'scicat:{profile}' for profile in args.scicat]
    if not names:
        parser.error('--no-local needs --scicat')
    allowed = file_sources(names)
    if args.scicat:  # now, not at the first request of a workspace
        from ess.apps.scicat import login  # needs the extra scicat

        profile = ''
        try:
            for profile in args.scicat:
                login(profile)
        except Exception as error:  # scitacean raises many kinds
            parser.exit(1, f'Cannot log in to SciCat {profile}: {error}\n')
    serve(launch(args.root, allowed, interval=args.interval), port=args.port)


if __name__ == '__main__':
    main()

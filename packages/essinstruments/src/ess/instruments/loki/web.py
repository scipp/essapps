# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page of LoKI's I(Q) on a workspace folder, as a web page
(:mod:`ess.apps.batch.web`). It needs the extra ``[loki-app]``::

    python -m ess.instruments.loki.web FOLDER [--port 8000]

``FOLDER`` is a workspace folder (:mod:`ess.apps.folder`): its ``files.csv``
says the run, role and sample of each file, as ``essapps-batch files`` writes
it. A row is a sample run, paired with the transmission run of its sample
(:func:`~.batch.pair_by_sample`). The settings name the masks by a record, so
reduce the detector and the masks first, with ``essapps-batch`` on the same
folder. The edits, the settings and the corrections of runs are kept in
``FOLDER/definitions/``, the results in ``FOLDER/store/``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

from ess.apps import Client
from ess.apps.batch import CorrectedFiles, FolderDefinitions
from ess.apps.batch.web import Shown, create_app, plots
from ess.apps.batch.web.api import STATIC
from ess.apps.batch.web.models import Words
from ess.apps.folder import folder_files, open_folder

from .batch import iofq_batch
from .bindings import BINDINGS


class Marks(BaseModel):
    """What a person may correct about a run in the Runs view."""

    role: Literal['sample', 'transmission', 'background', 'empty_beam'] | None = None
    sample: str | None = Field(default=None, title='Sample name')


def open_loki(directory: Path) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the workspace in ``directory``, its file source with the
    corrections kept in the folder, and the folder's definitions.
    """
    definitions = FolderDefinitions(directory)
    files = CorrectedFiles(folder_files(directory), definitions=definitions)
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
) -> FastAPI:
    """
    The page of the I(Q) batch of ``client``, labelled ``iofq``, scanned. The
    batch persists its results and keeps its definition in ``definitions``; it
    starts with ``settings`` unless the definitions hold some.
    """
    batch = iofq_batch(client, persist=True, settings=settings, definitions=definitions)
    batch.scan()
    return create_app(
        batch,
        title='LoKI I(Q)',
        words=Words(one='sample run', many='sample runs'),
        name='name',
        draw=_draw(client),
        files=files,
        marks=Marks,
        folder=folder,
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description='Serve the LoKI batch page.')
    parser.add_argument('folder', type=Path, help='the workspace folder')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    client, files, definitions = open_loki(args.folder)
    with client:
        app = build(client, files, definitions, folder=args.folder.resolve().name)
        uvicorn.run(app, host='localhost', port=args.port)


if __name__ == '__main__':
    main()

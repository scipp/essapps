# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The LoKI batch page (:mod:`.web`) on a new folder into which runs arrive over
time, as if the instrument wrote them. It needs the extra ``[loki-app]`` and
esssans' tutorial data, which ``ess.loki.data`` downloads at the first start::

    python -m ess.instruments.loki.arrivals [FOLDER] [--port 8000] [--arrive 15]
        [--count N] [--start M] [--follow [SECONDS]]

``FOLDER`` must be new or empty; a new temporary folder if not given. The can
and its transmission run, the masks and the direct beam are there from the
start, and the settings of the one batch are filled in. Then a run arrives
every ``--arrive`` seconds, in the order of :data:`EXPERIMENT`: ``--count``
runs in all (12), the experiment again with the samples named ``#2``, ``#3``...
beyond that, and the first ``--start`` of them (0) there from the start, in the
first batch, "Sample runs". With ``--follow``, that batch is made to follow:
a step every ``SECONDS`` (5) adds the rows that the runs fill and reduces
them. Without it, the batch does not follow, and a person makes a batch that
follows with "New batch", Follow ticked. "List runs again" lists the runs in
the Runs view and changes no row.

The data and the I(Q) workflow are real; the run numbers, the sample names and
the roles are this module's. A run arrives as a hard link to a tutorial file
(a copy if the folder is on another file system) under ``raw/<run>.nxs``, then
as a row of ``files.csv`` with its run, role and sample, which
:class:`~ess.apps.folder.ArrivingFiles` reads again.
"""

from __future__ import annotations

import argparse
import functools
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ess.batch.web.api import STATIC, every
from ess.loki import data

from ess.apps import Client, file
from ess.apps.folder import write_files_table
from ess.spec import FileRef

from .specs import CALIBRATED_DETECTOR, MASKS
from .web import build, open_loki

FIRST_RUN = 70001

TUTORIAL: dict[int, Callable[[], Any]] = {
    60387: data.loki_tutorial_agbeh_sample_run,
    60386: data.loki_tutorial_agbeh_transmission_run,
    60385: data.loki_tutorial_porous_silica_sample_run,
    60384: data.loki_tutorial_porous_silica_transmission_run,
    60395: data.loki_tutorial_isis_polymer_sample_run,
    60394: data.loki_tutorial_sample_transmission_run,
    60389: data.loki_tutorial_deut_sds_sample_run,
    60388: data.loki_tutorial_deut_sds_transmission_run,
    60339: data.loki_tutorial_sample_run_60339,
    60250: data.loki_tutorial_sample_run_60250,
    60393: data.loki_tutorial_background_run_60393,
    60392: data.loki_tutorial_run_60392,
}
"""The runs of esssans' LoKI tutorial (Larmor, 2022) that the arrivals copy."""

EXPERIMENT = (
    # sample, role, tutorial run, in the order of arrival
    ('AgBeh', 'sample', 60387),
    ('AgBeh', 'transmission', 60386),
    ('porous silica', 'sample', 60385),
    ('porous silica', 'transmission', 60384),
    ('ISIS polymer', 'sample', 60395),
    ('ISIS polymer', 'transmission', 60394),
    ('d-SDS', 'sample', 60389),
    ('d-SDS', 'transmission', 60388),
    ('tutorial sample', 'sample', 60339),
    ('tutorial sample', 'transmission', 60394),
    ('tutorial sample', 'sample', 60250),  # measured again: a second row
    ('tutorial sample', 'transmission', 60394),  # a newer transmission run
)
"""
The runs that arrive. A sample run waits until a transmission run of its
sample arrives, and Follow then adds its row. The last transmission run changes
no row: the two rows of ``tutorial sample`` keep the transmission run they were
added with.
"""

BEAM_CENTRE = {'x': -0.02914868, 'y': -0.01816138, 'z': 4.11610}
"""The beam centre that esssans' tutorial holds for these runs."""


def arrivals(count: int) -> list[tuple[str, str, int]]:
    """
    The first ``count`` arrivals: :data:`EXPERIMENT`, then again with the
    samples named ``#2``, ``#3``...
    """
    plan = []
    for i in range(count):
        sample, role, source = EXPERIMENT[i % len(EXPERIMENT)]
        repeat = i // len(EXPERIMENT)
        plan.append((f'{sample} #{repeat + 1}' if repeat else sample, role, source))
    return plan


def _place(source: Path, target: Path) -> None:
    """
    ``source`` at ``target``: a hard link, or a copy on another file system. A
    symbolic link would not do: the file source resolves it, so two runs would
    be one file.
    """
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


class Instrument:
    """
    Writes runs into ``directory`` and registers each in its ``files.csv``,
    which it replaces at once, so that no reader sees half of it. It starts
    with the masks, the direct beam, the can, and the can's transmission run,
    which is also the empty-beam run.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.rows: list[dict[str, Any]] = []
        self.next_run = FIRST_RUN
        (directory / 'raw').mkdir(parents=True)
        (directory / 'calibration').mkdir()
        mask = Path(data.loki_tutorial_mask_filenames()[0])
        beam = Path(data.loki_tutorial_direct_beam_all_pixels())
        for source, row in (
            (mask, {'kind': 'mask'}),
            (beam, {'kind': 'calibration', 'role': 'direct_beam'}),
        ):
            name = f'calibration/{source.name}'
            shutil.copyfile(source, directory / name)
            self.rows.append({'file': name, **row})
        self.mask = directory / 'calibration' / mask.name
        self.direct_beam = directory / 'calibration' / beam.name
        self.can = self.measure('background', 'empty can', 60393)
        self.empty_beam = self.measure(None, None, 60392)

    def measure(self, role: str | None, sample: str | None, source: int) -> FileRef:
        """A new run: the data of tutorial run ``source``, under the next number."""
        run, self.next_run = self.next_run, self.next_run + 1
        name = f'raw/{run}.nxs'
        _place(Path(TUTORIAL[source]()), self.directory / name)
        self.rows.append(
            {'file': name, 'run': run, 'kind': 'raw', 'role': role, 'sample': sample}
        )
        write_files_table(self.directory, self.rows)
        return FileRef(file=f'path:{name}')


def settings(client: Client, instrument: Instrument) -> dict[str, Any]:
    """
    The settings of I(Q) for the runs of ``instrument``: the detector and its
    masks, reduced and persisted here, and the beam centre of the tutorial.
    """
    detector = client.submit(
        CALIBRATED_DETECTOR, params={'run': instrument.can}, persist=True
    )
    masks = client.submit(
        MASKS,
        params={
            'detector': detector.ref('detector'),
            'mask_files': [file(path=instrument.mask)],
        },
        label='masks',
        persist=True,
    )
    if set(map(str, client.wait([detector, masks]))) != {'completed'}:
        raise RuntimeError(f'detector and masks: {client.failure([detector, masks])}')
    return {
        'can': instrument.can,
        'can_transmission': instrument.empty_beam,
        'empty_beam': instrument.empty_beam,
        'direct_beam': file(path=instrument.direct_beam),
        'masks': masks.ref('masks'),
        'beam_centre': BEAM_CENTRE,
        'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
        'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
    }


def _say(text: str) -> None:
    sys.stdout.write(f'{datetime.now().astimezone():%H:%M:%S}  {text}\n')
    sys.stdout.flush()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        description='Serve the LoKI batch page on a folder into which runs arrive.'
    )
    parser.add_argument('folder', type=Path, nargs='?', help='a new or empty folder')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument(
        '--arrive',
        type=float,
        default=15.0,
        metavar='SECONDS',
        help='a run every SECONDS',
    )
    parser.add_argument('--count', type=int, default=len(EXPERIMENT), help='runs')
    parser.add_argument(
        '--start', type=int, default=0, metavar='M', help='runs there at the start'
    )
    parser.add_argument(
        '--follow',
        type=float,
        nargs='?',
        const=5.0,
        metavar='SECONDS',
        help='make the first batch follow the files, a step every SECONDS (5)',
    )
    args = parser.parse_args()
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    directory = args.folder or Path(tempfile.mkdtemp(prefix='loki-arrivals-'))
    if directory.exists() and any(directory.iterdir()):
        parser.exit(1, f'{directory} is not empty\n')
    instrument = Instrument(directory)
    client, files, definitions = open_loki(directory)
    _say(f'folder {directory}; reducing the detector and the masks')
    with client:
        given = settings(client, instrument)

        def arrive(sample: str, role: str, source: int) -> None:
            ref = instrument.measure(role, sample, source)
            name = ref.file.removeprefix('path:')
            _say(f'arrived {name}: {role} run of {sample!r}, data of {source}')

        plan = arrivals(args.count)
        for arrival in plan[: args.start]:
            arrive(*arrival)
        app = build(
            client,
            files,
            definitions,
            folder=directory.name,
            settings=given,
            interval=args.follow or 5.0,
            follow=args.follow is not None,
        )

        later = [functools.partial(arrive, *a) for a in plan[args.start :]]
        _say(f'serving http://localhost:{args.port}/; Ctrl-C stops')
        with every(args.arrive, later):
            uvicorn.run(app, host='localhost', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()

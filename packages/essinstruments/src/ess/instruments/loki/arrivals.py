# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The LoKI batch page (:mod:`.web`) on a new folder into which runs arrive over
time, as if the instrument wrote them. It needs the extra ``[loki-app]`` and
esssans' tutorial data, which ``ess.loki.data`` downloads at the first start::

    python -m ess.instruments.loki.arrivals [FOLDER] [--port 8000] [--interval 15]

``FOLDER`` must be new or empty; a new temporary folder if not given. The can
and its transmission run, the masks and the direct beam are there from the
start, and the settings are filled in. Then a run arrives every ``--interval``
seconds, in the order of :data:`EXPERIMENT`. Press "Rescan runs" to see it, and
"Reduce" to reduce the rows it completes.

The data and the I(Q) workflow are real; the run numbers, the sample names and
the roles are this module's. A run arrives as a hard link to a tutorial file
(a copy if the folder is on another file system) under ``raw/<run>.nxs``, then
as a row of ``files.csv`` with its run, role and sample, which
:class:`ArrivingFiles` reads again.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ess.loki import data

from ess.apps import Client, FolderFiles, file
from ess.apps.batch import CorrectedFiles, FolderDefinitions
from ess.apps.batch.web.api import STATIC
from ess.apps.files import Selector
from ess.apps.folder import folder_files, open_folder
from ess.spec import FileRef

from .bindings import BINDINGS
from .specs import CALIBRATED_DETECTOR, MASKS
from .web import build

FIRST_RUN = 70001
_FIELDS = ('file', 'run', 'kind', 'role', 'sample')

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
The runs that arrive. A sample run makes an incomplete row, and the
transmission run of its sample completes it. The last transmission run changes
both rows of ``tutorial sample``.
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


class ArrivingFiles:
    """
    The files that ``files.csv`` in ``directory`` lists (:func:`folder_files`),
    read again whenever the file was replaced or changed.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._files: FolderFiles | None = None

    def _current(self) -> FolderFiles:
        stat = (self._directory / 'files.csv').stat()
        stamp = (stat.st_ino, stat.st_mtime_ns)
        with self._lock:
            if self._files is None or stamp != self._stamp:
                self._files = folder_files(self._directory)
                self._stamp = stamp
            return self._files

    def resolve(self, name: FileRef) -> FileRef:
        return self._current().resolve(name)

    def path(self, ref: FileRef) -> Path:
        return self._current().path(ref)

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return self._current().metadata(ref)

    def list(self, selector: Selector) -> list[FileRef]:
        return self._current().list(selector)


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
        text = io.StringIO()
        writer = csv.DictWriter(text, _FIELDS, lineterminator='\n')
        writer.writeheader()
        writer.writerows([{k: r.get(k) or '' for k in _FIELDS} for r in self.rows])
        partial = self.directory / 'files.csv.partial'
        partial.write_text(text.getvalue())
        os.replace(partial, self.directory / 'files.csv')
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
    parser.add_argument('--interval', type=float, default=15.0, help='seconds')
    parser.add_argument('--count', type=int, default=len(EXPERIMENT), help='runs')
    args = parser.parse_args()
    if not (STATIC / 'vendor').is_dir():
        parser.exit(1, f'The page needs its libraries: sh {STATIC / "vendor.sh"}\n')
    directory = args.folder or Path(tempfile.mkdtemp(prefix='loki-arrivals-'))
    if directory.exists() and any(directory.iterdir()):
        parser.exit(1, f'{directory} is not empty\n')
    instrument = Instrument(directory)
    definitions = FolderDefinitions(directory)
    files = CorrectedFiles(ArrivingFiles(directory), definitions=definitions)
    _say(f'folder {directory}; reducing the detector and the masks')
    with open_folder(directory, BINDINGS, files=files) as client:
        given = settings(client, instrument)
        app = build(client, files, definitions, folder=directory.name, settings=given)
        stop = threading.Event()

        def arrive() -> None:
            for sample, role, source in arrivals(args.count):
                if stop.wait(args.interval):
                    return
                ref = instrument.measure(role, sample, source)
                name = ref.file.removeprefix('path:')
                _say(f'arrived {name}: {role} run of {sample!r}, data of {source}')
            _say('all runs arrived')

        thread = threading.Thread(target=arrive, name='arrivals')
        thread.start()
        _say(f'serving http://localhost:{args.port}/; Ctrl-C stops')
        try:
            uvicorn.run(app, host='localhost', port=args.port, log_level='warning')
        finally:
            stop.set()
            thread.join()


if __name__ == '__main__':
    main()

"""
LoKI's automatic reduction written with LoKI's batch (ess.instruments.loki.batch)
on the real workflows and esssans' tutorial files, which arrive one by one.

Configuration in three layers: the instrument's settings, the proposal's (can,
empty beam, masks), and a part of the proposal (the AgBeh runs, another Q
range) that is a second batch with its own label and source.
"""

import time
from pathlib import Path

from ess.instruments.loki.batch import iofq_batch, pair_by_sample
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.specs import CALIBRATED_DETECTOR, MASKS
from ess.loki import data
from rules_common import auto_step

from ess.apps import FolderFiles, Selector, file, local
from ess.apps.batch import FileInfo, Row, summary


class Arriving(FolderFiles):
    """A folder whose runs appear when ``arrive`` is called."""

    def __init__(self, directory, table):
        super().__init__(directory, table)
        self.visible = {r for r, m in self._metadata.items() if 'run' not in m}

    def arrive(self, run: int) -> None:
        self.visible.add(self.resolve(file(run=run)))

    def list(self, selector: Selector):
        return [r for r in super().list(selector) if r in self.visible]


folder = Path(data.loki_tutorial_sample_run_60339()).parent
mask = Path(data.loki_tutorial_mask_filenames()[0])
direct_beam = Path(data.loki_tutorial_direct_beam_all_pixels())


def run(n: int, **metadata):
    return {'file': f'{n}-2022-02-28_2215.nxs', 'run': n, **metadata}


files = Arriving(folder, [
    run(60392, role='empty_beam'),
    run(60393, role='background'),
    run(60394, role='transmission', sample='water'),
    run(60339, role='sample', sample='water'),
    run(60250, role='sample', sample='water'),
    run(60386, role='transmission', sample='agbeh'),
    run(60387, role='sample', sample='agbeh'),
    {'file': mask.name, 'kind': 'mask'},
    {'file': direct_beam.name, 'kind': 'calibration', 'role': 'direct_beam'},
])

# Layer 1, the instrument: what every proposal starts from.
INSTRUMENT = {
    'direct_beam': file(path=direct_beam),
    'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
    'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
    'beam_centre': {'x': -0.02914868, 'y': -0.01816138, 'z': 4.11610},
}
# Layer 2, the proposal: its can, empty beam, and masks.
PROPOSAL = {
    'can': file(run=60393),
    'can_transmission': file(run=60392),
    'empty_beam': file(run=60392),
}
# Layer 3, a part of the proposal: the AgBeh runs, with another Q range.
PARTS = {'agbeh': {'q': {'start': 0.02, 'stop': 0.3, 'num_bins': 50}}}


def only(names: set[str], keep: bool):
    def source(infos: list[FileInfo]) -> list[Row]:
        return [r for r in pair_by_sample(infos) if (r.info.get('name') in names) == keep]
    return source


with local(proposal='p1', files=files, bind=BINDINGS) as client:
    for n in (60392, 60393, 60339):
        files.arrive(n)
    # Preparatory records of the proposal: computed once, referenced by every row.
    detector = client.submit(CALIBRATED_DETECTOR, params={'run': file(run=60339)})
    masks = client.submit(MASKS, params={'detector': detector.ref('detector'),
                                         'mask_files': [file(path=mask)]})
    PROPOSAL['masks'] = masks.ref('masks')

    main = iofq_batch(client, label='auto/iofq')
    main.source = only(set(PARTS), keep=False)
    agbeh = iofq_batch(client, label='auto/iofq/agbeh')
    agbeh.source = only({'agbeh'}, keep=True)
    batches = [(main, INSTRUMENT | PROPOSAL),
               (agbeh, INSTRUMENT | PROPOSAL | PARTS['agbeh'])]

    def step(note: str) -> None:
        t0 = time.perf_counter()
        out = {b.label: auto_step(b, s, at='sample') for b, s in batches}
        records = [r for done, _ in out.values() for r in done.values()]
        client.wait(records)
        print(f'{note:34}', {k: (sorted(d), r) for k, (d, r) in out.items()},
              {b.label: summary(b.state()) for b, _ in batches},
              f'{time.perf_counter() - t0:.1f} s')

    step('60339 arrives, no transmission')
    files.arrive(60394)
    step('60394, its transmission')
    files.arrive(60250)
    files.arrive(60387)
    step('60250 (water), 60387 (agbeh)')
    files.arrive(60386)
    step('60386, the agbeh transmission')
    step('nothing new')
    for b, _ in batches:
        b.close()

"""
Where ``arrived(at)`` of the spike and the branch's "a new file at ``at``"
pick different rows. Branch: a file at ``at`` that no record of the key holds.
Spike: the cell at ``at`` differs from that of the key's newest record.

Run: .scratch/venv-container/bin/python .scratch/one-interface/differs.py
"""

from typing import Any

from common import Proposal, say, stories

from ess.apps.batch import Batch as BranchBatch
from ess.apps.batch import Row
from ess.apps.batch.auto import Automatic as BranchAutomatic
from one import Automatic, Batch, arrived

bad: set[int] = set()  # runs that a person marked as not to use


def angles(files: Any) -> list[Row]:
    """A row per sample: all its runs so far that are not marked bad."""
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        if f.metadata.get('role') == 'sample' and f.metadata['run'] not in bad:
            runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(sample, {'runs': refs}) for sample, refs in runs.items()]


def run(name: str) -> None:
    bad.clear()
    p = Proposal()
    client = p.open()
    reference = p.measure(1, [1.0, 1.0], role='reference')
    options: dict[str, Any] = {
        'columns': ('runs',), 'label': 'r', 'source': angles, 'persist': True}  # fmt: skip
    settings = {'reference': reference}
    if name == 'branch':
        batch: Any = BranchBatch(client, stories.STITCH, **options)
        auto: Any = BranchAutomatic(batch, at='runs', settings=lambda: settings)
    else:
        batch = Batch(
            client, stories.STITCH, resolve=p.resolve(stories.STITCH), **options
        )
        auto = Automatic(batch, runs=arrived('runs'), settings=lambda: settings)
    for n in (2, 3, 4):
        p.measure(n, [1.0, float(n)], role='sample', sample='si')
    client.wait(auto.step())

    def held() -> list[int]:
        newest = client.latest('r', member='si')
        return [client.files.metadata(f)['run'] for f in newest.request.params['runs']]

    bad.add(3)  # run 3 leaves the list
    client.wait(auto.step())
    say(f'{name}: run 3 marked bad', 'the newest result holds runs', held())
    bad.clear()  # and comes back
    client.wait(auto.step())
    say(f'{name}: run 3 back', 'the newest result holds runs', held())
    p.close()


run('branch')
run('one')

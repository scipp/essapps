"""
Story E1: a growing series is reduced again at every new run; a run that
arrives again is not; then a restart of the process, and a new angle after it.
Once with the obvious loop (reduce remaining, stale by settings and cells),
once with "handled at runs".
"""

from rules_common import Batch, Proposal, auto_step, naive_step, runs, show, toys

from ess.apps.batch import FileInfo, Row


def angles(files: list[FileInfo]) -> list[Row]:
    """A row per sample: all its angles so far, in run order."""
    by_sample: dict[str, list[FileInfo]] = {}
    for f in files:
        if f.metadata.get('role') == 'sample':
            by_sample.setdefault(f.metadata['sample'], []).append(f)
    return [
        Row(name, {'runs': [f.ref for f in sorted(fs, key=lambda f: f.metadata['run'])]})
        for name, fs in by_sample.items()
    ]


for loop in ('obvious', 'handled'):
    p = Proposal()
    client = p.open()
    settings = {'reference': p.measure(1, [1.0, 1.0], role='reference')}

    def make(client):
        return Batch(client, toys.STITCH, columns=('runs',), label='reflectivity',
                     source=angles, persist=True)

    def step(batch):
        if loop == 'obvious':
            out = naive_step(batch, settings)
        else:
            out = auto_step(batch, settings, at='runs')[0]
        show(batch.client, out)
        return {k: runs(batch.client, r, 'runs') for k, r in out.items()}

    batch = make(client)
    p.measure(2, [1.0, 2.0], role='sample', sample='si')
    s1 = step(batch)
    p.measure(4, [4.0, 8.0], role='sample', sample='si')
    p.measure(3, [1.0, 1.0], role='sample', sample='si')
    p.measure(2, [1.0, 2.0], role='sample', sample='si')  # arrives again
    s2 = step(batch)
    records = client.records(label='reflectivity')
    curve = client.output(records[-1], 'stitched').curve.values.tolist()
    s3 = step(batch)  # nothing new

    client = p.restart()
    batch = make(client)
    s4 = step(batch)  # after the restart, nothing new
    p.measure(5, [2.0, 2.0], role='sample', sample='si')
    s5 = step(batch)  # a new angle after the restart
    print(f'{loop:8} steps: {s1} {s2} {s3} | restart | {s4} new angle 5: {s5}')
    print(f'{"":8} records {len(records)}, curve {curve}')
    print(f'{"":8} state of si after the restart:', batch.state(settings)['si'].text)
    p.close()

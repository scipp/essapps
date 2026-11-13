"""
One step of ess.apps.batch.auto.Automatic with one new file, N runs already
reduced, as rules_scale_changed.py measures the prototype. Also the parts of a
step: listing the files (file_infos), the scan (listing, source, rescan).
Run: .scratch/venv-container/bin/python .scratch/next-steps/auto_scale.py 3000 30000 100000
"""

import sys
import time

from rules_common import Proposal, toys

from ess.apps.batch import Batch, FileInfo, Row
from ess.apps.batch.auto import Automatic
from ess.apps.batch.reduction import file_infos


def pair(files: list[FileInfo]) -> list[Row]:
    """As loki's pair_by_sample: a join over all files."""
    transmissions = {}
    for f in files:
        if f.metadata.get('role') == 'transmission':
            transmissions[f.metadata['sample']] = f.ref
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        name = f.metadata.get('sample')
        info = {'name': str(name), 'transmission': str(transmissions.get(name))}
        rows.append(Row(str(f.metadata['run']), {'sample': f.ref}, info=info))
    return rows


def best(fn, repeat=3):
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        t = time.perf_counter() - t0
        out = t if out is None else min(out, t)
    return out


for n in map(int, sys.argv[1:]):
    p = Proposal()
    client = p.open()
    for i in range(n):
        p.measure(i, [1.0, 2.0], role='sample', sample=f's{i}')
        if i % 10 == 0:
            p.measure(n + i, [1.0], role='transmission', sample=f's{i}')
    batch = Batch(client, toys.IOFQ, columns=('sample',), label='iofq', source=pair,
                  persist=True)
    auto = Automatic(batch, at='sample', settings=lambda: {'normalization': None})
    t0 = time.perf_counter()
    first = auto.step()
    client.wait(list(first.values()))
    fill = time.perf_counter() - t0
    assert len(first) == n
    total, times = None, []
    for j in range(10):
        p.measure(3 * n + j, [1.0, 2.0], role='sample', sample=f'new{j}')
        t0 = time.perf_counter()
        done = auto.step()
        t = time.perf_counter() - t0
        assert list(done) == [str(3 * n + j)], done
        total = t if total is None else min(total, t)
        times.append(t)
        client.wait(list(done.values()))
    auto.step()  # reads the finished records
    t0 = time.perf_counter()
    assert auto.step() == {}
    idle = time.perf_counter() - t0
    listing = best(lambda: file_infos(client))
    scan = best(batch.scan)
    state = best(lambda: batch.state(keys=['0']))
    print(f'N={n} ({len(file_infos(client))} files, first step {fill:.1f} s); '
          f'one new file: best {total * 1e3:.0f} ms, mean {sum(times) / len(times) * 1e3:.0f}, of which scan {scan * 1e3:.0f} '
          f'(listing {listing * 1e3:.0f}); state of one row {state * 1e3:.1f}; '
          f'idle step {idle * 1e3:.0f} ms', flush=True)
    p.close()

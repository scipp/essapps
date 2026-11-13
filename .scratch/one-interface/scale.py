"""
Cost at N runs already reduced, for the branch (``branch``) and the spike
(``one``): a step of the automatic reduction with one new file, a step with
nothing new, the first step after a restart, and the state of every row with
settings, which a page shows.

Run: .scratch/venv-container/bin/python .scratch/one-interface/scale.py branch 3000 30000 100000
     .scratch/venv-container/bin/python .scratch/one-interface/scale.py one 3000 30000 100000
"""

import sys
import time
from typing import Any

from common import Proposal, stories

from ess.apps.batch import Row

which, *sizes = sys.argv[1:]
SETTINGS = {'normalization': None}
OTHER = {'reduction': {'threshold': 1.5}, 'normalization': None}


def pair(files: Any) -> list[Row]:
    """As LoKI's pair_by_sample: a join over all files."""
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


def make(p: Proposal, client: Any) -> tuple[Any, Any]:
    options: dict[str, Any] = {
        'columns': ('sample',), 'label': 'iofq', 'source': pair, 'persist': True}  # fmt: skip
    if which == 'branch':
        from ess.apps.batch import Batch
        from ess.apps.batch.auto import Automatic

        batch = Batch(client, stories.IOFQ, **options)
        return batch, Automatic(batch, at='sample', settings=lambda: SETTINGS)
    from one import Automatic, Batch, arrived

    batch = Batch(client, stories.IOFQ, resolve=p.resolve(stories.IOFQ), **options)
    return batch, Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)


def timed(fn: Any) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1e3, out


for n in map(int, sizes):
    p = Proposal()
    client = p.open()
    for i in range(n):
        p.measure(i, [1.0, 2.0], role='sample', sample=f's{i}')
        if i % 10 == 0:
            p.measure(n + i, [1.0], role='transmission', sample=f's{i}')
    batch, auto = make(p, client)
    fill, first = timed(auto.step)
    client.wait(list(first.values()))
    assert len(first) == n
    auto.step()  # reads the finished records
    times = []
    for j in range(10):
        p.measure(3 * n + j, [1.0, 2.0], role='sample', sample=f'new{j}')
        t, done = timed(auto.step)
        assert list(done) == [str(3 * n + j)], done
        times.append(t)
        client.wait(list(done.values()))
    auto.step()
    idle = min(timed(auto.step)[0] for _ in range(3))
    one_row = min(timed(lambda: batch.state(keys=['0']))[0] for _ in range(3))
    page = [timed(lambda: batch.state(SETTINGS))[0] for _ in range(3)]
    changed = [timed(lambda: batch.state(OTHER))[0] for _ in range(2)]

    client = p.restart()
    batch, auto = make(p, client)
    restart, done = timed(auto.step)
    assert done == {}
    page_restart = timed(lambda: batch.state(SETTINGS))[0]
    print(
        f'{which} N={n}: fill {fill / 1e3:.1f} s; one new file: best {min(times):.0f} ms, '
        f'mean {sum(times) / len(times):.0f}; idle step {idle:.0f}; '
        f'state of one row {one_row:.2f}; state of all rows with settings: '
        f'{page[0]:.0f}, then {min(page[1:]):.0f}; after a changed setting '
        f'{changed[0]:.0f}, then {changed[1]:.0f}; restart: first step {restart:.0f}, '
        f'state of all rows {page_restart:.0f}',
        flush=True,
    )
    p.close()

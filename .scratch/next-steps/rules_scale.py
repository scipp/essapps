"""
Cost of one step of the automatic reduction with N runs already reduced and
one new file, by part, for the batch chain (recompute all rows, join with the
records) and for the trigger loop. Toy workflow; times are the best of 3.
"""

import sys
import time

from rules_common import Batch, Proposal, files_at, reduce_all_but_refused, toys

from ess.apps import Rule, Selector, TriggerLoop
from ess.apps.batch import FileInfo, Row, ready
from ess.apps.batch.reduction import file_infos
from ess.spec.refs import iter_refs


def per_run(files: list[FileInfo]) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files
            if f.metadata.get('role') == 'sample']


def submit(client, ref):
    member = str(client.files.metadata(ref)['run'])
    return client.submit(toys.IOFQ, params={'sample': ref, 'normalization': None},
                         label='iofq', member=member, persist=True)


def best(f, repeat=3):
    times = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        f()
        times.append(time.perf_counter() - t0)
    return min(times) * 1e3


def by_key(batch, at, states):
    newest = {r.member: r for r in batch.client.records(label=batch.label) if r.member}
    out = []
    for k in ready(states):
        cell = {ref for _, ref in iter_refs({at: batch.rows[k].values.get(at)})}
        if cell - (files_at(newest[k], at) if k in newest else set()):
            out.append(k)
    return out


settings = {'normalization': None}
for n in map(int, sys.argv[1:]):
    p = Proposal()
    client = p.open()
    for i in range(n):
        p.measure(i, [1.0, 2.0], role='sample')
    batch = Batch(client, toys.IOFQ, columns=('sample',), label='iofq', source=per_run,
                  persist=True)
    batch.scan()
    t0 = time.perf_counter()
    done, _ = reduce_all_but_refused(batch, settings, by_key(batch, 'sample', batch.state()))
    client.wait(list(done.values()))
    t_fill = time.perf_counter() - t0
    loop = TriggerLoop(client, [Rule('iofq', submit, Selector(role='sample'), 'iofq', 'sample')])
    loop.step()

    infos = file_infos(client)
    states = batch.state()
    parts = {
        'list files + metadata': best(lambda: file_infos(client)),
        'source(files)': best(lambda: per_run(infos)),
        'rescan (keys unique)': best(lambda: batch.rows.rescan(per_run(infos))),
        'records(label)': best(lambda: client.records(label='iofq')),
        'state() (incl. records)': best(batch.state),
        'policy by key (incl. records)': best(lambda: by_key(batch, 'sample', states)),
    }

    def step():
        batch.scan()
        s = batch.state()
        return reduce_all_but_refused(batch, settings, by_key(batch, 'sample', s))

    idle_batch = best(step)
    idle_loop = best(loop.step)
    new_batch, new_loop, one_submit = [], [], []
    for j in range(3):
        p.measure(n + 2 * j, [1.0, 2.0], role='sample')
        t0 = time.perf_counter()
        done, _ = step()
        new_batch.append(time.perf_counter() - t0)
        client.wait(list(done.values()))
        loop.step()  # takes the batch's record as handled
        ref = p.measure(n + 2 * j + 1, [1.0, 2.0], role='sample')
        t0 = time.perf_counter()
        settled = loop.step()
        new_loop.append(time.perf_counter() - t0)
        client.wait(settled)
        ref = p.measure(10 * n + j, [1.0, 2.0], role='other')
        t0 = time.perf_counter()
        r = client.submit(toys.IOFQ, params={'sample': ref, 'normalization': None},
                          label='x', persist=True)
        one_submit.append(time.perf_counter() - t0)
        client.wait([r])
    print(f'N={n}  (filled in {t_fill:.1f} s)')
    for name, t in parts.items():
        print(f'    {name:32} {t:8.1f} ms')
    print(f'    {"batch step, idle":32} {idle_batch:8.1f} ms')
    print(f'    {"batch step, one new file":32} {min(new_batch) * 1e3:8.1f} ms')
    print(f'    {"trigger loop step, idle":32} {idle_loop:8.1f} ms')
    print(f'    {"trigger loop, one new file":32} {min(new_loop) * 1e3:8.1f} ms')
    print(f'    {"client.submit of one request":32} {min(one_submit) * 1e3:8.1f} ms', flush=True)
    p.close()

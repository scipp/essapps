"""
Cost of the first step (N new runs, reduced row by row), and of one idle step (nothing new) with N runs already reduced: the batch loop
(scan, handled, state) against the trigger loop (reads only the changes).
"""

import time

from rules_common import Batch, Proposal, auto_step, toys

from ess.apps import Rule, Selector, TriggerLoop
from ess.apps.batch import FileInfo, Row


def per_run(files: list[FileInfo]) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files
            if f.metadata.get('role') == 'sample']


def submit(client, ref):
    member = str(client.files.metadata(ref)['run'])
    return client.submit(toys.IOFQ, params={'sample': ref, 'normalization': None},
                         label='iofq', member=member, persist=True)


settings = {'normalization': None}
for n in (300, 1000, 3000):
    p = Proposal()
    client = p.open()
    for i in range(n):
        p.measure(i, [1.0, 2.0], role='sample')
    batch = Batch(client, toys.IOFQ, columns=('sample',), label='iofq', source=per_run,
                  persist=True)
    t0 = time.perf_counter()
    done, _ = auto_step(batch, settings, at='sample', each=True)
    t_each = time.perf_counter() - t0
    client.wait(list(done.values()))
    other = Batch(client, toys.IOFQ, columns=('sample',), label='iofq-2', source=per_run,
                  persist=True)
    t0 = time.perf_counter()
    done, _ = auto_step(other, settings, at='sample')
    t_first = time.perf_counter() - t0
    client.wait(list(done.values()))
    loop = TriggerLoop(client, [Rule('iofq', submit, Selector(role='sample'), 'iofq', 'sample')])
    loop.step()

    def best(f):
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            f()
            times.append(time.perf_counter() - t0)
        return min(times)

    q = Proposal()
    qc = q.open()
    for i in range(n):
        q.measure(i, [1.0, 2.0], role='sample')
    t0 = time.perf_counter()
    TriggerLoop(qc, [Rule('iofq', submit, Selector(role='sample'), 'iofq', 'sample')]).step()
    t_rules_first = time.perf_counter() - t0
    q.close()
    t_batch = best(lambda: auto_step(batch, settings, at='sample'))
    t_loop = best(loop.step)
    print(f'N={n:5}  batch step {t_batch * 1e3:7.1f} ms   trigger loop step {t_loop * 1e3:6.1f} ms   '
          f'first step: batch row by row {t_each:5.1f} s, all but refused {t_first:5.1f} s, trigger loop {t_rules_first:5.1f} s')
    p.close()

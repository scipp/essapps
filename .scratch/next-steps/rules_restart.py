"""
Story: a restart of the automatic reduction.

1. Runs 1 and 2 are reduced. Run 9's reduction is running when the process
   ends (its workflow never returns; the next workspace opens the same history).
2. After the restart the record of run 9 has failed ("started" event, #189).
3. A restart under another submitter name.
4. Run 2 arrives again.
5. The history is dropped (a new, empty log), as durable.md item 7 describes.
"""

import os
import sys
import threading
import time

from rules_common import Batch, Proposal, auto_step, naive_step, toys

from ess.apps.batch import FileInfo, Row
from ess.apps.log import Log
from ess.spec import function_binding

never = threading.Event()


def iofq(params):
    if params.sample.name == 'run-9.json':
        never.wait()  # the process dies while this runs
    return toys.iofq(params)


def per_run(files: list[FileInfo]) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref})
            for f in files if f.metadata.get('role') == 'sample']


def restart(p: Proposal):
    time.sleep(0.5)  # let the worker start what was submitted
    p.workspace = None  # the process ends: the old workspace is never closed
    return p.restart()


settings = {'normalization': None}
for loop in ('obvious', 'handled'):
    p = Proposal()
    p.bind = [function_binding(toys.IOFQ, iofq)]
    client = p.open()

    def step(client):
        batch = Batch(client, toys.IOFQ, columns=('sample',), label='auto/iofq',
                      source=per_run, persist=True)
        if loop == 'obvious':
            return sorted(naive_step(batch, settings))
        return sorted(auto_step(batch, settings, at='sample')[0])

    p.measure(1, [1.0, 2.0], role='sample')
    p.measure(2, [1.0, 2.0], role='sample')
    out = [step(client)]
    client.wait(client.records())
    p.measure(9, [1.0, 2.0], role='sample')
    out.append(step(client))
    client = restart(p)
    (nine,) = [r for r in client.records() if r.member == '9']
    failure = client.failure([nine])[0]
    out.append(step(client))
    again = [r for r in client.records() if r.member == '9' and r.id != nine.id]
    p.submitter = 'auto-v2'
    out.append(step(restart(p)))
    p.measure(2, [1.0, 2.0], role='sample')  # arrives again
    out.append(step(restart(p)))
    p.log = Log()
    out.append(step(restart(p)))
    print(f'{loop:8}', 'submitted per step:', out)
    print(f'{"":8}', 'run 9 after the restart:', failure, '| run 9 submitted again:', len(again))

print('steps: 1+2 | 9 | after restart | other submitter | 2 again | history dropped')
sys.stdout.flush()
os._exit(0)  # the hung workflows hold their threads

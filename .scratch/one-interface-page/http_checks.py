"""
What the page's server answers while the loop steps the same Batch: the version
when a row waits, the primary button while reductions run, and the page's
actions (retry, load a table, edit at the trigger column) against the loop.

Run: /workspace/essapps/.scratch/venv-container/bin/python http_checks.py
"""
import time
from typing import Any

import harness
from harness import Setup
from ess.apps.batch.web.models import CellEdit, Reduce

def say(what: str, *rest: Any) -> None:
    print(f'{what:<46}', *rest, flush=True)

def records_of(s: Setup, key: str) -> int:
    return sum(r.member == key for r in s.client.records(label='auto'))

s = Setup()
s.fill(20)
s.start_loop()
srv = s.server
time.sleep(1.5)
st = srv.poll(0)
v = st.version
assert srv.poll(v, st.start) is None

# A row that waits: the version stays, so a page that polls gets 204
s.sample(100, 'lonely', partner=False)
time.sleep(2.5)
p = srv.poll(v, st.start)
say('lonely row: poll answers', 'nothing new (204)' if p is None else f'{len(p.rows)} rows')
say('  rows on the server', len(s.batch.rows), 'loop refused', s.auto.refused)
say('  status the state would show', next(r.status for r in srv.state().rows if r.key == '100'))

# A failing row: does the loop run it again?
s.sample(101, 'bad', value=-1.0)
time.sleep(3)
say('failing row: records after 3 s of steps', records_of(s, '101'))
st = srv.state()
say('  primary button', st.action.label)
srv.reduce(Reduce(which='remaining'))   # "Retry 1 failed"
time.sleep(3)
say('  after "Retry", records', records_of(s, '101'), 'loop errors', s.errors[:1])

# An edit at the trigger column: the loop reduces the row
before = records_of(s, '5')
other = next(c for c in srv.state().columns if c.name == 'sample')
opts = srv.state().rows[6].cells['sample'].value   # label of row 6's file
srv.edit(CellEdit(keys=['5'], column='sample', value=opts))
time.sleep(2.5)
say('edit sample of row 5 to another file: records', before, '->', records_of(s, '5'))
say('  status shown', next(r.status for r in srv.state().rows if r.key == '5'))
# An edit at a column that is not the trigger: the loop leaves it
before = records_of(s, '7')
srv.edit(CellEdit(keys=['7'], column='thickness', value=3.0))
time.sleep(2.5)
say('edit thickness of row 7: records', before, '->', records_of(s, '7'), next(r.status for r in srv.state().rows if r.key == '7'))

# Load a table file while the loop runs: does the next step replace it?
csv = srv.table_file('.csv').decode().splitlines()
head, *rows = csv
i = head.split(',').index('thickness')
cells = rows[3].split(','); cells[i] = '4.0'; rows[3] = ','.join(cells)
key3 = cells[0]
try:
    srv.load_table('mine.csv', '\n'.join([head, *rows]).encode())
    say('load a table: thickness of row', key3, s.batch.rows[key3].values.get('thickness'), 'loaded', srv.loaded)
    time.sleep(2.5)
    say('  after 2.5 s of steps', s.batch.rows[key3].values.get('thickness'), 'loaded', srv.loaded)
except Exception as e:
    say('load a table: refused', repr(e)[:200])

# Reductions that take 3 s, a file every second: the primary button
harness.DELAY = 3.0
seen = []
for k in range(8):
    s.sample(200 + k, f'slow{k}')
    for _ in range(4):
        a = srv.state().action
        seen.append((a.label, a.progress, a.running))
        time.sleep(0.25)
labels = []
for x in seen:
    if not labels or labels[-1] != x:
        labels.append(x)
say('slow reductions, a file a second: primary', '')
for x in labels[:14]:
    print('   ', x)
say('  action blocked by progress (page disables)', sum(1 for x in seen if x[1] is not None), 'of', len(seen))
harness.DELAY = 0.0
time.sleep(4)
s.stop()

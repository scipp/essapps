"""
A step of the automatic reduction that recomputes the rows from all files, but
works out state and submits only for the rows that changed since the last
step. N runs already reduced, one new file per step. The list of files is kept
and appended to, as a file source that reports what is new would allow.
"""

import sys
import time

from rules_common import Proposal, files_at, toys

from ess.apps import Template
from ess.apps.batch import FileInfo, Row
from ess.apps.batch.reduction import file_infos
from ess.apps.batch.state import problems
from ess.spec.refs import iter_refs

LABEL, AT = 'iofq', 'sample'


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
        values = {'sample': f.ref}
        if name in transmissions:
            values['normalization'] = None  # the toy has no transmission parameter
        rows.append(Row(str(f.metadata['run']), values, info={'name': str(name)}))
    return rows


class Auto:
    def __init__(self, client, template):
        self.client, self.template = client, template
        self.rows: dict[str, Row] = {}
        self.held: dict[str, set] = {}   # files at AT that records of a key hold
        self.cursor = 0
        self.t = {}

    def step(self, infos):
        t0 = time.perf_counter()
        changes = self.client.changes(after=self.cursor)
        self.cursor = changes.cursor
        for r in self.client.records(ids=changes.records):
            if r.label == LABEL and r.member:
                self.held.setdefault(r.member, set()).update(files_at(r, AT))
        t1 = time.perf_counter()
        rows = {r.key: r for r in pair(infos)}
        t2 = time.perf_counter()
        changed = [k for k, r in rows.items() if self.rows.get(k) != r]
        self.rows = rows
        t3 = time.perf_counter()
        todo = {}
        for k in changed:
            row = rows[k]
            cell = {ref for _, ref in iter_refs({AT: row.values.get(AT)})}
            if cell - self.held.get(k, set()) and not problems(
                row, toys.IOFQ.params, ('sample',), {}
            ):
                todo[k] = self.template.fill(params={'sample': row.values['sample']})
        records = self.client.submit(todo, label=LABEL, persist=True) if todo else {}
        t4 = time.perf_counter()
        self.t = {'changes': t1 - t0, 'source': t2 - t1, 'diff': t3 - t2,
                  'state+submit of changed': t4 - t3, 'total': t4 - t0}
        return records


for n in map(int, sys.argv[1:]):
    p = Proposal()
    client = p.open()
    for i in range(n):
        p.measure(i, [1.0, 2.0], role='sample', sample=f's{i}')
        if i % 10 == 0:
            p.measure(n + i, [1.0], role='transmission', sample=f's{i}')
    template = Template(toys.IOFQ, params={'normalization': None}, blanks=('sample',))
    auto = Auto(client, template)
    infos = file_infos(client)
    t0 = time.perf_counter()
    first = auto.step(infos)
    client.wait(list(first.values()))
    fill = time.perf_counter() - t0
    best = None
    for j in range(5):
        ref = p.measure(3 * n + j, [1.0, 2.0], role='sample', sample=f'new{j}')
        infos.append(FileInfo(ref, client.files.metadata(ref)))
        done = auto.step(infos)
        assert list(done) == [str(3 * n + j)], done
        if best is None or auto.t['total'] < best['total']:
            best = auto.t
        client.wait(list(done.values()))
    assert auto.step(infos) == {}
    idle = auto.t
    print(f'N={n} ({len(infos)} files, first step {fill:.1f} s); one new file, ms: '
          + ', '.join(f'{k} {v * 1e3:.1f}' for k, v in best.items())
          + f'; idle total {idle["total"] * 1e3:.1f}', flush=True)
    p.close()

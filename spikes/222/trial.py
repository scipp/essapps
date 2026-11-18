# The trial script of #222, as the issue gives it.
# ruff: noqa: E501, F401, T201
"""Emulate ADR 0011's three steps with today's Batch, to see what the code paths give."""

import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from ess.apps import Client, Workspace
from ess.apps.batch import Batch, FileInfo, Row, earlier_settings, left_to_reduce
from ess.apps.batch.auto import Automatic
from ess.apps.batch.state import status_texts
from ess.apps.testing import FakeFiles, read_file
from ess.spec import Referenced, RunFile, WorkflowSpec, function_binding


class Image(BaseModel):
    value: float


class Centre(BaseModel):
    x: float


class DetP(BaseModel):
    run: RunFile


class DetO(BaseModel):
    detector: Image


class CenP(BaseModel):
    detector: Referenced[Image]
    masks: float = 0.0


class CenO(BaseModel):
    centre: Centre


class IqP(BaseModel):
    sample: RunFile
    sample_transmission: RunFile
    beam_centre: Centre | Referenced[Centre]
    q: int = 10


class IqO(BaseModel):
    total: float


def spec(name, p, o):
    return WorkflowSpec(
        name=name, version=1, title=name, description='toy', params=p, outputs=o
    )


DET, CEN, IQ = spec('det', DetP, DetO), spec('cen', CenP, CenO), spec('iq', IqP, IqO)


def det(p: DetP) -> DetO:
    v = read_file(p.run)
    if v < 0:
        raise ValueError('bad file')
    return DetO(detector=Image(value=v))


def cen(p: CenP) -> CenO:
    if p.detector.value == 13:
        raise ValueError('no centre found')
    return CenO(centre=Centre(x=p.detector.value + p.masks))


def iq(p: IqP) -> IqO:
    return IqO(total=read_file(p.sample) + p.beam_centre.x + p.q)


def pair(files: Sequence[FileInfo]) -> list[Row]:
    """As LoKI's pair_by_sample: the last transmission of the sample's name."""
    files = sorted(files, key=lambda f: f.metadata['run'])
    trans = {
        f.metadata['sample']: f.ref
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = []
    for f in files:
        if f.metadata.get('role') == 'sample':
            v = {'sample': f.ref}
            if f.metadata['sample'] in trans:
                v['sample_transmission'] = trans[f.metadata['sample']]
            rows.append(Row(str(f.metadata['run']), v))
    return rows


class Steps:
    """A batch of ADR 0011, emulated: one scope, and a today's Batch per step."""

    def __init__(self, client, ident, scope, steps):
        self.scope, self.order, self.steps, self.takes = scope, [], {}, {}
        for sid, sp, takes in steps:
            self.order.append(sid)
            self.takes[sid] = takes
            self.steps[sid] = Batch(
                client,
                sp,
                columns=tuple(takes),
                label=f'{ident}/{sid}',
                source=self._source(sid),
            )

    def _source(self, sid):
        def source(files):
            lo, hi = self.scope
            rows = []
            for row in pair(files):
                if not (lo <= int(row.key) and (hi is None or int(row.key) <= hi)):
                    continue
                values = {}
                for param, column in self.takes[sid].items():
                    if '.' in column:
                        other, output = column.split('.')
                        rec = (
                            self.steps[other].results([row.key])[row.key].record
                            if row.key in self.steps[other].rows
                            else None
                        )
                        if rec is not None:
                            values[param] = rec.ref(output)
                    elif column in row.values:
                        values[param] = row.values[column]
                rows.append(Row(row.key, values))
            return rows

        return source

    def edit(self, key, column, value):
        for sid in self.order:
            for param, col in self.takes[sid].items():
                if col == column:
                    b = self.steps[sid]
                    b.rows = b.rows.edit(key, {param: value})

    def loop(self):
        return {
            sid: sorted(Automatic(self.steps[sid], at=set(self.takes[sid])).step())
            for sid in self.order
        }

    def show(self, title):
        print(f'-- {title}')
        texts = {}
        for sid in self.order:
            b = self.steps[sid]
            b.scan()
            texts[sid] = status_texts(b.state(), b.columns)
        for key in texts[self.order[0]]:
            print('  ', key, {sid: texts[sid].get(key) for sid in self.order})


STEPS = [
    ('detector', DET, {'run': 'sample'}),
    ('centre', CEN, {'detector': 'detector.detector'}),
    (
        'iofq',
        IQ,
        {
            'sample': 'sample',
            'sample_transmission': 'sample_transmission',
            'beam_centre': 'centre.centre',
        },
    ),
]


def world():
    fake = FakeFiles('p1', Path(tempfile.mkdtemp()))
    ws = Workspace(
        proposal='p1',
        files=fake,
        bind=[
            function_binding(DET, det),
            function_binding(CEN, cen),
            function_binding(IQ, iq),
        ],
    )
    client = Client(ws, submitter='anna')
    return fake, ws, client


def wait(client, b):
    for s in b.steps.values():
        client.wait([st.record for st in s.results().values() if st.record])


# Case A: the document's example
fake, ws, client = world()
b = Steps(client, 'b7', (60339, None), STEPS)
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
fake.measure(60340, 2.0, role='sample', sample='x')
print('A loop pass:', b.loop())
wait(client, b)
b.show('A after one pass')

# Case B: the detector of a row fails, with the later steps already submitted
fake.measure(60342, -1.0, role='sample', sample='w')
print('B loop pass:', b.loop())
wait(client, b)
b.show('B detector of 60342 failed')
print('B second pass:', b.loop())
wait(client, b)
fake.measure(60342, 5.0, role='sample', sample='w')  # the file is repaired
d = b.steps['detector']
d.scan()
retry = left_to_reduce(d.state(), d.columns)
print('B person: Reduce on the detector page:', retry)
client.wait(list(d.reduce(retry).values()))
b.show('B after the retry of the detector, before a pass')
print('B loop pass:', b.loop())
wait(client, b)
b.show('B after the pass')

# Case B2: the centre fails by itself
fake.measure(60343, 13.0, role='sample', sample='w')
print('B2 loop pass:', b.loop())
wait(client, b)
b.show('B2 centre of 60343 failed')
print('B2 next pass:', b.loop())
wait(client, b)

# Case C: a person edits `sample` of a row
other = fake.measure(60350, 7.0, role='calibration_like', sample='zz')
b.edit('60339', 'sample', other)
print('C loop pass after an edit of sample at 60339:', b.loop())
wait(client, b)
b.show('C after the pass')
for sid in b.order:
    s = b.steps[sid].state()['60339']
    print(
        '   ',
        sid,
        'set_by_hand',
        set(s.set_by_hand),
        'changed_by_hand',
        s.changed_by_hand,
    )

# Case E: a person changes a setting of the centre step and reduces "them again" there
c = b.steps['centre']
c.settings = {'masks': 1.0}
c.scan()
again = earlier_settings(c.state(), c.columns)
print('E person: Reduce them again on the centre page:', again)
client.wait(list(c.reduce(again).values()))
b.show('E before a pass')
print('E loop pass:', b.loop())
wait(client, b)
b.show('E after the pass')
ws.close()

# Case D: a series that ended, and a partner measured later
fake, ws, client = world()
morning = Steps(client, 'b1', (1, 10), STEPS)
fake.measure(1, 1.0, role='sample', sample='water')
fake.measure(2, 1.0, role='transmission', sample='water')
print('D loop pass:', morning.loop())
wait(client, morning)
morning.show('D morning, ended at 10')
fake.measure(20, 1.0, role='sample', sample='water')
fake.measure(21, 1.0, role='transmission', sample='water')
morning.show('D morning, after water is measured again in the afternoon')
i = morning.steps['iofq']
print(
    '   changed:',
    set(i.state()['1'].changed),
    'Reduce offers:',
    left_to_reduce(i.state(), i.columns),
)
ws.close()

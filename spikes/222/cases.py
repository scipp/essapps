# ruff: noqa: T201, PERF203
"""
The cases of #222 run against the table prototype (``table.py``).

    .venv/bin/python spikes/222/cases.py

Toy specs as in the trial script of #222 (``trial.py``), plus a transmission
step (TR) and a step added later that needs a new column (NORM).
"""

import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel
from pydantic_core import from_json, to_json

from ess.apps import Client, Workspace
from ess.apps.batch import FileInfo, Row
from ess.apps.testing import FakeFiles, read_file
from ess.spec import Referenced, RunFile, WorkflowSpec, function_binding

sys.path.insert(0, str(Path(__file__).parent))
from table import Table


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


class Total(BaseModel):
    value: float


class IqO(BaseModel):
    total: Total


class TrP(BaseModel):
    run: RunFile


class TrO(BaseModel):
    fraction: float


class NormP(BaseModel):
    total: Referenced[Total]
    thickness: float


class NormO(BaseModel):
    normalised: float


def spec(name, p, o):
    return WorkflowSpec(
        name=name, version=1, title=name, description='toy', params=p, outputs=o
    )


DET, CEN, IQ = spec('det', DetP, DetO), spec('cen', CenP, CenO), spec('iq', IqP, IqO)
TR, NORM = spec('tr', TrP, TrO), spec('norm', NormP, NormO)


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
    return IqO(total=Total(value=read_file(p.sample) + p.beam_centre.x + p.q))


def tr(p: TrP) -> TrO:
    return TrO(fraction=read_file(p.run) / 10)


def norm(p: NormP) -> NormO:
    return NormO(normalised=p.total.value / p.thickness)


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


def scoped(lo, hi):
    def source(files):
        return [
            r
            for r in pair(files)
            if lo <= int(r.key) and (hi is None or int(r.key) < hi)
        ]

    return source


FILLS = ('sample', 'sample_transmission')


def world():
    fake = FakeFiles('p1', Path(tempfile.mkdtemp()))
    bind = [
        function_binding(DET, det),
        function_binding(CEN, cen),
        function_binding(IQ, iq),
        function_binding(TR, tr),
        function_binding(NORM, norm),
    ]
    ws = Workspace(proposal='p1', files=fake, bind=bind)
    return fake, ws, Client(ws, submitter='anna')


def chain(t: Table, *, accept: bool = False) -> None:
    print('   added columns:', t.add(DET, id='detector', takes={'run': 'sample'}))
    print(
        '   added columns:',
        t.add(CEN, id='centre', takes={'detector': 'detector.detector'}, accept=accept),
    )
    print(
        '   added columns:',
        t.add(IQ, id='iofq', takes={'beam_centre': 'centre.centre'}),
    )


def say(*args):
    print('  ', *args)


def case(title):
    print(f'\n===== {title}')


# ---------------------------------------------------------------- A, B, B2, C, E
case('A: a new row, with Follow')
fake, ws, client = world()
t = Table(client, 'b7', source=pair, fills=FILLS)
chain(t)
say('columns:', t.columns)
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
fake.measure(60340, 2.0, role='sample', sample='x')
say('Follow:', t.follow())
t.wait()
t.show('A after one pass')

case('B: the detector fails, then is reduced again')
fake.measure(60342, -1.0, role='sample', sample='w')
say('Follow:', t.follow())
t.wait()
t.show('B detector of 60342 failed')
say('Follow again:', t.follow())
fake.measure(60342, 5.0, role='sample', sample='w')  # the file is repaired
say('person: Reduce row 60342:', t.reduce(['60342']))
t.wait()
t.show('B after the person reduced the row', ['60342'])
say('Follow:', t.follow())

case('B2: the centre fails by itself')
fake.measure(60343, 13.0, role='sample', sample='w')
say('Follow:', t.follow())
t.wait()
t.show('B2 centre of 60343 failed', ['60343'])
say('Follow again:', t.follow())

case('C: sample of 60339 set by hand, without Follow')
other = fake.measure(60350, 7.0, role='calibration_like', sample='zz')
t.edit('60339', {'sample': other})
t.show('C after the edit', ['60339'])
say('person: Reduce up to detector:', t.reduce(['60339'], upto='detector'))
t.wait()
t.show('C after Reduce up to detector', ['60339'])

case('C-Follow: the same, then Follow')
say('Follow:', t.follow())
say('person: Reduce row 60339:', t.reduce(['60339']))
t.wait()
t.show('C after the person reduced the row', ['60339'])
say('Follow:', t.follow())
t.rows = t.rows.add(Row('mine', {'sample': other, 'sample_transmission': other}))
t.show('C-added: a row added by hand', ['mine'])
say('Follow:', t.follow())
say('person: Reduce row mine:', t.reduce(['mine']))
t.wait()
fake.measure(
    60350, 8.0, role='calibration_like', sample='zz'
)  # same identity, so no change
say('Follow after the reduction:', t.follow())
t.rows = t.rows.remove('mine')

case('E: a setting of the centre changes; settings do not decide "current"')
t.step('centre').settings = {'masks': 1.0}
t.show('E after the change, before anything')
say('Follow:', t.follow())
say(
    'person: Reduce them again at centre, up to centre:',
    t.reduce(again={'centre'}, upto='centre'),
)
t.wait()
t.show('E after Reduce them again up to centre')
say('Follow:', t.follow())
t.wait()
t.show('E after Follow')
t.step('centre').settings = {'masks': 2.0}
say('person: Reduce them again at centre, whole chain:', t.reduce(again={'centre'}))
t.wait()
t.show('E2 after another change and Reduce them again, without upto')
say('Follow:', t.follow())
ws.close()

case('E-strict: the same, with settings deciding "current" (settings_count=True)')
fake, ws, client = world()
t = Table(client, 'b7', source=pair, fills=FILLS, settings_count=True)
chain(t)
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
t.follow()
t.wait()
t.step('centre').settings = {'masks': 1.0}
t.show('E-strict after the change of masks')
say('Follow:', t.follow())
say('person: Reduce:', t.reduce())
t.wait()
t.show('E-strict after Reduce')
ws.close()

# ---------------------------------------------------------------- D
case('D: a series that ended at 10, and a partner measured later')
fake, ws, client = world()
t = Table(client, 'b1', source=scoped(1, 10), fills=FILLS)
chain(t)
fake.measure(1, 1.0, role='sample', sample='water')
fake.measure(2, 1.0, role='transmission', sample='water')
say('Follow:', t.follow())
t.wait()
t.show('D morning')
fake.measure(20, 1.0, role='sample', sample='water')
fake.measure(21, 1.0, role='transmission', sample='water')
t.scan()
t.show('D after water is measured again in the afternoon')
say('changed at iofq:', set(t.state()['iofq']['1'].changed))
ws.close()

# ---------------------------------------------------------------- F
case('F: adding a workflow to a table that has results')
fake, ws, client = world()
t = Table(client, 'b2', source=pair, fills=FILLS)
t.add(DET, id='detector', takes={'run': 'sample'})
t.add(CEN, id='centre', takes={'detector': 'detector.detector'})
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
fake.measure(60340, 2.0, role='sample', sample='x')
t.follow()
t.wait()
say('added columns:', t.add(IQ, id='iofq', takes={'beam_centre': 'centre.centre'}))
t.show('F after adding iofq')
say('Follow:', t.follow())
t.wait()
say(
    'added columns:',
    t.add(NORM, id='norm', takes={'total': 'iofq.total', 'thickness': 'thickness'}),
)
say('columns:', t.columns)
t.show('F after adding norm')
t.edit('60339', {'thickness': 2.0})
say('Follow:', t.follow())
say('person: Reduce:', t.reduce())
t.wait()
t.show('F after thickness of 60339 and Reduce')

case('I: removing a workflow')
for sid in ('centre', 'norm'):
    try:
        say(f'remove {sid}: columns gone', t.remove(sid))
    except ValueError as e:
        say(f'remove {sid}: refused:', e)
say('edits left:', dict(t.rows.edits))
say('columns:', t.columns)
ws.close()

# ---------------------------------------------------------------- G
case('G: a person accepts a centre before I(Q) runs')
fake, ws, client = world()
t = Table(client, 'b3', source=pair, fills=FILLS)
chain(t, accept=True)
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
say('Follow:', t.follow())
t.wait()
t.show('G after Follow')
first = t.state()['centre']['60339'].record
t.use('60339', 'centre.centre', first)
t.show('G after Use, before Reduce')
say('Follow:', t.follow())
say('person: Reduce row:', t.reduce(['60339']))
t.wait()
t.show('G after Use and Reduce')
t.step('centre').settings = {'masks': 1.0}
say('person: Reduce them again at centre:', t.reduce(['60339'], again={'centre'}))
t.wait()
t.show('G after a newer centre')
say('Follow:', t.follow())
say(
    'cell centre.centre:',
    t.rows['60339'].values['centre.centre'].record == first.id,
    '(the accepted one)',
)
ws.close()

# ---------------------------------------------------------------- H
case('H: two sample runs share one transmission run')
fake, ws, client = world()
t = Table(client, 'b4', source=pair, fills=FILLS)
t.add(TR, id='trans', takes={'run': 'sample_transmission'}, per='sample_transmission')
t.add(TR, id='trans_by_row', takes={'run': 'sample_transmission'})
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60340, 3.0, role='sample', sample='w')
fake.measure(60341, 4.0, role='transmission', sample='w')
say('Follow:', t.follow())
t.wait()
t.show('H after Follow')
st = t.state()
for sid in ('trans', 'trans_by_row'):
    say(sid, 'records:', {k: c.record.id[:8] for k, c in st[sid].items()})
other_t = fake.measure(60345, 5.0, role='transmission', sample='other')
t.edit('60340', {'sample_transmission': other_t})
t.show('H after sample_transmission of 60340 set by hand')
say('Follow:', t.follow())
fake.measure(60346, 6.0, role='transmission', sample='w')  # a newer transmission of w
t.scan()
t.show('H after a newer transmission run of w (before Follow)')
say('Follow:', t.follow())
t.wait()
t.show('H after Follow')
ws.close()

# ---------------------------------------------------------------- J
case('J: a restart')
fake, ws, client = world()
t = Table(client, 'b5', source=pair, fills=FILLS)
chain(t, accept=True)
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')
fake.measure(60340, 2.0, role='sample', sample='w')
t.follow()
t.wait()
t.use('60339', 'centre.centre', t.state()['centre']['60339'].record)
t.step('iofq').settings = {'q': 20}
t.reduce(['60339'])
t.wait()
t.show('J before')
# as FolderDefinitions writes and reads them: JSON
files = {name: from_json(to_json(data)) for name, data in t.definitions().items()}
for name, data in files.items():
    say(f'{name}:', json.dumps(data))
again = Table.restore(
    Client(ws, submitter='anna'), 'b5', files, source=pair, fills=FILLS
)
again.scan()
again.show('J after a restart: a new Table from the definitions and the records')
ws.close()

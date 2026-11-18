# ruff: noqa: T201
"""
The table prototype bound once to LoKI's three real specs, to check names and
types. The bindings are stand-ins: no real data is reduced.

    .venv/bin/python spikes/222/loki.py
"""

import sys
import tempfile
from pathlib import Path

import scipp as sc
from ess.instruments.loki.batch import pair_by_sample
from ess.instruments.loki.specs import (
    BEAM_CENTRE,
    CALIBRATED_DETECTOR,
    IOFQ,
    MASKS,
    BeamCentre,
    BeamCentreOutputs,
    CalibratedDetectorOutputs,
    DetectorImage,
    MasksOutputs,
    PixelMasks,
)

from ess.apps import Client, Workspace
from ess.apps.testing import FakeFiles
from ess.spec import function_binding

sys.path.insert(0, str(Path(__file__).parent))
from table import Table

FILLS = ('sample', 'sample_transmission')


def detector(p):
    counts = sc.DataArray(sc.zeros(dims=['pixel'], shape=[2]))
    return CalibratedDetectorOutputs(detector=DetectorImage(counts=counts))


def masks(p):
    return MasksOutputs(masks=PixelMasks(masks={}))


def centre(p):
    return BeamCentreOutputs(centre=BeamCentre(x=0.0, y=0.0, z=1.0))


def iofq(p):
    raise RuntimeError('not run in this check')


fake = FakeFiles('p1', Path(tempfile.mkdtemp()))
bind = [
    function_binding(CALIBRATED_DETECTOR, detector),
    function_binding(MASKS, masks),
    function_binding(BEAM_CENTRE, centre),
    function_binding(IOFQ, iofq),
]
ws = Workspace(proposal='p1', files=fake, bind=bind)
client = Client(ws, submitter='anna')
fake.measure(60339, 1.0, role='sample', sample='w')
fake.measure(60341, 1.0, role='transmission', sample='w')

print('== The rule first tried: every required parameter without a setting is a column')
t = Table(client, 'loki-default', source=pair_by_sample, fills=FILLS)
print(
    ' detector adds', t.add(CALIBRATED_DETECTOR, id='detector', takes={'run': 'sample'})
)
print(
    ' centre adds  ',
    t.add(BEAM_CENTRE, id='centre', takes={'detector': 'detector.detector'}),
)
print(' iofq adds    ', t.add(IOFQ, id='iofq', takes={'beam_centre': 'centre.centre'}))
for s in t.steps:
    fields = s.spec.params.model_fields
    would = [
        n
        for n, f in fields.items()
        if n not in s.takes and n not in s.settings and f.is_required()
    ]
    print(
        f' {s.id}: takes {s.takes}; the rule first tried would add the columns {would}'
    )
t.scan()
t.show('without settings')

print('\n== Settings given where the value is one for all rows (masks by a record)')
m = client.submit(
    MASKS,
    params={
        'detector': client.submit(
            CALIBRATED_DETECTOR, params={'run': fake.measure(60300, 0.0)}
        ).ref('detector'),
        'mask_files': [],
    },
)
client.wait([m])
t = Table(client, 'loki', source=pair_by_sample, fills=FILLS)
print(
    ' detector adds', t.add(CALIBRATED_DETECTOR, id='detector', takes={'run': 'sample'})
)
print(
    ' centre adds  ',
    t.add(
        BEAM_CENTRE,
        id='centre',
        takes={'detector': 'detector.detector'},
        settings={'masks': m.ref('masks')},
        accept=True,
    ),
)
can = fake.measure(60330, 1.0, role='background')
print(
    ' iofq adds    ',
    t.add(
        IOFQ,
        id='iofq',
        takes={'beam_centre': 'centre.centre'},
        settings={
            'masks': m.ref('masks'),
            'can': can,
            'can_transmission': can,
            'empty_beam': can,
            'direct_beam': None,
            'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 100, 'unit': 'Å'},
            'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 50, 'unit': '1/Å'},
        },
    ),
)
print(' columns', t.columns)
print(' Follow:', t.follow())
t.wait()
t.show('after Follow')
centre_record = t.state()['centre']['60339'].record
t.use('60339', 'centre.centre', centre_record)
st = t.state()['iofq']['60339']
print(
    ' iofq after Use: problems', st.problems, 'waits', st.waits, 'text', st.text(False)
)
ws.close()

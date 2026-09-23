# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section E of docs/developer/user-stories.md: automatic reduction.
"""

import pytest

from ess.apps.batch import (
    TriggerLoop,
    batch_table,
    dataset_table,
    reprocess,
    trigger_status,
)
from ess.apps.client import Client
from ess.apps.examples import LOAD, NORMALIZE
from ess.apps.records import Template
from ess.apps.rules import Like, Rule, Selector, Series
from ess.apps.sources import Dataset
from ess.apps.spec import SpecId
from ess.apps.testing import FakeDatasetSource, FakePublisher

from .conftest import Measure


def test_e1_series_grows_reduction_follows(client: Client, measure: Measure) -> None:
    """NORMALIZE's sum over runs stands in for the stitch over angles."""
    rule = Rule(
        name='reflectivity',
        template=Template(name='normalize-defaults', spec=NORMALIZE, blanks=('run',)),
        selector=Selector(match={'role': Like(pattern='sample')}),
        series=Series(
            key='sample',
            accumulate=('numerator', 'denominator'),
            outputs=('normalized',),
        ),
    )
    loop = TriggerLoop(client, rule)

    measure(1, [1.0, 1.0], role='reference')
    measure(2, [1.0, 2.0], role='sample', sample='si')
    loop.run_once()
    measure(4, [2.0, 2.0], role='sample', sample='si')
    measure(3, [1.0, 1.0], role='sample', sample='si')
    measure(2, [1.0, 2.0], role='sample', sample='si')  # arrives again
    loop.run_once()
    curve = client.latest('reflectivity', 'si')

    assert batch_table(client, rule).index.tolist() == [
        'run:dream/2',
        'run:dream/3',
        'run:dream/4',
        'si',
    ]
    assert len(client.records(label='reflectivity', member_key='si')) == 3
    assert client.output(curve, 'normalized').values.tolist() == [4.0 / 9.0, 5.0 / 9.0]


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason='trigger_status does not validate the request the rule would make, so '
    'it says the rule fires; the refusal is only in the TriggerLoop object',
)
def test_e2_automatic_reduction_goes_quiet(client: Client, measure: Measure) -> None:
    """A template naming load/v2 stands in for one whose version the upgrade removed."""
    stale = Template(
        name='load-defaults', spec=SpecId(name='load', version=2), blanks=('run',)
    )
    rule = Rule(name='auto-load', template=stale)
    measure(1)

    fired = TriggerLoop(client, rule).run_once()

    assert fired == []
    assert client.records() == []
    (dataset,) = client.datasets()
    status = trigger_status(client, rule, dataset)
    assert (status.fires, status.reason) == (False, 'unknown spec load/v2')


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason='no clause keeps the trigger loop off a dataset carrying our provenance '
    'snapshot, and no dataset source tells published outputs from raw data',
)
def test_e3_reduction_of_our_own_output(
    client: Client,
    measure: Measure,
    catalogue: FakeDatasetSource,
    scicat: FakePublisher,
) -> None:
    """Adding the published output to the catalogue stands in for SciCat listing it."""
    rule = Rule(
        name='auto-load',
        template=Template(name='load-defaults', spec=LOAD, blanks=('run',)),
    )
    loop = TriggerLoop(client, rule)
    measure(1)
    (reduced,) = loop.run_once()
    # allow_reused: the toy workflows are bound in process
    pid = client.publish(reduced.ref('data'), 'scicat', allow_reused=True)
    entry = scicat.entries[pid]
    catalogue.add(
        Dataset(path=entry['path'], pid=pid, metadata={'provenance': entry['snapshot']})
    )

    fired = loop.run_once()

    assert fired == []
    assert dataset_table(client).index.tolist() == ['run:dream/1']


def test_e4_template_improved_during_a_beamtime(
    client: Client, measure: Measure
) -> None:
    rule = Rule(
        name='auto-load',
        template=Template(
            name='load-defaults', spec=LOAD, params={'scale': 1.0}, blanks=('run',)
        ),
    )
    measure(1)
    (before,) = TriggerLoop(client, rule).run_once()

    improved = rule.revise(template=rule.template.revise(scale=2.0))
    measure(2)
    TriggerLoop(client, improved).run_once()

    assert batch_table(client, improved)[['template', 'rule']].to_dict('index') == {
        'run:dream/1': {'template': 'load-defaults/v1', 'rule': 'auto-load/v1'},
        'run:dream/2': {'template': 'load-defaults/v2', 'rule': 'auto-load/v2'},
    }
    assert client.record(before.id) == before
    assert list(reprocess(client, improved)) == ['run:dream/1']

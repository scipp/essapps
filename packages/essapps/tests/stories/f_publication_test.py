# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section F of docs/developer/user-stories.md: publication and provenance.
"""

import json

import pytest

from ess.apps.backend import SubmitError
from ess.apps.client import Client
from ess.apps.examples import HISTOGRAM, LOAD, REBIN
from ess.apps.records import Derivation, Template
from ess.apps.testing import FakePublisher

from .conftest import Measure


def test_f1_publish_then_trace_six_months_later(
    client: Client, measure: Measure, scicat: FakePublisher
) -> None:
    loaded = client.run(LOAD, {'run': measure(1), 'scale': 2.0})
    reduced = client.run(REBIN, {'data': loaded.ref('data'), 'bins': 2})

    # allow_reused: the toy workflows are bound in process
    pid = client.publish(reduced.ref('result'), 'scicat', allow_reused=True)

    snapshot = scicat.entries[pid]['snapshot']
    assert json.loads(json.dumps(snapshot)) == snapshot  # plain data, no service
    assert snapshot['spec'] == 'rebin/v1'
    assert snapshot['params']['bins'] == 2
    assert {'essapps', 'scipp', 'sciline'} <= snapshot['package_versions'].keys()
    (source,) = snapshot['inputs']
    assert source['params']['scale'] == 2.0
    assert source['raw'] == [{'dataset': 'run:dream/1'}]


@pytest.mark.xfail(
    raises=pytest.fail.Exception,
    strict=True,
    reason="recompute does not compare the current environment with the record's",
)
def test_f2_reproduce_after_two_upgrades(
    client: Client, measure: Measure, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('CONDA_DEFAULT_ENV', 'ess-2026.03')
    reduced = client.run(LOAD, {'run': measure(1)})
    monkeypatch.setenv('CONDA_DEFAULT_ENV', 'ess-2027.03')

    with pytest.raises(SubmitError, match='environment'):
        client.recompute(reduced)
    again = client.recompute(reduced, allow_other_environment=True)

    assert again.derives_from == Derivation(record=reduced.id, reason='recompute')
    assert again.environment == 'ess-2027.03'


@pytest.mark.xfail(
    raises=TypeError,
    strict=True,
    reason='publication refuses a result a held stage served instead of recomputing '
    'it cold, and allow_reused waives that and the in-process check alike',
)
def test_f3_publish_what_was_tuned_interactively(
    client: Client, measure: Measure, scicat: FakePublisher
) -> None:
    data = client.run(LOAD, {'run': measure(1)}).ref('data')
    plot = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='plot'
    )
    for bins in (2, 3, 4):
        tuned = client.run(plot, {'bins': bins})

    pid = client.publish(tuned.ref('histogram'), 'scicat', allow_in_process=True)

    snapshot = scicat.entries[pid]['snapshot']
    published = client.record(snapshot['record'])
    assert published.derives_from == Derivation(record=tuned.id, reason='recompute')
    assert not published.reused
    assert snapshot['binding'] == 'in_process'


@pytest.mark.xfail(
    raises=TypeError,
    strict=True,
    reason='a publication cannot name the PID it supersedes',
)
def test_f4_publish_a_corrected_version(
    client: Client, measure: Measure, scicat: FakePublisher
) -> None:
    """HISTOGRAM's threshold stands in for the mask."""
    data = client.run(LOAD, {'run': measure(1)}).ref('data')
    bad = client.run(HISTOGRAM, {'data': data, 'threshold': 2.5})
    old = client.publish(bad.ref('histogram'), 'scicat', allow_reused=True)
    fixed = client.run(HISTOGRAM, {'data': data, 'threshold': 1.5})

    new = client.publish(
        fixed.ref('histogram'), 'scicat', allow_reused=True, supersedes=old
    )

    assert scicat.entries[new]['snapshot']['supersedes'] == old
    assert old in scicat.entries

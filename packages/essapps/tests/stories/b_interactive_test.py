# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section B of docs/developer/user-stories.md: manual and interactive reduction.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ess.apps.client import Client, local
from ess.apps.datastore import MissingCopyError
from ess.apps.examples import HISTOGRAM, LOAD, NORMALIZE, registry
from ess.apps.records import Accumulate, RunRecord, Template
from ess.apps.testing import FakeDatasetSource, equal

from .conftest import Measure


def summed(members: list[RunRecord]) -> dict[str, Accumulate]:
    """A finalize's inputs: the accumulation of every member's intermediates."""
    return {
        name: Accumulate(accumulate=[m.ref(name) for m in members])
        for name in ('numerator', 'denominator')
    }


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason='from_request blanks what the request varied, so the tuned bin count '
    'is not in the saved template',
)
def test_b1_tune_a_sans_reduction_in_a_notebook(
    client: Client, measure: Measure
) -> None:
    """HISTOGRAM stands in for the SANS reduction: bins for Q, threshold for a mask."""
    data = client.run(LOAD, {'run': measure(1)}).ref('data')
    iofq = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='iofq'
    )

    tuned = [client.run(iofq, {'bins': bins}) for bins in (2, 3, 4)]
    tuned += [client.run(iofq, {'bins': bins, 'threshold': 1.5}) for bins in (4, 3)]
    final = client.latest('iofq')
    beamtime = Template.from_request('sans-beamtime', final.request, HISTOGRAM)

    assert [r.reused for r in tuned] == [False, True, True, False, True]
    assert client.records(label='iofq') == tuned
    assert final == tuned[-1]
    assert beamtime == Template(
        name='sans-beamtime',
        spec=HISTOGRAM,
        params={'threshold': 1.5, 'bins': 3},
        blanks=('data',),
        outputs=('histogram',),
    )


def test_b2_add_a_run_to_a_sum_then_remove_one(
    client: Client, measure: Measure
) -> None:
    normalize = Template(spec=NORMALIZE)
    member = normalize.cut(blanks=('run',), outputs=('numerator', 'denominator'))
    finalize = normalize.cut(blanks=('numerator', 'denominator'), name='sum')
    run611 = client.run(member, {'run': measure(611, [1.0, 3.0])})
    run612 = client.run(member, {'run': measure(612, [2.0, 6.0])})
    first = client.run(finalize, summed([run611, run612]))

    run613 = client.run(member, {'run': measure(613, [3.0, 1.0])})
    added = client.run(finalize, summed([run611, run612, run613]))
    removed = client.run(finalize, summed([run611, run613]))

    states = client.records(label='sum')
    assert states == [first, added, removed]
    assert [client.output(s, 'normalized').values.tolist() for s in states] == [
        [0.25, 0.75],
        [0.375, 0.625],
        [0.5, 0.5],
    ]
    assert [{ref.record for ref in s.request.refs()} for s in states] == [
        {run611.id, run612.id},
        {run611.id, run612.id, run613.id},
        {run611.id, run613.id},
    ]


def test_b3_compare_two_parameter_sets_side_by_side(
    client: Client, measure: Measure
) -> None:
    data = client.run(LOAD, {'run': measure(1)}).ref('data')
    iofq = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='iofq'
    )
    masked = iofq.cut(name='iofq-masked')

    plain = client.run(iofq, {'bins': 2})
    with_mask = client.run(masked, {'bins': 2, 'threshold': 1.5})
    plot = [client.view(r.ref('histogram')) for r in (plain, with_mask)]
    kept = client.run(masked, {'bins': 4, 'threshold': 1.5})

    assert [line['values'].tolist() for line in plot] == [[3.0, 7.0], [2.0, 7.0]]
    assert client.latest('iofq') == plain  # discarding it changes nothing
    assert client.latest('iofq-masked') == kept
    assert kept.supersedes == with_mask.id


@pytest.mark.xfail(
    raises=ImportError,
    strict=True,
    reason='no toy spec with a multi-dimensional output, nor one that takes a cut '
    'of it as a parameter',
)
def test_b4_explore_a_4d_volume(client: Client, measure: Measure) -> None:
    from ess.apps.examples import FIT, VOLUME

    reduced = client.run(VOLUME, {'run': measure(1)})
    volume = reduced.ref('volume')

    cuts = [
        client.view(volume, select={'qz': 0, 'energy_transfer': e}) for e in range(4)
    ]
    after_dragging = client.records()
    chosen = {'qz': 0, 'energy_transfer': 2}
    fit = client.run(FIT, {'volume': volume, 'select': chosen})

    assert [cut['dims'] for cut in cuts] == 4 * [['qx', 'qy']]
    assert after_dragging == [reduced]
    assert fit.request.refs() == [volume]
    assert fit.request.params['select'] == chosen


def test_b5_notebook_kernel_dies_mid_session(
    tmp_path: Path, catalogue: FakeDatasetSource, measure: Measure
) -> None:
    run = measure(1)

    def notebook() -> tuple[Client, Template]:
        """The notebook's first cells, as run after every start of its kernel."""
        client = local(
            tmp_path / 'notebook',
            instrument='dream',
            proposal='p1',
            submitter='simon',
            registry=registry(),
            sources=[catalogue],
        )
        data = client.run(LOAD, {'run': run}).ref('data')
        plot = Template(
            spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='plot'
        )
        return client, plot

    kernel, plot = notebook()
    for bins in (2, 3, 4):
        tuned = kernel.run(plot, {'bins': bins})
    seen = kernel.output(tuned, 'histogram')
    kernel.close()  # the kernel dies, and with it what the session held

    restarted, plot = notebook()
    last = restarted.latest('plot')
    again = restarted.run(plot, {'bins': last.request.params['bins']})

    assert last == tuned
    with pytest.raises(MissingCopyError):
        restarted.output(last, 'histogram')
    assert not again.reused  # recomputed: one full run
    assert again.supersedes == last.id
    assert equal(restarted.output(again, 'histogram'), seen)
    restarted.close()


def test_b6_find_last_weeks_result(client: Client, measure: Measure) -> None:
    run = measure(1)
    client.run(LOAD, {'run': run, 'scale': 2.0})
    tuesday = datetime.now(UTC)
    made = client.run(LOAD, {'run': run})
    wednesday = datetime.now(UTC)
    client.run(LOAD, {'run': run, 'scale': 3.0})

    (found,) = [r for r in client.records(since=tuesday) if r.created < wednesday]

    assert found == made
    assert found.request.params == {'run': {'dataset': 'run:dream/1'}, 'scale': 1.0}

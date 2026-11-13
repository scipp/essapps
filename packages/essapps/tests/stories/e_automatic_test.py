# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section E of docs/developer/user-stories.md: automatic reduction."""

# ruff: noqa: F821

from collections.abc import Callable, Sequence

import pytest

from ess.apps import Client
from ess.apps.batch import Batch, FileInfo, Row
from ess.apps.batch.auto import Automatic
from ess.apps.testing import FakeFiles
from ess.spec import FileRef

from . import toys

NO_PUBLISH = 'publication is not implemented'


def per_run(files: Sequence[FileInfo]) -> list[Row]:
    """A row per file the batch's selector matches: every raw file."""
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files]


def iofq_batch(client: Client) -> Batch:
    return Batch(
        client,
        toys.IOFQ,
        columns=('sample',),
        label='iofq',
        source=per_run,
        persist=True,
    )


def test_e1_series_grows_reduction_follows(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    reference = measure(1, [1.0, 1.0], role='reference')

    def angles(files: Sequence[FileInfo]) -> list[Row]:
        """A row per sample: all its angles so far, in run order."""
        runs: dict[str, list[FileRef]] = {}
        for f in sorted(files, key=lambda f: f.metadata['run']):
            if f.metadata.get('role') == 'sample':
                runs.setdefault(f.metadata['sample'], []).append(f.ref)
        return [Row(sample, {'runs': refs}) for sample, refs in runs.items()]

    batch = Batch(
        client,
        toys.STITCH,
        columns=('runs',),
        label='reflectivity',
        source=angles,
        persist=True,
    )
    auto = Automatic(batch, at='runs', settings=lambda: {'reference': reference})

    r2 = measure(2, [1.0, 2.0], role='sample', sample='si')
    client.wait(auto.step())
    r4 = measure(4, [4.0, 8.0], role='sample', sample='si')
    r3 = measure(3, [1.0, 1.0], role='sample', sample='si')
    measure(2, [1.0, 2.0], role='sample', sample='si')
    auto.step()

    user = connect()  # any client of the proposal
    latest = user.latest('reflectivity', member='si')
    assert len(user.records(label='reflectivity')) == 2
    # run order
    assert latest.request.params['runs'] == [r2, r3, r4]
    assert user.output(latest, 'stitched').curve.values.tolist() == [
        1.0,
        2.0,
        2.0,
        2.0,
        2.0,
        4.0,
    ]


def test_e2_automatic_reduction_goes_quiet(
    upgrade: Callable[..., Client], measure: toys.Measure
) -> None:
    upgraded = upgrade(specs=[toys.IOFQ_V2])  # a workspace without version 1
    auto = Automatic(
        iofq_batch(upgraded), at='sample', settings=lambda: {'normalization': None}
    )
    measure(1, [1.0, 1.0], role='sample')

    assert auto.step() == {}
    assert auto.refused == {'1': 'unknown spec sans-iofq/v1'}


@pytest.mark.xfail(reason=NO_PUBLISH)
def test_e3_reduction_of_our_own_output(
    client: Client, measure: toys.Measure, files: FakeFiles
) -> None:
    auto = Automatic(
        iofq_batch(client), at='sample', settings=lambda: {'normalization': None}
    )
    measure(1, [1.0, 2.0, 3.0, 4.0])
    (reduced,) = auto.step().values()
    pid = client.publish(reduced.ref('iofq'), 'scicat')
    files.add_published(scicat.entries[pid])

    assert auto.step() == {}


def test_e4_template_improved_during_a_beamtime(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    settings = {'normalization': None}
    auto = Automatic(iofq_batch(client), at='sample', settings=lambda: settings)
    first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    (before,) = auto.step().values()

    settings = {'reduction': toys.Reduction(threshold=1.5), 'normalization': None}
    second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    (after,) = auto.step().values()

    user = connect()  # any client of the proposal
    assert after.request.files() == [second]
    assert [user.output(r, 'iofq').values.tolist() for r in (before, after)] == [
        [3.0, 7.0],
        [2.0, 7.0],
    ]
    stale = [
        r
        for r in user.records(label='iofq')
        if r.request.params['reduction']['threshold'] != 1.5
    ]
    assert [r.request.files() for r in stale] == [[first]]

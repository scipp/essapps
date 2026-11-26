# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section E of docs/developer/user-stories.md: automatic reduction."""

# ruff: noqa: F821

from collections.abc import Callable, Sequence

import pandas as pd
import pytest
from ess.batch import Batch, FileInfo, RowSource, rows_of
from ess.batch.auto import step

from ess.apps import Client, Record
from ess.apps.testing import FakeFiles

from . import toys

NO_PUBLISH = 'publication is not implemented'


def per_run(files: Sequence[FileInfo]) -> pd.DataFrame:
    """A row per file the source's selector matches: every raw file."""
    return pd.DataFrame(
        {'sample': [f.ref.file for f in files]},
        index=[str(f.metadata['run']) for f in files],
    )


PER_RUN = RowSource(per_run, source_columns=('sample',))


def iofq_batch(client: Client) -> Batch:
    return Batch(
        client,
        toys.IOFQ,
        label='iofq',
        table=rows_of(client, PER_RUN),
        persist=True,
        settings={'normalization': None},
    )


def angles(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    A row per sample: its runs so far, in run order, keyed by the sample and
    the runs, so that a list that grows is a new row, whatever the order the
    runs are listed in.
    """
    runs: dict[str, list[FileInfo]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        if f.metadata.get('role') == 'sample':
            runs.setdefault(f.metadata['sample'], []).append(f)
    keys = [
        f'{sample}@{",".join(str(f.metadata["run"]) for f in found)}'
        for sample, found in runs.items()
    ]
    return pd.DataFrame(
        {
            'sample': list(runs),
            'runs': [[f.ref.file for f in found] for found in runs.values()],
        },
        index=keys,
    )


def test_e1_series_grows_reduction_follows(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    reference = measure(1, [1.0, 1.0], role='reference')
    source = RowSource(angles, source_columns=('runs',))
    batch = Batch(
        client,
        toys.STITCH,
        label='reflectivity',
        table=rows_of(client, source),
        persist=True,
        settings={'reference': reference},
    )
    counts = {2: [1.0, 2.0], 3: [1.0, 1.0], 4: [4.0, 8.0]}

    seen_keys: frozenset[str] = frozenset()
    reduced: dict[str, Record] = {}
    # Run 4 is listed before run 3, and run 2 is listed again at the end.
    for run, key in [(2, 'si@2'), (4, 'si@2,4'), (3, 'si@2,3,4'), (2, None)]:
        measure(run, counts[run], role='sample', sample='si')
        done = step(batch, source, seen_keys)
        seen_keys = done.seen_keys
        reduced |= done.records
        # The newest row only; the same list again is the same key: no row.
        assert list(done.records) == ([] if key is None else [key])
        client.wait(done.records)

    states = batch.state()
    user = connect()  # any client of the proposal
    newest = user.latest('reflectivity', member='si@2,3,4')
    assert list(batch.table().index) == ['si@2', 'si@2,4', 'si@2,3,4']
    assert [len(r.request.params['runs']) for r in reduced.values()] == [1, 2, 3]
    assert user.output(newest, 'stitched').curve.values.tolist() == [
        1.0,
        2.0,
        2.0,
        2.0,
        2.0,
        4.0,
    ]
    # The earlier rows keep their results, and none is reduced again.
    assert {k: s.record for k, s in states.items()} == reduced
    assert {s.status for s in states.values()} == {'done'}
    assert len(user.records(label='reflectivity')) == 3


def test_e2_automatic_reduction_goes_quiet(
    upgrade: Callable[..., Client], measure: toys.Measure
) -> None:
    upgraded = upgrade(specs=[toys.IOFQ_V2])  # a workspace without version 1
    batch = iofq_batch(upgraded)
    measure(1, [1.0, 1.0], role='sample')

    done = step(batch, PER_RUN, ())
    assert done.records == {}
    assert done.submit_errors == {'1': 'unknown spec sans-iofq/v1'}


@pytest.mark.xfail(reason=NO_PUBLISH)
def test_e3_reduction_of_our_own_output(
    client: Client, measure: toys.Measure, files: FakeFiles
) -> None:
    batch = iofq_batch(client)
    measure(1, [1.0, 2.0, 3.0, 4.0])
    first = step(batch, PER_RUN, ())
    (reduced,) = first.records.values()
    pid = client.publish(reduced.ref('iofq'), 'scicat')
    files.add_published(scicat.entries[pid])

    assert step(batch, PER_RUN, first.seen_keys).records == {}


def test_e4_template_improved_during_a_beamtime(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    following = iofq_batch(client)
    first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    done = step(following, PER_RUN, ())
    (before,) = done.records.values()

    settings = {'reduction': toys.Reduction(threshold=1.5), 'normalization': None}
    following.settings = settings
    second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    (after,) = step(following, PER_RUN, done.seen_keys).records.values()

    user = connect()  # any client of the proposal
    assert after.request.files() == [second]
    assert [user.output(r, 'iofq').values.tolist() for r in (before, after)] == [
        [3.0, 7.0],
        [2.0, 7.0],
    ]
    batch = iofq_batch(user)
    batch.settings = settings
    states = batch.state()
    outdated = [k for k, s in states.items() if s.outdated]
    assert [states[k].record.request.files() for k in outdated] == [[first]]

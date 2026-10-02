# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section A of docs/developer/user-stories.md: getting data in."""

# ruff: noqa: F821

import pytest

from ess.apps import Client, SubmitError, dataset
from ess.apps.testing import FakeDatasets

from .conftest import IOFQ, Measure


@pytest.mark.xfail(reason='dataset(path=...) with a folder is not implemented')
def test_a1_browse_a_local_folder_next_to_a_catalogue_reference(
    client: Client, measure: Measure
) -> None:
    files = sorted(folder.glob('*.h5'))
    vanadium = measure(9, [5.0, 6.0], pid='20.500.12269/vanadium')
    named = [dataset(path=f) for f in files] + [dataset(pid='20.500.12269/vanadium')]
    plots = [client.compute(IOFQ, {'run': d}) for d in named]

    assert [client.output(p, 'iofq').values.tolist() for p in plots] == [
        [1.0, 2.0],
        [3.0, 4.0],
        [5.0, 6.0],
    ]
    assert plots[2].request.datasets() == [vanadium]


def test_a2_run_number_instead_of_file(client: Client, measure: Measure) -> None:
    run = measure(4711, [1.0, 2.0])
    result = client.compute(IOFQ, {'run': dataset(run=4711)})

    assert result.request.datasets() == [run]
    with pytest.raises(SubmitError):
        client.compute(IOFQ, {'run': dataset(run=4712)})
    assert client.records() == [result]


@pytest.mark.xfail(reason='removing a dataset (client.remove) is deferred')
def test_a4_mistaken_copy_into_the_shared_service(client: Client) -> None:
    private_file = next(folder.glob('*.h5'))
    first = client.compute(IOFQ, {'run': dataset(path=private_file)})
    (uploaded,) = first.request.datasets()
    client.remove(uploaded)

    with pytest.raises(SubmitError, match='run'):
        client.compute(IOFQ, first.request.params)
    assert client.records() == [first]


def test_a5_metadata_corrected_after_the_fact(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    runs = [measure(n, [1.0, 2.0], sample='water') for n in (1, 2, 3)]
    reduced = [client.compute(IOFQ, {'run': run}) for run in runs]
    datasets.correct(runs[1], sample='heavy water')

    (named,) = reduced[1].request.datasets()
    assert named == runs[1]
    assert client.datasets.metadata(named)['sample'] == 'heavy water'

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section A of docs/developer/user-stories.md: getting data in."""

# ruff: noqa: F821

import pytest

from ess.apps import Client, SubmitError, dataset

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

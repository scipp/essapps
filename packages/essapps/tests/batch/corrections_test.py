# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Corrections reach what lists files and what makes rows."""

from ess.apps import Client, Listing, Selector
from ess.apps.batch import Batch, CorrectedFiles, ready
from ess.apps.testing import FakeFiles
from ess.spec import File

from .toys import COLUMNS, STAGED, Measure, pair


def test_a_corrected_file_is_listed_as_corrected(
    fake: FakeFiles, files: CorrectedFiles
) -> None:
    run = fake.measure(1, 1.0, role='transmission')

    files.correct(run, role='sample')

    assert files.list(Selector(role='sample')) == [run]
    assert files.list(Selector(role='transmission')) == []
    assert files.metadata(run)['role'] == 'sample'


def test_reset_forgets_a_correction(fake: FakeFiles, files: CorrectedFiles) -> None:
    run = fake.measure(1, 1.0, role='transmission')
    files.correct(run, role='sample')

    files.reset(run)

    assert files.list(Selector(role='transmission')) == [run]


def test_a_form_offers_a_file_as_corrected(
    fake: FakeFiles, files: CorrectedFiles, client: Client
) -> None:
    run = fake.measure(1, 1.0, role='transmission')
    marker = File(kind='raw', role='sample')

    assert list(Listing(client).files(marker).values()) == []
    files.correct(run, role='sample')

    assert list(Listing(client).files(marker).values()) == [run]


def test_a_correction_reaches_the_rows_at_the_next_scan(
    fake: FakeFiles, files: CorrectedFiles, client: Client, sample: Measure
) -> None:
    run = sample(1, 'water', partner=False)
    partner = fake.measure(2, 1.0, role='sample', sample='water')
    batch = Batch(client, STAGED, columns=COLUMNS, label='scan', source=pair)
    batch.scan()
    assert ready(batch.state()) == []

    files.correct(partner, role='transmission')
    batch.scan()

    assert ready(batch.state()) == ['1']
    assert batch.rows['1'].values == {'sample': run, 'transmission': partner}


def test_a_run_marked_excluded_leaves_the_rows(
    fake: FakeFiles, files: CorrectedFiles, client: Client, sample: Measure
) -> None:
    run = sample(1, 'water')
    batch = Batch(client, STAGED, columns=COLUMNS, label='scan', source=pair)
    batch.scan()
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})

    files.correct(run, exclude=True)
    batch.scan()

    assert '1' not in batch.rows

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


def test_reset_of_a_field_forgets_its_correction_alone(
    fake: FakeFiles, files: CorrectedFiles
) -> None:
    run = fake.measure(1, 1.0, role='transmission', sample='water')
    files.correct(run, role='sample', sample='oil')

    files.reset(run, 'role')

    assert files.corrections(run) == {'sample': 'oil'}
    assert files.metadata(run)['role'] == 'transmission'
    files.reset(run, 'sample')
    assert files.corrections(run) == {}


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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The bank histogram, a stand-in for raw files of ESS, on files laid out as they
are: ``entry/instrument/loki_detector_<bank>/detector_events``.
"""

from pathlib import Path

import h5py
import pytest
import scipp as sc
from ess.batch import FileInfo, file_sources, open_workspace
from ess.instruments.loki.batch import per_run
from ess.instruments.loki.bindings import BINDINGS, bank_histogram_binding
from ess.instruments.loki.specs import BANK_HISTOGRAM, BankHistogramParams
from ess.instruments.loki.web import build
from fastapi.testclient import TestClient

from ess.apps.folder import write_files_table
from ess.spec import FileRef


def write_run(path: Path, offsets: dict[int, list[float]]) -> Path:
    """A raw file with the events of each bank at the offsets given, in ns."""
    with h5py.File(path, 'w') as f:
        for bank, values in offsets.items():
            events = f.create_group(
                f'entry/instrument/loki_detector_{bank}/detector_events'
            )
            data = events.create_dataset('event_time_offset', data=values)
            data.attrs['units'] = 'ns'
    return path


def test_counts_the_events_of_one_bank_by_time_of_arrival(tmp_path: Path) -> None:
    run = write_run(tmp_path / 'run.hdf', {0: [5.0], 1: [0.0, 1.0, 3.0, 4.0]})

    histogram = bank_histogram_binding.compute(
        BankHistogramParams(run=run, bank=1, bins=2)
    ).histogram

    assert sc.identical(
        histogram,
        sc.DataArray(
            sc.array(
                dims=['event_time_offset'],
                values=[2.0, 2.0],
                variances=[2.0, 2.0],
                unit='counts',
            ),
            coords={
                'event_time_offset': sc.array(
                    dims=['event_time_offset'], values=[0.0, 2.0, 4.0], unit='ns'
                )
            },
        ),
    )


def test_refuses_a_bank_that_the_file_lacks_or_without_events(tmp_path: Path) -> None:
    run = write_run(tmp_path / 'run.hdf', {0: []})

    with pytest.raises(ValueError, match='has no entry/instrument/loki_detector_1'):
        bank_histogram_binding.compute(BankHistogramParams(run=run, bank=1))
    with pytest.raises(ValueError, match=r'bank 0 of run\.hdf holds no events'):
        bank_histogram_binding.compute(BankHistogramParams(run=run, bank=0))


def test_a_row_per_raw_run_in_the_order_measured() -> None:
    def info(run: int, **metadata: object) -> FileInfo:
        return FileInfo(FileRef(file=f'pid:p/{run}'), {'run': str(run), **metadata})

    rows = per_run([info(2, title='second'), info(1, title='first', sample='water')])

    assert rows.index.tolist() == ['1', '2']
    assert rows['run'].tolist() == ['pid:p/1', 'pid:p/2']
    assert rows['name'].tolist() == ['first', 'second']
    assert rows['sample'].isna().tolist() == [False, True]


def test_a_run_of_two_files_is_a_row_per_file_keyed_by_its_identity() -> None:
    files = [
        FileInfo(FileRef(file='pid:p/a'), {'run': '1'}),
        FileInfo(FileRef(file='pid:p/b'), {'run': '1'}),
        FileInfo(FileRef(file='pid:p/c'), {'run': '2'}),
    ]

    assert per_run(files).index.tolist() == ['pid:p/a', 'pid:p/b', '2']


def test_the_page_of_raw_runs_draws_a_histogram_per_run(tmp_path: Path) -> None:
    (tmp_path / 'raw').mkdir()
    for r in (1, 2):
        write_run(tmp_path / 'raw' / f'{r}.hdf', {0: [1.0, 2.0, 3.0 * r]})
    write_files_table(
        tmp_path, [{'file': f'raw/{r}.hdf', 'run': r, 'kind': 'raw'} for r in (1, 2)]
    )
    client, files, definitions = open_workspace(
        tmp_path, BINDINGS, allowed=file_sources(['local'])
    )
    with client:
        app = build(client, files, definitions, first='raw-runs')
        manager = app.state.pages.intake.manager
        (label,) = manager.headers()
        spec = manager.batch(label).spec
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        http.post('/api/reduce', json={'which': 'remaining'})
        client.wait(client.records())
        state = http.get('/api/state').json()
        plot = http.post('/api/plot', json={}).json()

    assert spec == BANK_HISTOGRAM
    assert [r['status'] for r in state['rows']] == ['done', 'done']
    assert [curve['name'] for curve in plot['figure']['data']] == ['1', '2']

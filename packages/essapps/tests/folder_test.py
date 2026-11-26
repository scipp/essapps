# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A workspace kept in a folder."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated

import pandas as pd
import pydantic
import pytest
import scipp as sc

from ess.apps import Client, NotReadable, Request, Selector, Status, file, local
from ess.apps.folder import (
    ArrivingFiles,
    FolderStore,
    described,
    open_folder,
    scan_folder,
    write_files_table,
)
from ess.apps.testing import read_file
from ess.spec import (
    Binding,
    FileRef,
    Referenced,
    RunFile,
    WorkflowSpec,
    function_binding,
)


class Array(pydantic.BaseModel, arbitrary_types_allowed=True):
    data: sc.DataArray


class Curve(pydantic.BaseModel):
    iofq: Array


class NoParams(pydantic.BaseModel):
    pass


CURVE = WorkflowSpec(
    name='curve',
    version=1,
    title='Curve',
    description='A toy.',
    params=NoParams,
    outputs=Curve,
)


class Offset(pydantic.BaseModel):
    value: float = 0.0


class FindParams(pydantic.BaseModel):
    sample: RunFile


class FindOutputs(pydantic.BaseModel):
    offset: Offset


class PlainParams(pydantic.BaseModel):
    sample: RunFile
    transmission: RunFile
    thickness: Annotated[float, pydantic.Field(ge=0)] = 1.0
    offset: Offset | Referenced[Offset] | None = None


class PlainOutputs(pydantic.BaseModel):
    total: float


FIND = WorkflowSpec(
    name='find',
    version=1,
    title='Find',
    description='A toy.',
    params=FindParams,
    outputs=FindOutputs,
)
PLAIN = WorkflowSpec(
    name='plain',
    version=1,
    title='Plain',
    description='A toy.',
    params=PlainParams,
    outputs=PlainOutputs,
)


def _find(params: FindParams) -> FindOutputs:
    return FindOutputs(offset=Offset(value=read_file(params.sample)))


def _plain(params: PlainParams) -> PlainOutputs:
    offset = 0.0 if params.offset is None else params.offset.value
    return PlainOutputs(total=params.thickness + offset)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """
    A folder of runs 1 to 3, each holding its number; the catalogue lists run 3.
    """
    folder = tmp_path / 'workspace'
    folder.mkdir()
    lines = ['file,run,role,pid']
    for run in (1, 2, 3):
        (folder / f'run-{run}.json').write_text(str(run))
        pid = '20.500.12269/abc' if run == 3 else ''
        lines.append(f'run-{run}.json,{run},sample,{pid}')
    (folder / 'files.csv').write_text('\n'.join(lines))
    return folder


@pytest.fixture
def bindings() -> list[Binding]:
    return [
        function_binding(FIND, _find),
        function_binding(PLAIN, _plain),
    ]


@pytest.fixture
def open_client(folder: Path, bindings: list[Binding]) -> Callable[[], Client]:
    return lambda: open_folder(folder, bindings)


def find_offset(client: Client) -> str:
    record = client.submit(FIND, params={'sample': file(run=3)}, persist=True)
    client.wait([record])
    return record.id


def test_a_store_refuses_to_read_what_was_never_written(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        FolderStore(tmp_path).read('r1', 'out', float)


def test_a_second_session_reads_a_group_holding_an_array_that_the_first_persisted(
    folder: Path,
) -> None:
    x = sc.arange('x', 3.0, unit='m')
    array = sc.DataArray(x, coords={'x': x})
    bindings = [function_binding(CURVE, lambda params: Curve(iofq=Array(data=array)))]

    with open_folder(folder, bindings) as first:
        record = first.submit(CURVE, params={}, persist=True)
        first.wait([record])
    with open_folder(folder, bindings) as second:
        (record,) = second.records(ids=[record.id])
        read = second.output(record, 'iofq')

    assert isinstance(read, Array)
    assert sc.identical(read.data, array)


def test_a_second_session_lists_the_records_of_the_first(
    open_client: Callable[[], Client],
) -> None:
    with open_client() as first:
        record = first.submit(FIND, params={'sample': file(run=3)}, label='a')
        first.wait([record])

    with open_client() as second:
        assert [r.id for r in second.records(label='a')] == [record.id]


def test_a_second_session_reads_an_output_the_first_persisted(
    open_client: Callable[[], Client],
) -> None:
    with open_client() as first:
        found = find_offset(first)

    with open_client() as second:
        (record,) = second.records(ids=[found])
        assert second.output(record, 'offset') == Offset(value=3)


def test_a_request_of_a_second_session_references_a_persisted_output(
    open_client: Callable[[], Client],
) -> None:
    with open_client() as first:
        found = find_offset(first)

    with open_client() as second:
        (record,) = second.records(ids=[found])
        total = second.submit(
            PLAIN,
            params={
                'sample': file(run=1),
                'transmission': file(run=2),
                'thickness': 1.0,
                'offset': record.ref('offset'),
            },
        )
        second.wait([total])
        assert second.output(total, 'total') == 4.0


def test_a_cleared_record_stays_cleared_in_a_second_session_and_its_files_are_gone(
    open_client: Callable[[], Client], folder: Path
) -> None:
    with open_client() as first:
        (record,) = first.records(ids=[find_offset(first)])
        first.clear(record)

    assert not (folder / 'store' / record.id).exists()
    with open_client() as second:
        assert second.cleared(record) is True
        assert second.status(record) is Status.COMPLETED
        with pytest.raises(NotReadable, match='its outputs are cleared'):
            second.output(record, 'offset')


def test_a_moved_folder_keeps_the_identity_of_its_files_and_what_was_done_with_them(
    folder: Path, bindings: list[Binding]
) -> None:
    with open_folder(folder, bindings) as first:
        found = [
            first.submit(FIND, params={'sample': file(run=run)}, persist=True)
            for run in (1, 2, 3)
        ]
        first.wait(found)
    files = [r.request.params['sample'] for r in found]
    assert files == [
        FileRef(file='path:run-1.json'),
        FileRef(file='path:run-2.json'),
        FileRef(file='pid:20.500.12269/abc'),
    ]

    with open_folder(folder.rename(folder.with_name('moved')), bindings) as second:
        assert second.files.list(Selector()) == files
        assert [second.output(r, 'offset').value for r in found] == [1, 2, 3]
        again = second.submit(found[0].request)
        second.wait([again])
        assert second.output(again, 'offset').value == 1


def test_a_file_that_gains_a_pid_resolves_to_another_identity_than_a_record_holds(
    tmp_path: Path, bindings: list[Binding]
) -> None:
    (tmp_path / 'run-1.json').write_text('1')
    row = {'file': 'run-1.json', 'run': 1}
    write_files_table(tmp_path, [row])
    request = Request(FIND, params={'sample': file(run=1)})
    with local(proposal='p1', files=ArrivingFiles(tmp_path), bind=bindings) as client:
        record = client.compute(request)
        write_files_table(tmp_path, [{**row, 'pid': '20.500.12269/abc'}])
        resolved = client.resolve(request)

    assert record.request.params['sample'] == FileRef(file='path:run-1.json')
    assert resolved.params['sample'] == FileRef(file='pid:20.500.12269/abc')


def test_a_folder_held_by_a_client_cannot_be_opened_again(
    open_client: Callable[[], Client],
) -> None:
    with open_client(), pytest.raises(RuntimeError):
        open_client()


def test_a_folder_without_a_file_table_is_refused(
    tmp_path: Path, bindings: Sequence[Binding]
) -> None:
    with pytest.raises(FileNotFoundError):
        open_folder(tmp_path, bindings)


def by_suffix(path: Path) -> dict[str, object] | None:
    return {'run': int(path.stem)} if path.suffix == '.json' else None


def test_a_scan_gives_a_row_for_each_file_that_the_reader_knows_by_run(
    tmp_path: Path,
) -> None:
    (tmp_path / 'raw').mkdir()
    for name in ('raw/2.json', 'raw/1.json', '3.json', 'notes.txt'):
        (tmp_path / name).write_text('1.0')

    assert scan_folder(tmp_path, by_suffix) == [
        {'file': 'raw/1.json', 'run': 1},
        {'file': 'raw/2.json', 'run': 2},
        {'file': '3.json', 'run': 3},
    ]


def test_a_scan_leaves_out_what_the_workspace_keeps_in_its_folder(
    tmp_path: Path,
) -> None:
    (tmp_path / 'store' / 'r1').mkdir(parents=True)
    for name in ('store/r1/7.json', 'files.csv', 'log.jsonl', '1.json'):
        (tmp_path / name).write_text('1.0')

    assert scan_folder(tmp_path, lambda path: {}) == [{'file': '1.json'}]


def test_a_scan_puts_the_runs_first_by_number_and_then_the_other_files_by_path(
    tmp_path: Path,
) -> None:
    runs = {'a.json': 10, 'b.json': 9, 'c.json': 100}
    for name in (*runs, 'z.dat', 'm.dat'):
        (tmp_path / name).write_text('1.0')

    def read(path: Path) -> dict[str, object]:
        return {'run': runs[path.name]} if path.name in runs else {}

    assert [row['file'] for row in scan_folder(tmp_path, read)] == [
        'b.json',
        'a.json',
        'c.json',
        'm.dat',
        'z.dat',
    ]


def test_a_scan_refuses_two_files_of_one_run(tmp_path: Path) -> None:
    for name in ('a.json', 'b.json'):
        (tmp_path / name).write_text('1.0')

    with pytest.raises(ValueError, match='run'):
        scan_folder(tmp_path, lambda path: {'run': 1})


def test_a_scan_leaves_out_a_file_that_cannot_be_read_with_a_warning(
    tmp_path: Path,
) -> None:
    for name in ('1.json', '2.json'):
        (tmp_path / name).write_text('1.0')

    def read(path: Path) -> dict[str, object]:
        if path.name == '1.json':
            raise PermissionError('no permission')
        return {}

    with pytest.warns(UserWarning, match=r'1\.json is left out: no permission'):
        rows = scan_folder(tmp_path, read)

    assert rows == [{'file': '2.json'}]


# The files table described by a run table


@pytest.fixture
def scanned() -> pd.DataFrame:
    """The table of a scan: two runs with a title, and a file that is no run."""
    return pd.DataFrame(
        {
            'file': ['1.nxs', '2.nxs', 'beam.h5'],
            'run': ['1', '2', ''],
            'title': ['one', 'two', ''],
        }
    )


def test_a_file_gets_what_the_run_table_says_of_its_run(scanned: pd.DataFrame) -> None:
    run_table = pd.DataFrame({'run': [2, 1], 'role': ['transmission', 'sample']})

    files = described(scanned, run_table)

    assert list(files.columns) == ['file', 'run', 'title', 'role']
    assert list(files['role']) == ['sample', 'transmission', '']


def test_a_run_of_the_run_table_without_a_file_is_ignored(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', '7'], 'role': ['sample', 'sample']})

    files = described(scanned, run_table)

    assert list(files['file']) == ['1.nxs', '2.nxs', 'beam.h5']
    assert list(files['role']) == ['sample', '', '']


def test_a_row_of_the_run_table_without_a_run_describes_the_file_it_names(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame(
        {'run': ['1', ''], 'file': ['', 'beam.h5'], 'role': ['sample', 'direct_beam']}
    )

    files = described(scanned, run_table)

    assert list(files['role']) == ['sample', '', 'direct_beam']


def test_a_cell_of_the_run_table_replaces_what_the_file_says_unless_it_is_empty(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', '2'], 'title': ['water', '']})

    files = described(scanned, run_table)

    assert list(files['title']) == ['water', 'two', '']


def test_two_rows_of_the_run_table_for_one_run_are_refused(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', 1], 'role': ['sample', 'background']})

    with pytest.raises(
        ValueError, match='two rows of the run table describe the run 1'
    ):
        described(scanned, run_table)


def test_a_row_of_the_run_table_that_names_neither_run_nor_file_is_refused(
    scanned: pd.DataFrame,
) -> None:
    with pytest.raises(ValueError, match='neither a run nor a file'):
        described(scanned, pd.DataFrame({'run': [''], 'role': ['sample']}))


def test_arriving_files_reads_the_table_again_when_it_is_replaced(
    tmp_path: Path,
) -> None:
    def register(*runs: int) -> None:
        write_files_table(tmp_path, [{'file': f'{r}.nxs', 'run': r} for r in runs])

    for run in (1, 2):
        (tmp_path / f'{run}.nxs').touch()
    files = ArrivingFiles(tmp_path)
    register(1)
    assert [files.metadata(r)['run'] for r in files.list(Selector())] == ['1']

    register(1, 2)

    assert [files.metadata(r)['run'] for r in files.list(Selector())] == ['1', '2']
    assert files.resolve(file(run=2)) == files.list(Selector())[1]


def test_arriving_files_lists_no_file_until_the_table_is_written(
    tmp_path: Path,
) -> None:
    (tmp_path / '1.nxs').touch()
    files = ArrivingFiles(tmp_path)
    before = files.list(Selector())

    write_files_table(tmp_path, [{'file': '1.nxs', 'run': 1}])

    assert (before, len(files.list(Selector()))) == ([], 1)

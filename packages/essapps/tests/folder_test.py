# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A workspace kept in a folder."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pydantic
import pytest
import scipp as sc

from ess.apps import Client, NotReadable, file
from ess.apps.folder import FolderStore, open_folder, scan_folder
from ess.apps.testing import read_file
from ess.spec import Binding, Referenced, WorkflowSpec, function_binding

from .batch import toys


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


def _find(params: toys.FindParams) -> toys.FindOutputs:
    return toys.FindOutputs(offset=toys.Offset(value=read_file(params.sample)))


def _plain(params: toys.Params) -> toys.Outputs:
    offset = 0.0 if params.offset is None else params.offset.value
    return toys.Outputs(total=params.thickness + offset)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A folder of runs 1 to 3, each holding its number."""
    lines = ['file,run,role']
    for run in (1, 2, 3):
        (tmp_path / f'run-{run}.json').write_text(str(run))
        lines.append(f'run-{run}.json,{run},sample')
    (tmp_path / 'files.csv').write_text('\n'.join(lines))
    return tmp_path


@pytest.fixture
def bindings() -> list[Binding]:
    return [
        function_binding(toys.FIND, _find),
        function_binding(toys.PLAIN, _plain),
    ]


@pytest.fixture
def open_client(folder: Path, bindings: list[Binding]) -> Callable[[], Client]:
    return lambda: open_folder(folder, bindings)


def find_offset(client: Client) -> str:
    record = client.submit(toys.FIND, params={'sample': file(run=3)}, persist=True)
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
        record = first.submit(toys.FIND, params={'sample': file(run=3)}, label='a')
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
        assert second.output(record, 'offset') == toys.Offset(value=3)


def test_a_session_that_does_not_run_the_spec_of_a_record_cannot_read_its_output(
    folder: Path, bindings: list[Binding]
) -> None:
    with open_folder(folder, bindings) as first:
        found = find_offset(first)

    with open_folder(folder, [bindings[1]]) as second:
        (record,) = second.records(ids=[found])
        with pytest.raises(NotReadable, match='does not run'):
            second.output(record, 'offset')


def test_a_request_of_a_second_session_references_a_persisted_output(
    open_client: Callable[[], Client],
) -> None:
    with open_client() as first:
        found = find_offset(first)

    with open_client() as second:
        (record,) = second.records(ids=[found])
        total = second.submit(
            toys.PLAIN,
            params={
                'sample': file(run=1),
                'transmission': file(run=2),
                'thickness': 1.0,
                'offset': record.ref('offset'),
            },
        )
        second.wait([total])
        assert second.output(total, 'total') == 4.0


class Centre(pydantic.BaseModel):
    x: float


class Centred(pydantic.BaseModel):
    centre: Centre


class CentreWithZ(pydantic.BaseModel):
    """``Centre`` after a field was added, the spec's version unchanged."""

    x: float
    z: float


class CentredWithZ(pydantic.BaseModel):
    centre: CentreWithZ


class UseParams(pydantic.BaseModel):
    centre: Referenced[CentreWithZ]


def _centre_spec(outputs: type[pydantic.BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='A toy.',
        params=NoParams,
        outputs=outputs,
    )


USE = WorkflowSpec(
    name='use',
    version=1,
    title='Use',
    description='A toy.',
    params=UseParams,
    outputs=toys.Outputs,
)


def test_an_output_persisted_before_its_spec_gained_a_field_is_not_read(
    folder: Path,
) -> None:
    old = _centre_spec(Centred)
    with open_folder(
        folder, [function_binding(old, lambda _: Centred(centre=Centre(x=1.0)))]
    ) as first:
        record = first.submit(old, params={}, persist=True)
        first.wait([record])

    changed = _centre_spec(CentredWithZ)
    bindings = [
        function_binding(changed, lambda _: None),
        function_binding(USE, lambda params: toys.Outputs(total=params.centre.z)),
    ]
    message = "field 'z' of CentreWithZ is missing. The spec's outputs changed"
    with open_folder(folder, bindings) as second:
        (record,) = second.records(ids=[record.id])
        with pytest.raises(NotReadable, match=message):
            second.output(record, 'centre')
        use = second.submit(USE, params={'centre': record.ref('centre')})
        second.wait([use])
        assert message in second.failure(use)


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

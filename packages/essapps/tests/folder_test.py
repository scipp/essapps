# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A workspace kept in a folder."""

from collections.abc import Callable, Sequence
from pathlib import Path

import pydantic
import pytest
import scipp as sc

from ess.apps import Client, file
from ess.apps.folder import FolderStore, open_folder
from ess.apps.testing import read_file
from ess.spec import Binding, function_binding

from .batch import toys


class Array(pydantic.BaseModel, arbitrary_types_allowed=True):
    data: sc.DataArray


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


def test_a_store_reads_back_a_model_holding_an_array(tmp_path: Path) -> None:
    store = FolderStore(tmp_path)
    x = sc.arange('x', 3.0, unit='m')
    array = sc.DataArray(x, coords={'x': x})

    store.write('r1', 'out', Array(data=array))

    assert sc.identical(store.read('r1', 'out').data, array)


def test_a_store_reads_back_a_data_group_with_an_array_and_a_list(
    tmp_path: Path,
) -> None:
    store = FolderStore(tmp_path)
    x = sc.arange('x', 3.0, unit='m')
    group = sc.DataGroup(array=sc.DataArray(x, coords={'x': x}), items=[1, 'a'])

    store.write('r1', 'out', group)

    read = store.read('r1', 'out')
    assert isinstance(read, sc.DataGroup)
    assert sc.identical(read['array'], group['array'])
    assert read['items'] == [1, 'a']


def test_a_store_refuses_to_read_what_was_never_written(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        FolderStore(tmp_path).read('r1', 'out')


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

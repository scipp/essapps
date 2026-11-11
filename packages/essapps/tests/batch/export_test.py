# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The files taken away from a batch: the outputs of a record, and of a table."""

import json
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pydantic
import pytest
import scipp as sc

from ess.apps import Client, Record, Template, file, hdf5
from ess.apps.batch.export import export_table, write_outputs
from ess.apps.batch.tables import reduce_table
from ess.apps.folder import open_folder
from ess.spec import Binding, WorkflowSpec, function_binding

from . import toys
from .conftest import BAD


class Array(pydantic.BaseModel, arbitrary_types_allowed=True):
    data: sc.DataArray


class Curve(pydantic.BaseModel):
    n: int = 3


class Many(pydantic.BaseModel):
    curves: list[Array]
    note: toys.Offset | None = None
    total: float


def _array(n: int) -> Array:
    x = sc.arange('q', float(n))
    return Array(data=sc.DataArray(x, coords={'q': x}))


def _many(params: Curve) -> Many:
    return Many(curves=[_array(params.n), _array(params.n + 1)], total=1.5)


def _spec(name: str, outputs: type[pydantic.BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description='A toy.',
        params=Curve,
        outputs=outputs,
    )


MANY = _spec('many', Many)


class Named(pydantic.BaseModel):
    status: float


NAMED = _spec('named', Named)


@pytest.fixture
def client(folder: Path, folder_bindings: list[Binding]) -> Iterator[Client]:
    """A client that also runs the workflows that output arrays."""
    bindings = [
        *folder_bindings,
        function_binding(MANY, _many),
        function_binding(NAMED, lambda params: Named(status=1.0)),
    ]
    with open_folder(folder, bindings) as client:
        yield client


def run(client: Client, spec: WorkflowSpec, **params: object) -> Record:
    record = client.submit(spec, params=params, persist=True)
    client.wait([record])
    return record


def test_the_record_is_written_with_its_outputs(client: Client, tmp_path: Path) -> None:
    record = run(client, toys.FIND, sample=file(run=3))

    write_outputs(client, record, tmp_path / 'out')

    written = json.loads((tmp_path / 'out' / '_record.json').read_text())
    assert written['id'] == record.id


def test_each_output_returned_is_a_file_and_an_output_left_out_has_none(
    client: Client, tmp_path: Path
) -> None:
    record = run(client, MANY, n=2)

    paths = write_outputs(client, record, tmp_path / 'out')

    assert paths == {
        'curves': tmp_path / 'out' / 'curves.h5',
        'total': tmp_path / 'out' / 'total.h5',
    }
    assert sorted(p.name for p in (tmp_path / 'out').iterdir()) == [
        '_record.json',
        'curves.h5',
        'total.h5',
    ]


def test_each_completed_row_is_exported_under_its_key(
    client: Client, template: Template, table: pd.DataFrame, tmp_path: Path
) -> None:
    out = tmp_path / 'export'
    reduced = reduce_table(client, template, table, label='scan')

    result = export_table(client, reduced, out)

    assert list(result['total']) == [str(out / k / 'total.h5') for k in 'ab']
    assert hdf5.read(float, out / 'b' / 'total.h5') == 1.0
    assert (out / 'a' / '_record.json').is_file()


def test_a_row_that_failed_has_no_folder_and_empty_output_columns(
    client: Client, template: Template, tmp_path: Path
) -> None:
    table = pd.DataFrame(
        {'name': ['a', 'b'], 'sample': ['1', str(BAD)], 'transmission': ['1001'] * 2}
    )
    out = tmp_path / 'export'
    reduced = reduce_table(client, template, table, label='scan')

    result = export_table(client, reduced, out)

    assert list(result['total']) == [str(out / 'a' / 'total.h5'), '']
    assert sorted(p.name for p in out.iterdir()) == ['a']


def test_a_table_without_records_is_refused(client: Client, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='record'):
        export_table(client, pd.DataFrame({'key': ['a']}), tmp_path / 'export')


@pytest.mark.parametrize('key', ['../a', 'a/b', '', '..'])
def test_a_key_that_is_not_a_folder_name_is_refused_and_nothing_is_written(
    client: Client, template: Template, tmp_path: Path, key: str
) -> None:
    table = pd.DataFrame({'name': ['ok', key], 'sample': ['1', '2']})
    table['transmission'] = '1001'
    reduced = reduce_table(client, template, table, label='scan')
    out = tmp_path / 'parent' / 'export'

    with pytest.raises(ValueError, match='folder'):
        export_table(client, reduced, out)

    assert not (tmp_path / 'parent').exists()


def test_a_table_exports_the_records_it_names_after_later_reductions(
    client: Client, template: Template, table: pd.DataFrame, tmp_path: Path
) -> None:
    older = reduce_table(client, template, table, label='scan')
    newer = reduce_table(client, template, older, label='scan')

    export_table(client, older, tmp_path / 'export')

    ids = [
        json.loads((tmp_path / 'export' / k / '_record.json').read_text())['id']
        for k in 'ab'
    ]
    assert ids == list(older['record'])
    assert not set(ids) & set(newer['record'])


def test_a_record_that_the_workspace_lacks_is_refused_and_nothing_is_written(
    client: Client, template: Template, table: pd.DataFrame, tmp_path: Path
) -> None:
    reduced = reduce_table(client, template, table, label='scan')
    reduced.loc[1, 'record'] = 'no-such-record'
    out = tmp_path / 'export'

    with pytest.raises(ValueError, match='no-such-record'):
        export_table(client, reduced, out)

    assert not out.exists()


def test_a_key_that_two_rows_share_is_refused_and_nothing_is_written(
    client: Client, template: Template, table: pd.DataFrame, tmp_path: Path
) -> None:
    reduced = reduce_table(client, template, table, label='scan')
    reduced['name'] = 'a'
    out = tmp_path / 'export'

    with pytest.raises(ValueError, match='one key'):
        export_table(client, reduced, out)

    assert not out.exists()


def test_an_output_named_as_a_column_of_the_result_is_refused_and_nothing_is_written(
    client: Client, tmp_path: Path
) -> None:
    record = run(client, NAMED)
    table = pd.DataFrame({'key': ['a'], 'status': ['completed'], 'record': [record.id]})

    with pytest.raises(ValueError, match='status'):
        export_table(client, table, tmp_path / 'export')

    assert not (tmp_path / 'export').exists()

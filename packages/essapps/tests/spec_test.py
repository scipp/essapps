# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import pytest
import scipp as sc
from pydantic import BaseModel, ValidationError

from ess.apps.spec import (
    Array,
    ArraySpec,
    ArrayValue,
    Kind,
    NexusFile,
    Quantity,
    Ref,
    as_ref,
    data_ref_fields,
    ref_fields,
    walk_refs,
)


class Params(BaseModel):
    data: ArrayValue
    background: Array(ArraySpec(dims=('x',))) | None = None
    runs: list[NexusFile] = []
    banks: dict[str, Array(ArraySpec(dims=('tof',)))] = {}
    centre: Quantity | Ref | None = None
    label: str = ''


REF = {'record': 'r1', 'output': 'data'}


def test_data_ref_fields_include_optionals_and_collections() -> None:
    fields = data_ref_fields(Params)
    assert set(fields) == {'data', 'background', 'runs', 'banks'}
    assert fields['data'].kind is Kind.ARRAY
    assert fields['background'].array == ArraySpec(dims=('x',))
    assert fields['banks'].array == ArraySpec(dims=('tof',))
    assert fields['runs'].kind is Kind.NEXUS
    assert ref_fields(Params) == {'data', 'background', 'runs', 'banks', 'centre'}


def test_array_field_accepts_a_reference_or_a_scipp_object_only() -> None:
    assert Params(data=REF).data == Ref(record='r1', output='data')
    assert Params(data=sc.scalar(1.0)).data.value == 1.0
    for bad in ('a path', 3, [1, 2], {'x': 1}):
        with pytest.raises(ValidationError):
            Params(data=bad)


def test_json_schema_marks_data_fields() -> None:
    schema = Params.model_json_schema()['properties']
    assert schema['data']['dataRef'] == {'kind': 'array'}
    assert schema['runs']['items']['dataRef'] == {'kind': 'nexus'}


def test_walk_refs_finds_references_at_any_depth() -> None:
    params = {
        'data': REF,
        'runs': [
            {'record': 'f1', 'output': 'file'},
            {'record': 'f2', 'output': 'file'},
        ],
        'banks': {'a': {'record': 'r2', 'output': 'banks', 'key': 'a'}},
        'centre': {'value': 1.0, 'unit': 'm'},
    }
    assert [(p, str(r)) for p, r in walk_refs(params)] == [
        ('data', 'r1.data'),
        ('runs[0]', 'f1.file'),
        ('runs[1]', 'f2.file'),
        ('banks.a', 'r2.banks[a]'),
    ]


def test_as_ref_decides_what_a_reference_is() -> None:
    assert as_ref(Ref(record='r', output='o')) == Ref(record='r', output='o')
    assert as_ref(REF) == Ref(record='r1', output='data')
    assert as_ref({'record': 'r1', 'output': 'o', 'extra': 1}) is None
    assert as_ref({'value': 1.0}) is None
    assert as_ref('r1.data') is None

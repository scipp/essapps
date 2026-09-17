# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest
import scipp as sc
from pydantic import BaseModel, ValidationError

from ess.apps.spec import (
    Array,
    ArraySpec,
    DatasetRef,
    Format,
    NexusFile,
    Quantity,
    Ref,
    WorkflowSpec,
    as_ref,
    data_ref_fields,
    ref_fields,
    walk_refs,
)


class Params(BaseModel):
    data: Array()
    background: Array(ArraySpec(dims=('x',))) | None = None
    runs: list[NexusFile] = []
    banks: dict[str, Array(ArraySpec(dims=('tof',)))] = {}
    centre: Quantity | Ref | None = None
    label: str = ''


REF = {'record': 'r1', 'output': 'data'}


def test_data_ref_fields_include_optionals_and_collections() -> None:
    fields = data_ref_fields(Params)
    assert set(fields) == {'data', 'background', 'runs', 'banks'}
    assert fields['data'].format is Format.SCIPP
    assert fields['background'].array == ArraySpec(dims=('x',))
    assert fields['banks'].array == ArraySpec(dims=('tof',))
    assert fields['runs'].format is Format.NEXUS
    assert ref_fields(Params) == {'data', 'background', 'runs', 'banks', 'centre'}


def test_a_data_field_holds_a_reference_only() -> None:
    assert Params(data=REF).data == Ref(record='r1', output='data')
    for bad in (sc.scalar(1.0), 'a path', Path('/data/x.h5'), 3, [1, 2], {'x': 1}):
        with pytest.raises(ValidationError):
            Params(data=bad)


def test_json_schema_marks_data_fields() -> None:
    schema = Params.model_json_schema()['properties']
    assert schema['data']['dataRef'] == {'format': 'scipp'}
    assert schema['runs']['items']['dataRef'] == {'format': 'nexus'}


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


def test_as_ref_tells_a_dataset_from_a_params_dict() -> None:
    dataset = DatasetRef(instrument='dream', run=4711)
    assert as_ref(dataset) is dataset
    assert as_ref(dataset.model_dump()) == dataset
    assert as_ref(dataset.model_dump(mode='json')) == dataset
    assert as_ref({'pid': '20.500/abc'}) == DatasetRef(pid='20.500/abc')
    assert as_ref({'run': 4711}) is None
    assert as_ref({'run': Ref(record='r1', output='data')}) is None
    assert as_ref({'pid': None, 'instrument': None, 'run': None, 'path': None}) is None


def test_a_dataset_has_exactly_one_identity() -> None:
    assert str(DatasetRef(pid='20.500/abc')) == 'dataset:20.500/abc'
    assert str(DatasetRef(instrument='dream', run=4711)) == 'dataset:dream/4711'
    assert str(DatasetRef(path=Path('/data/x.nxs'))) == 'dataset:/data/x.nxs'
    with pytest.raises(ValidationError, match='exactly one identity'):
        DatasetRef(pid='20.500/abc', path=Path('/data/x.nxs'))
    with pytest.raises(ValidationError, match='exactly one identity'):
        DatasetRef()
    with pytest.raises(ValidationError, match='together'):
        DatasetRef(instrument='dream')


def test_a_data_field_holds_either_form_of_reference() -> None:
    dataset = DatasetRef(instrument='dream', run=4711)
    assert Params(data=REF, runs=[dataset]).runs == [dataset]
    params = {'data': REF, 'runs': [dataset.model_dump()]}
    assert [(p, str(r)) for p, r in walk_refs(params)] == [
        ('data', 'r1.data'),
        ('runs[0]', 'dataset:dream/4711'),
    ]


def spec(outputs: type[BaseModel], **fields: object) -> WorkflowSpec:
    return WorkflowSpec(
        name='combining',
        version=1,
        title='Combining',
        description='A workflow that declares a contribution.',
        outputs=outputs,
        **fields,
    )


def test_a_contribution_needs_the_other_outputs_optional() -> None:
    """A member run returns the contribution alone, against the full model."""

    class Required(BaseModel):
        contribution: Array()
        normalized: Array()

    class Optional(BaseModel):
        contribution: Array()
        normalized: Array() | None = None

    with pytest.raises(ValidationError, match="\\['normalized'\\] must be optional"):
        spec(Required, contribution='contribution')
    assert spec(Optional, contribution='contribution').contribution == 'contribution'
    assert spec(Required).contribution is None

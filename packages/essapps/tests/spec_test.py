# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest
import scipp as sc
from pydantic import BaseModel, ValidationError

from ess.apps.spec import (
    Array,
    ArraySpec,
    Format,
    NexusFile,
    OutputRef,
    Quantity,
    SerializedWorkflowSpec,
    WorkflowSpec,
    as_ref,
    data_fields,
    dataset_path,
    dataset_ref,
    parse_ref,
    ref_fields,
    walk_refs,
)


class Params(BaseModel):
    data: Array()
    background: Array(ArraySpec(dims=('x',))) | None = None
    runs: list[NexusFile] = []
    banks: dict[str, Array(ArraySpec(dims=('tof',)))] = {}
    centre: Quantity | OutputRef | None = None
    label: str = ''


REF = {'record': 'r1', 'output': 'data'}


def test_data_fields_include_optionals_and_collections() -> None:
    fields = data_fields(Params)
    assert set(fields) == {'data', 'background', 'runs', 'banks'}
    assert fields['data'].format is Format.SCIPP
    assert fields['background'].array == ArraySpec(dims=('x',))
    assert fields['banks'].array == ArraySpec(dims=('tof',))
    assert fields['runs'].format is Format.NEXUS
    assert ref_fields(Params) == {'data', 'background', 'runs', 'banks', 'centre'}


def test_a_data_field_holds_a_reference_only() -> None:
    assert Params(data=REF).data == OutputRef(record='r1', output='data')
    for bad in (sc.scalar(1.0), 'a path', Path('/data/x.h5'), 3, [1, 2], {'x': 1}):
        with pytest.raises(ValidationError):
            Params(data=bad)


def test_json_schema_marks_data_fields() -> None:
    schema = Params.model_json_schema()['properties']
    assert schema['data']['dataField'] == {'format': 'scipp'}
    assert schema['runs']['items']['dataField'] == {'format': 'nexus'}


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
    assert as_ref(OutputRef(record='r', output='o')) == OutputRef(
        record='r', output='o'
    )
    assert as_ref(REF) == OutputRef(record='r1', output='data')
    assert as_ref({'record': 'r1', 'output': 'o', 'extra': 1}) is None
    assert as_ref({'value': 1.0}) is None
    assert as_ref('r1.data') is None


def test_as_ref_tells_a_dataset_from_a_params_dict() -> None:
    dataset = dataset_ref(instrument='dream', run=4711)
    assert as_ref(dataset) is dataset
    assert as_ref(dataset.model_dump()) == dataset
    assert as_ref(dataset.model_dump(mode='json')) == dataset
    assert as_ref({'pid': '20.500/abc'}) is None
    assert as_ref({'path': '/data/x.nxs'}) is None
    assert as_ref({'run': OutputRef(record='r1', output='data')}) is None


def test_a_dataset_has_exactly_one_identity() -> None:
    assert str(dataset_ref(pid='20.500/abc')) == 'pid:20.500/abc'
    assert str(dataset_ref(instrument='dream', run=4711)) == 'run:dream/4711'
    assert str(dataset_ref(path=Path('/data/x.nxs'))) == 'path:/data/x.nxs'
    assert dataset_path(dataset_ref(path=Path('/data/x.nxs'))) == Path('/data/x.nxs')
    assert dataset_path(dataset_ref(pid='20.500/abc')) is None
    with pytest.raises(ValueError, match='exactly one identity'):
        dataset_ref(pid='20.500/abc', path=Path('/data/x.nxs'))
    with pytest.raises(ValueError, match='exactly one identity'):
        dataset_ref()
    with pytest.raises(ValueError, match='together'):
        dataset_ref(instrument='dream')


def test_a_data_field_holds_either_form_of_reference() -> None:
    dataset = dataset_ref(instrument='dream', run=4711)
    assert Params(data=REF, runs=[dataset]).runs == [dataset]
    params = {'data': REF, 'runs': [dataset.model_dump()]}
    assert [(p, str(r)) for p, r in walk_refs(params)] == [
        ('data', 'r1.data'),
        ('runs[0]', 'run:dream/4711'),
    ]


class ReduceParams(BaseModel):
    run: NexusFile
    scale: float = 1.0


class ReduceOutputs(BaseModel):
    result: Array()
    numerator: Array()
    count: int = 0


def spec(**fields: object) -> WorkflowSpec:
    return WorkflowSpec(
        name='reduce',
        version=1,
        title='Reduce',
        description='A workflow that exposes an intermediate.',
        params=ReduceParams,
        outputs=ReduceOutputs,
        **fields,
    )


def test_the_results_are_the_outputs_that_are_not_intermediates() -> None:
    assert spec().results == ('result', 'numerator', 'count')
    assert spec(intermediates=('numerator',)).results == ('result', 'count')


def test_an_intermediate_must_be_an_output() -> None:
    with pytest.raises(ValidationError, match='are not outputs'):
        spec(intermediates=('run',))


def test_serialized_spec_carries_the_intermediates() -> None:
    serialized = SerializedWorkflowSpec.model_validate_json(
        spec(intermediates=('numerator',)).serialize().model_dump_json()
    )
    assert serialized.intermediates == ('numerator',)
    assert serialized.results == ('result', 'count')
    assert 'scale' in serialized.params_schema['properties']


def test_serialized_spec_id_matches_the_workflow_specs() -> None:
    assert spec().serialize().id == spec().id


def test_parse_ref_round_trips_a_dataset_and_an_output() -> None:
    dataset = dataset_ref(instrument='dream', run=4711)
    assert parse_ref(str(dataset)) == dataset
    ref = OutputRef(record='r1', output='data')
    assert parse_ref(str(ref)) == ref
    keyed = OutputRef(record='r1', output='banks', key='a')
    assert parse_ref(str(keyed)) == keyed


def test_parse_ref_rejects_text_that_is_neither() -> None:
    with pytest.raises(ValueError, match='not a reference'):
        parse_ref('not-a-ref-or-path-dict')

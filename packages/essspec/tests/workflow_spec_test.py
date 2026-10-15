# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path
from typing import Any, NewType

import pydantic
import pytest
import scipp as sc
from pydantic import BaseModel, Field, field_validator, model_validator

from ess.spec import NoInputs, NoParams, SerializedWorkflowSpec, WorkflowSpec


class Reduction(BaseModel):
    bins: int = 2


class SampleGeometry(BaseModel):
    thickness: float

    @field_validator('thickness')
    @classmethod
    def _positive(cls, thickness: float) -> float:
        if thickness <= 0:
            raise ValueError('thickness must be positive')
        return thickness


class TimeRange(BaseModel):
    start: float
    stop: float


SampleFile = NewType('SampleFile', Path)
CanFile = NewType('CanFile', Path)
BeamCentre = NewType('BeamCentre', sc.Variable)
IofQ = NewType('IofQ', sc.DataArray)
Banks = NewType('Banks', sc.DataArray)


class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample_geometry: SampleGeometry | None = None
    time_range: TimeRange | None = None


class IofQInputs(BaseModel, arbitrary_types_allowed=True):
    sample: SampleFile = Field(title='Sample run')
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None

    @model_validator(mode='after')
    def _can_needs_a_centre(self) -> 'IofQInputs':
        if self.can is not None and self.beam_centre is None:
            raise ValueError('a can needs a beam centre')
        return self


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ = Field(title='I(Q)', description='Scattering intensity.')
    banks: dict[str, Banks] | None = None


def spec(**fields: Any) -> WorkflowSpec[Any, Any, Any]:
    """A spec of the toy models, with ``fields`` replacing any of them."""
    defaults = {
        'name': 'sans-iofq',
        'version': 1,
        'title': 'I(Q)',
        'description': 'Reduces a sample run to I(Q).',
        'params': IofQParams,
        'inputs': IofQInputs,
        'outputs': IofQOutputs,
    }
    return WorkflowSpec(**{**defaults, **fields})


IOFQ = spec()
IOFQ_MULTI = IOFQ.tabulate(
    name='sans-iofq-multi',
    version=1,
    tables={'sample': ('sample_geometry',), 'can': ()},
)


def model(name: str, **fields: Any) -> type[BaseModel]:
    return pydantic.create_model(
        name, __config__=pydantic.ConfigDict(arbitrary_types_allowed=True), **fields
    )


def annotations(model: type[BaseModel]) -> dict[str, Any]:
    return {name: f.annotation for name, f in model.model_fields.items()}


class TestMetadata:
    def test_params_and_inputs_default_to_models_without_fields(self) -> None:
        minimal = WorkflowSpec(
            name='wf', version=1, title='W', description='D', outputs=IofQOutputs
        )
        assert minimal.params is NoParams
        assert minimal.inputs is NoInputs
        assert minimal.tables == {}
        assert minimal.code_revision is None

    def test_no_params_and_no_inputs_refuse_any_value(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            NoParams(anything=1)
        with pytest.raises(pydantic.ValidationError):
            NoInputs(anything=1)

    def test_outputs_are_required(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            WorkflowSpec(name='wf', version=1, title='W', description='D')

    @pytest.mark.parametrize(
        'fields', [{'name': ''}, {'title': ''}, {'description': ''}, {'version': 0}]
    )
    def test_metadata_is_checked(self, fields: dict[str, Any]) -> None:
        with pytest.raises(pydantic.ValidationError):
            spec(**fields)

    def test_spec_is_frozen(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            IOFQ.title = 'Other'

    def test_fields_by_name(self) -> None:
        assert IOFQ.side('reduction') == 'params'
        assert IOFQ.side('sample') == 'inputs'
        assert IOFQ.field('sample').title == 'Sample run'
        with pytest.raises(KeyError, match='iofq'):
            IOFQ.side('iofq')


class TestModelRules:
    def test_the_example_spec_is_accepted(self) -> None:
        assert IOFQ.params is IofQParams
        assert IOFQ.inputs is IofQInputs
        assert IOFQ.outputs is IofQOutputs

    @pytest.mark.parametrize(
        'fields',
        [
            {'params': model('P', reduction=(list[Reduction], []))},
            {'params': model('P', reduction=(list[Reduction | None], []))},
            {'inputs': model('I', runs=(list[SampleFile], []))},
            {'inputs': model('I', runs=(list[SampleFile | None], []))},
            {'inputs': model('I', parts=(BaseModel, ...))},
            {'outputs': model('O', iofq=(list[IofQ], ...))},
            {'outputs': model('O', iofq=(dict[str, IofQ | None], ...))},
            {'outputs': model('O', path=(Path | None, None))},
        ],
    )
    def test_accepted(self, fields: dict[str, Any]) -> None:
        spec(**fields)

    @pytest.mark.parametrize(
        ('fields', 'reason'),
        [
            (
                {'outputs': model('O', sample=(IofQ, ...))},
                'sample: a field of both IofQInputs and O',
            ),
            (
                {'inputs': model('I', sample=(SampleFile, ...), can=(SampleFile, ...))},
                'sample and can: both of type SampleFile; give each a NewType',
            ),
            (
                {'inputs': model('I', a=(SampleFile, ...), b=(list[SampleFile], []))},
                'a and b: both of type SampleFile',
            ),
            (
                {'outputs': model('O', centre=(BeamCentre | None, None))},
                'beam_centre and centre: both of type BeamCentre',
            ),
            (
                {'params': model('P', bins=(int, 2))},
                'P.bins: a parameter is a group',
            ),
            (
                {'params': model('P', reduction=(dict[str, Reduction], {}))},
                'P.reduction: a parameter is a group',
            ),
            (
                {'params': model('P', reduction=(list[Reduction] | None, None))},
                'P.reduction: a parameter is a group',
            ),
            (
                {'params': model('P', run=(model('Run', path=(Path, ...)), ...))},
                'P.run: a parameter holds no file',
            ),
            (
                {
                    'params': model(
                        'P', mask=(model('Mask', values=(sc.Variable, ...)), ...)
                    )
                },
                'P: every parameter has a JSON form',
            ),
            (
                {'inputs': model('I', runs=(dict[str, SampleFile], {}))},
                'I.runs: an input is a data value',
            ),
            (
                {'inputs': model('I', sample=(SampleFile | CanFile, ...))},
                'I.sample: an input is a data value',
            ),
            (
                {'inputs': model('I', runs=(list[list[SampleFile]], []))},
                'I.runs: an input is a data value',
            ),
            (
                {'inputs': model('I', runs=(list[SampleFile] | None, None))},
                'I.runs: an input is a data value',
            ),
            (
                {'inputs': model('I', runs=(list, []))},
                'I.runs: an input is a data value',
            ),
            (
                {'inputs': model('I', sample=(SampleFile, Path('run.nxs')))},
                'I.sample: the default of an input is None or an empty list',
            ),
            (
                {'inputs': model('I', runs=(list[SampleFile], [Path('run.nxs')]))},
                'I.runs: the default of an input is None or an empty list',
            ),
            (
                {'outputs': model('O', iofq=(IofQ | Banks, ...))},
                'O.iofq: an output is a data value',
            ),
            (
                {'outputs': model('O', iofq=(list[list[IofQ]], ...))},
                'O.iofq: an output is a data value',
            ),
        ],
    )
    def test_refused(self, fields: dict[str, Any], reason: str) -> None:
        with pytest.raises(ValueError, match=f'sans-iofq: .*{reason}'):
            spec(**fields)

    def test_every_problem_is_reported_at_once(self) -> None:
        params = model('P', bins=(int, 2))
        inputs = model('I', sample=(SampleFile, Path('run.nxs')))
        with pytest.raises(ValueError, match=r'P\.bins: .*; I\.sample: '):
            spec(params=params, inputs=inputs)


class TestTables:
    def test_a_spec_may_declare_tables_of_its_list_fields(self) -> None:
        stitch = spec(
            inputs=model('I', sample=(list[SampleFile], ...)),
            outputs=model('O', iofq=(IofQ, ...), runs=(list[Banks], ...)),
            tables=['sample'],
        )
        assert stitch.tables == {'sample': ()}
        assert stitch.columns('sample') == ('sample',)
        assert stitch.table_of('sample') == 'sample'

    def test_a_list_field_is_a_table_only_where_tables_names_it(self) -> None:
        listing = spec(inputs=model('I', sample=(list[SampleFile], ...)))
        assert listing.tables == {}
        assert listing.table_of('sample') is None

    @pytest.mark.parametrize(
        ('inputs', 'tables', 'reason'),
        [
            (
                {'sample': (SampleFile, ...)},
                ['sample'],
                'sample: a column of a table is a list',
            ),
            (
                {'sample': (list[SampleFile | None], ...)},
                ['sample'],
                'sample: the index of a table holds no None',
            ),
            (
                {'sample': (list[SampleFile], ...)},
                ['run'],
                "'run': not a parameter or an input",
            ),
            (
                {'sample': (list[SampleFile], ...), 'can': (list[CanFile], [])},
                {'sample': ('can',), 'can': ()},
                'can: a column of two tables',
            ),
        ],
    )
    def test_refused(self, inputs: dict[str, Any], tables: Any, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            spec(inputs=model('I', **inputs), tables=tables)


class TestTabulate:
    def test_names_the_tables_and_keeps_the_rest_of_the_metadata(self) -> None:
        assert IOFQ_MULTI.name == 'sans-iofq-multi'
        assert IOFQ_MULTI.title == IOFQ.title
        assert IOFQ_MULTI.description == IOFQ.description
        assert IOFQ_MULTI.tables == {'sample': ('sample_geometry',), 'can': ()}
        assert IOFQ_MULTI.columns('sample') == ('sample', 'sample_geometry')
        assert IOFQ_MULTI.table_of('sample_geometry') == 'sample'
        assert IOFQ_MULTI.table_of('beam_centre') is None

    def test_each_field_named_becomes_a_list_on_its_side(self) -> None:
        assert annotations(IOFQ_MULTI.params) == {
            'reduction': Reduction,
            'sample_geometry': list[SampleGeometry | None],
            'time_range': TimeRange | None,
        }
        assert annotations(IOFQ_MULTI.inputs) == {
            'sample': list[SampleFile],
            'can': list[CanFile],
            'beam_centre': BeamCentre | None,
        }

    def test_a_required_index_takes_at_least_one_row(self) -> None:
        with pytest.raises(pydantic.ValidationError, match='sample'):
            IOFQ_MULTI.inputs(sample=[])

    def test_an_optional_index_is_empty_by_default(self) -> None:
        inputs = IOFQ_MULTI.inputs(sample=[Path('run.nxs')])
        assert inputs.can == []
        assert inputs.beam_centre is None

    def test_an_attached_column_has_no_default(self) -> None:
        assert IOFQ_MULTI.params.model_fields['sample_geometry'].is_required()
        params = IOFQ_MULTI.params(sample_geometry=[None, {'thickness': 0.2}])
        assert params.sample_geometry == [None, SampleGeometry(thickness=0.2)]

    def test_the_field_validators_of_the_types_still_run(self) -> None:
        with pytest.raises(pydantic.ValidationError, match='thickness must be'):
            IOFQ_MULTI.params(sample_geometry=[{'thickness': -1.0}])

    def test_the_model_validators_are_not_carried_over(self) -> None:
        with pytest.raises(pydantic.ValidationError, match='a can needs a beam centre'):
            IofQInputs(sample=Path('a.nxs'), can=Path('b.nxs'))
        IOFQ_MULTI.inputs(sample=[Path('a.nxs')], can=[Path('b.nxs')])

    def test_a_side_without_a_field_named_keeps_its_model(self) -> None:
        runs = IOFQ.tabulate(name='runs', version=1, tables=('sample', 'can'))
        assert runs.params is IofQParams
        assert runs.outputs is IofQOutputs
        assert runs.tables == {'sample': (), 'can': ()}

    def test_an_index_may_be_a_parameter(self) -> None:
        slices = IOFQ.tabulate(name='slices', version=1, tables=('time_range',))
        assert slices.params.model_fields['time_range'].annotation == list[TimeRange]
        assert slices.inputs is IofQInputs

    def test_an_index_with_a_default_has_one_row_of_it_by_default(self) -> None:
        geometry = IOFQ.tabulate(name='geometries', version=1, tables=('reduction',))
        assert geometry.params.model_fields['reduction'].annotation == list[Reduction]
        assert geometry.params().reduction == [Reduction()]

    @pytest.mark.parametrize(
        ('tables', 'reason'),
        [
            (('run',), r"\['run'\]: not parameters or inputs of sans-iofq"),
            ({'sample': ('iofq',)}, r"\['iofq'\]: not parameters or inputs"),
        ],
    )
    def test_refuses_names_that_are_not_fields(self, tables: Any, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            IOFQ.tabulate(name='multi', version=1, tables=tables)

    def test_refuses_a_spec_with_tables(self) -> None:
        with pytest.raises(ValueError, match='sans-iofq-multi has tables already'):
            IOFQ_MULTI.tabulate(name='multi', version=1, tables=('beam_centre',))

    def test_refuses_a_list_field(self) -> None:
        listing = spec(inputs=model('I', sample=(list[SampleFile], ...)))
        with pytest.raises(ValueError, match=r"\['sample'\]: lists already"):
            listing.tabulate(name='multi', version=1, tables=('sample',))


class TestSerialization:
    def test_params_schema_is_the_json_schema_of_the_model(self) -> None:
        assert IOFQ.serialize().params_schema == IofQParams.model_json_schema()

    def test_inputs_and_outputs_name_the_type_of_each_value(self) -> None:
        serialized = IOFQ.serialize()
        assert serialized.inputs_schema == {
            'title': 'IofQInputs',
            'type': 'object',
            'properties': {
                'sample': {'dataType': 'SampleFile', 'title': 'Sample run'},
                'can': {
                    'anyOf': [{'dataType': 'CanFile'}, {'type': 'null'}],
                    'default': None,
                },
                'beam_centre': {
                    'anyOf': [{'dataType': 'BeamCentre'}, {'type': 'null'}],
                    'default': None,
                },
            },
            'required': ['sample'],
        }
        assert serialized.outputs_schema['properties'] == {
            'iofq': {
                'dataType': 'IofQ',
                'title': 'I(Q)',
                'description': 'Scattering intensity.',
            },
            'banks': {
                'anyOf': [
                    {
                        'type': 'object',
                        'additionalProperties': {'dataType': 'Banks'},
                    },
                    {'type': 'null'},
                ],
                'default': None,
            },
        }

    def test_tables_are_arrays_and_listed(self) -> None:
        serialized = IOFQ_MULTI.serialize()
        properties = serialized.inputs_schema['properties']
        assert properties['sample'] == {
            'type': 'array',
            'items': {'dataType': 'SampleFile'},
            'title': 'Sample run',
            'minItems': 1,
        }
        assert properties['can'] == {
            'type': 'array',
            'items': {'dataType': 'CanFile'},
            'default': [],
        }
        assert serialized.tables == {'sample': ('sample_geometry',), 'can': ()}
        assert serialized.params_schema['required'] == ['sample_geometry']

    def test_metadata_is_kept(self) -> None:
        serialized = spec(version=2, code_revision='abc123').serialize()
        assert (serialized.name, serialized.version) == ('sans-iofq', 2)
        assert serialized.title == 'I(Q)'
        assert serialized.code_revision == 'abc123'

    def test_roundtrips_through_json(self) -> None:
        serialized = IOFQ_MULTI.serialize()
        restored = SerializedWorkflowSpec.model_validate_json(
            serialized.model_dump_json()
        )
        assert restored == serialized

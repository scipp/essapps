# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, NewType, TypedDict

import pydantic
import pytest
import scipp as sc
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python

from ess.spec import (
    AccumulatorRef,
    DatasetRef,
    NoParams,
    OutputRef,
    SerializedWorkflowSpec,
    WorkflowSpec,
)


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
CentreArray = NewType('CentreArray', sc.Variable)
IofQ = NewType('IofQ', sc.DataArray)
Banks = NewType('Banks', sc.DataArray)


class Node(BaseModel):
    """A recursive group."""

    name: str
    children: list['Node'] = []


ReductionOfCan = NewType('ReductionOfCan', Reduction)


class Mode(Enum):
    fast = 'fast'
    slow = 'slow'


def validated(name: str, **fields: Any) -> type[BaseModel]:
    """A model with ``fields`` and a field validator of its first field."""

    def check(cls: type[BaseModel], value: Any) -> Any:
        return value

    validators = {'_check': field_validator(next(iter(fields)))(classmethod(check))}
    return pydantic.create_model(
        name,
        __config__=pydantic.ConfigDict(arbitrary_types_allowed=True),
        __validators__=validators,
        **fields,
    )


class SerializedParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

    @field_serializer('reduction')
    def _bins(self, reduction: Reduction) -> int:
        return reduction.bins


class ComputedParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bins(self) -> int:
        return self.reduction.bins


class IofQSettings(BaseModel, arbitrary_types_allowed=True):
    reduction: Reduction = Field(default_factory=Reduction)
    sample_geometry: SampleGeometry | None = None
    time_range: TimeRange | None = None
    beam_centre: CentreArray | None = None


class IofQParams(IofQSettings):
    sample: SampleFile = Field(title='Sample run')
    can: CanFile | None = None

    @model_validator(mode='after')
    def _can_needs_a_centre(self) -> 'IofQParams':
        if self.can is not None and self.beam_centre is None:
            raise ValueError('a can needs a beam centre')
        return self


class IofQMultiParams(IofQSettings):
    sample: list[SampleFile] = Field(min_length=1, title='Sample run')
    can: list[CanFile | None] = Field(default_factory=list)


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ = Field(title='I(Q)', description='Scattering intensity.')
    banks: dict[str, Banks] | None = None


class Loose(BaseModel, extra='allow'):
    """A group that takes any field, ``@ref`` among them."""


Kind = TypedDict('Kind', {'@ref': str})
"""A value whose JSON form holds the key ``@ref``."""


def spec(**fields: Any) -> WorkflowSpec[Any, Any]:
    """A spec of the toy models, with ``fields`` replacing any of them."""
    defaults = {
        'name': 'sans-iofq',
        'version': 1,
        'title': 'I(Q)',
        'description': 'Reduces a sample run to I(Q).',
        'params': IofQParams,
        'outputs': IofQOutputs,
    }
    return WorkflowSpec(**{**defaults, **fields})


IOFQ = spec()
IOFQ_MULTI = spec(
    name='sans-iofq-multi', params=IofQMultiParams, tables=('sample', 'can')
)


def model(name: str, **fields: Any) -> type[BaseModel]:
    return pydantic.create_model(
        name, __config__=pydantic.ConfigDict(arbitrary_types_allowed=True), **fields
    )


class SampleRun(BaseModel):
    """A sample run and the geometry given next to it: a group that holds a file."""

    file: SampleFile
    geometry: SampleGeometry | None = None


RUNS = spec(
    name='sans-runs',
    params=model('P', sample=(list[SampleRun], Field(min_length=1))),
    tables=('sample',),
)


class TestMetadata:
    def test_params_default_to_a_model_without_fields(self) -> None:
        minimal = WorkflowSpec(
            name='wf', version=1, title='W', description='D', outputs=IofQOutputs
        )
        assert minimal.params is NoParams
        assert minimal.tables == ()
        assert minimal.code_revision is None

    def test_no_params_refuse_any_value(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            NoParams(anything=1)

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

    def test_a_type_with_a_json_form_takes_an_inline_value(self) -> None:
        assert [n for n in IofQParams.model_fields if IOFQ.takes_inline(n)] == [
            'reduction',
            'sample_geometry',
            'time_range',
        ]
        assert not IOFQ_MULTI.takes_inline('sample')

    @pytest.mark.parametrize(
        'tp',
        [
            float,
            NewType('Offset', float),
            Mode,
            ReductionOfCan,
            list[float],
            tuple[Reduction, Reduction],
            Reduction | None,
        ],
    )
    def test_a_number_an_enum_or_a_tuple_takes_an_inline_value(self, tp: Any) -> None:
        assert spec(params=model('P', value=(tp, ...))).takes_inline('value')

    @pytest.mark.parametrize('tp', [CentreArray, SampleFile, list[CanFile | None]])
    def test_a_file_or_an_object_takes_no_inline_value(self, tp: Any) -> None:
        assert not spec(params=model('P', value=(tp, ...))).takes_inline('value')


class TestModelRules:
    def test_the_example_spec_is_accepted(self) -> None:
        assert IOFQ.params is IofQParams
        assert IOFQ.outputs is IofQOutputs

    @pytest.mark.parametrize(
        'fields',
        [
            {'params': model('P', reduction=(list[Reduction], []))},
            {'params': model('P', reduction=(list[Reduction | None], []))},
            {'params': model('P', reduction=(Reduction, Reduction(bins=3)))},
            {'params': model('P', runs=(list[SampleFile], []))},
            {'params': model('P', run=(model('Run', path=(Path, ...)), ...))},
            {
                'params': model(
                    'P', run=(list[model('Run', path=(Path | None, None))], [])
                )
            },
            {'params': model('P', runs=(list[SampleFile | None], []))},
            {'params': model('P', parts=(BaseModel, ...))},
            {'params': model('P', bins=(int, ...))},
            {'params': model('P', a=(SampleFile, ...), b=(list[SampleFile], []))},
            {'params': model('P', steps=(list[TimeRange], Field(min_length=2)))},
            {
                'params': model(
                    'P', steps=(list[TimeRange], Field([], min_length=0, max_length=3))
                )
            },
            {
                'params': model(
                    'P',
                    steps=(list[Annotated[TimeRange, AfterValidator(lambda r: r)]], []),
                )
            },
            {
                'params': model(
                    'P', mask=(model('Mask', values=(sc.Variable, ...)), ...)
                )
            },
            {'outputs': model('O', iofq=(list[IofQ], ...))},
            {'outputs': model('O', iofq=(dict[str, IofQ | None], ...))},
            {'outputs': model('O', path=(Path | None, None))},
            {'params': model('P', reduction=(ReductionOfCan, ...))},
            {'params': model('P', bins=(int, 2), mode=(Mode, Mode.fast))},
            {'params': model('P', tree=(Node, ...))},
            {'params': model('P', centre=(validated('Centre', x=(float, ...)), ...))},
        ],
    )
    def test_accepted(self, fields: dict[str, Any]) -> None:
        spec(**fields)

    def test_a_model_validator_of_the_params_model_is_allowed(self) -> None:
        # The framework never calls it (see the backend's tests); a binding may.
        assert IofQParams.__pydantic_decorators__.model_validators
        with pytest.raises(pydantic.ValidationError, match='a can needs a beam'):
            IofQParams.model_validate({'sample': 'a.nxs', 'can': 'b.nxs'})

    @pytest.mark.parametrize(
        ('fields', 'reason'),
        [
            (
                {'outputs': model('O', sample=(IofQ, ...))},
                'sample: a field of both IofQParams and O',
            ),
            (
                {'params': model('P', centre=(CentreArray, sc.scalar(1.0)))},
                'P.centre: the default of a type without a JSON form is None or '
                'an empty list',
            ),
            (
                {'params': model('P', reduction=(dict[str, Reduction], {}))},
                'P.reduction: a parameter is a type, possibly optional, or a list',
            ),
            (
                {'params': model('P', reduction=(list[Reduction] | None, None))},
                'P.reduction: a parameter is a type',
            ),
            (
                {
                    'params': model(
                        'P', runs=(model('Runs', paths=(list[Path], [])), ...)
                    )
                },
                'P.runs: Runs.paths: a field of a group holds a file only as a file',
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model('Run', geometry=(model('G', path=(Path, ...)), ...)),
                            ...,
                        ),
                    )
                },
                'P.run: Run.geometry: a field of a group holds a file only as a file',
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model(
                                'Run', path=(Path, ...), shifts=(list[CentreArray], [])
                            ),
                            ...,
                        ),
                    )
                },
                'P.run: Run.shifts: a field of a group that is a list or a dict has a '
                'JSON form',
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model(
                                'Run', path=(Path, ...), bins=(int, Field(1, alias='b'))
                            ),
                            ...,
                        ),
                    )
                },
                'P.run: Run.bins: a field of a group has no alias',
            ),
            (
                {'params': validated('P', reduction=(Reduction, ...))},
                r"P: no decorators but model validators, {'field_validators': "
                r"\['_check'\]}",
            ),
            (
                {'params': model('P', run=(validated('Run', path=(Path, ...)), ...))},
                r"P.run: Run: no decorators but model validators, "
                r"{'field_validators': \['_check'\]}",
            ),
            (
                {'params': SerializedParams},
                r"SerializedParams: no decorators but model validators, "
                r"{'field_serializers': \['_bins'\]}",
            ),
            (
                {'params': ComputedParams},
                r"ComputedParams: no decorators but model validators, "
                r"{'computed_fields': \['bins'\]}",
            ),
            (
                {'params': model('P', reduction=(Reduction, Field(alias='r')))},
                'P.reduction: a parameter has no alias',
            ),
            (
                {
                    'params': model(
                        'P',
                        reduction=(
                            Reduction,
                            Field(default_factory=lambda data: Reduction()),
                        ),
                    )
                },
                'P.reduction: the default_factory of a parameter takes no other',
            ),
            (
                {
                    'params': model(
                        'P',
                        steps=(Annotated[list[TimeRange], AfterValidator(len)], []),
                    )
                },
                'P.steps: a list parameter carries only length constraints',
            ),
            (
                {'params': model('P', steps=(list[TimeRange], Field([], strict=True)))},
                'P.steps: a list parameter carries only length constraints',
            ),
            (
                {
                    'params': model(
                        'P',
                        time_range=(TimeRange, TimeRange(start=float('nan'), stop=1)),
                    )
                },
                'P.time_range: the default has no JSON form that reads back',
            ),
            (
                {
                    'params': model(
                        'P', loose=(Loose, Loose(**{'@ref': 'dataset', 'dataset': 'a'}))
                    )
                },
                'P.loose: the default has no JSON form that reads back',
            ),
            (
                {'params': model('P', kind=(Kind, {'@ref': 'dataset'}))},
                'P.kind: the default has a JSON form that reads as a reference',
            ),
            (
                {'params': model('P', kind=(list[Kind], [{'@ref': 'output'}]))},
                'P.kind: the default has a JSON form that reads as a reference',
            ),
            (
                {'params': model('P', kinds=(tuple[Kind, Kind], ({'@ref': 'a'},) * 2))},
                'P.kinds: the default has a JSON form that reads as a reference',
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model('Run', path=(Path, ...), kind=(Kind, {'@ref': 'a'})),
                            ...,
                        ),
                    )
                },
                'P.run: Run.kind: the default has a JSON form that reads as a',
            ),
            (
                {'params': model('P', steps=(list[TimeRange], None))},
                'P.steps: the default of a list parameter is a list',
            ),
            (
                {'params': model('P', runs=(dict[str, SampleFile], {}))},
                'P.runs: a parameter is a type',
            ),
            (
                {'params': model('P', sample=(SampleFile | CanFile, ...))},
                'P.sample: a parameter is a type',
            ),
            (
                {'params': model('P', runs=(list[list[SampleFile]], []))},
                'P.runs: a parameter is a type',
            ),
            (
                {'params': model('P', runs=(list[SampleFile] | None, None))},
                'P.runs: a parameter is a type',
            ),
            (
                {'params': model('P', runs=(list, []))},
                'P.runs: a parameter is a type',
            ),
            (
                {'params': model('P', sample=(SampleFile, Path('run.nxs')))},
                'P.sample: the default of a type without a JSON form is None',
            ),
            (
                {'params': model('P', runs=(list[SampleFile], [Path('run.nxs')]))},
                'P.runs: the default of a type without a JSON form is None',
            ),
            (
                {'outputs': model('O', iofq=(IofQ | Banks, ...))},
                'O.iofq: an output is a type, possibly optional',
            ),
            (
                {'outputs': model('O', iofq=(list[list[IofQ]], ...))},
                'O.iofq: an output is a type, possibly optional',
            ),
        ],
    )
    def test_refused(self, fields: dict[str, Any], reason: str) -> None:
        with pytest.raises(ValueError, match=f'sans-iofq: .*{reason}'):
            spec(**fields)

    def test_every_problem_is_reported_at_once(self) -> None:
        params = model(
            'P', centre=(CentreArray, sc.scalar(1.0)), sample=(SampleFile, Path('a'))
        )
        with pytest.raises(ValueError, match=r'P\.centre: .*; P\.sample: '):
            spec(params=params)


class TestTables:
    def test_a_spec_may_declare_tables_of_its_list_fields(self) -> None:
        stitch = spec(
            params=model('P', sample=(list[SampleFile], Field(min_length=1))),
            outputs=model('O', iofq=(IofQ, ...), runs=(list[Banks], ...)),
            tables=['sample'],
        )
        assert stitch.tables == ('sample',)

    def test_a_list_field_is_a_table_only_where_tables_names_it(self) -> None:
        listing = spec(params=model('P', sample=(list[SampleFile], ...)))
        assert listing.tables == ()

    def test_a_table_is_any_list_parameter(self) -> None:
        params = model(
            'P',
            sample=(list[SampleFile | None], ...),
            reduction=(list[Reduction], [Reduction()]),
        )
        assert spec(params=params, tables=['sample', 'reduction']).tables == (
            'sample',
            'reduction',
        )

    @pytest.mark.parametrize(
        ('params', 'tables', 'reason'),
        [
            (
                {'sample': (SampleFile, ...)},
                ['sample'],
                'sample: a table is a list parameter',
            ),
            (
                {'sample': (list[SampleFile], Field(min_length=1))},
                ['run'],
                'run: not a parameter of sans-iofq',
            ),
            (
                {'sample': (list[SampleFile], Field(min_length=1))},
                ['sample', 'sample'],
                'sample: a table named twice',
            ),
        ],
    )
    def test_refused(self, params: dict[str, Any], tables: Any, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            spec(params=model('P', **params), tables=tables)

    def test_a_table_may_be_of_groups_given_field_by_field(self) -> None:
        assert RUNS.takes_inline('sample')
        params = RUNS.params(
            sample=[
                {'file': Path('a.nxs')},
                {'file': Path('b.nxs'), 'geometry': {'thickness': 0.2}},
            ]
        )
        assert [row.geometry for row in params.sample] == [
            None,
            SampleGeometry(thickness=0.2),
        ]

    def test_the_field_validators_of_a_group_check_each_row(self) -> None:
        with pytest.raises(pydantic.ValidationError, match='thickness must be'):
            RUNS.params(
                sample=[{'file': Path('a.nxs'), 'geometry': {'thickness': -1.0}}]
            )

    def test_the_validators_of_the_element_type_check_each_row(self) -> None:
        def known(path: Path) -> Path:
            if path.name != 'run.nxs':
                raise ValueError('unknown run')
            return path

        params = model(
            'P',
            sample=(list[Annotated[SampleFile, AfterValidator(known)]], ...),
            can=(list[Annotated[CanFile, AfterValidator(known)] | None], []),
        )
        runs = spec(params=params, tables=('sample', 'can'))
        runs.params(sample=[Path('run.nxs')], can=[Path('run.nxs')])
        for rows in ({'sample': [Path('other.nxs')]}, {'can': [Path('other.nxs')]}):
            with pytest.raises(pydantic.ValidationError, match='unknown run'):
                runs.params(**{'sample': [Path('run.nxs')], **rows})
        assert runs.serialize().params_schema['properties']['sample']['items'] == (
            file_ref('SampleFile')
        )


def file_ref(data_type: str) -> dict[str, Any]:
    """The schema of a reference to a file, a dataset."""
    return {'$ref': '#/$defs/DatasetRef', 'dataType': data_type}


def data_ref(data_type: str) -> dict[str, Any]:
    """The schema of a reference to data of any other type, an output."""
    kinds = ('OutputRef', 'AccumulatorRef')
    return {'anyOf': [{'$ref': f'#/$defs/{k}'} for k in kinds], 'dataType': data_type}


def inline(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema of an inline value: an object with the key ``@ref`` is not one."""
    return {**schema, 'not': {'required': ['@ref']}}


class ScaledParams(BaseModel):
    scale: float = Field(default=1.0, gt=0, title='Scale', description='A factor.')
    mode: Mode = Mode.fast


class TestSerialization:
    def test_a_group_is_its_json_schema_or_a_reference(self) -> None:
        schema = IOFQ.serialize().params_schema
        assert schema['title'] == 'IofQParams'
        assert list(schema['properties']) == list(IofQParams.model_fields)
        assert schema['properties']['reduction'] == {
            'anyOf': [inline({'$ref': '#/$defs/Reduction'}), data_ref('Reduction')],
            'default': {'bins': 2},
            'title': 'Reduction',
        }
        assert schema['$defs']['Reduction'] == {
            **Reduction.model_json_schema(),
            'additionalProperties': False,
        }
        assert schema['required'] == ['sample']
        assert schema['additionalProperties'] is False

    def test_a_reference_is_an_object_with_its_kind_under_at_ref(self) -> None:
        defs = IOFQ.serialize().params_schema['$defs']
        for kind in (OutputRef, DatasetRef, AccumulatorRef):
            assert defs[kind.__name__] == kind.model_json_schema()
            assert '@ref' in defs[kind.__name__]['required']

    def test_a_float_and_an_enum_are_their_json_schema_or_a_reference(self) -> None:
        schema = spec(params=ScaledParams).serialize().params_schema
        assert schema['properties'] == {
            'scale': {
                'anyOf': [
                    {'type': 'number', 'exclusiveMinimum': 0},
                    data_ref('float'),
                ],
                'default': 1.0,
                'title': 'Scale',
                'description': 'A factor.',
            },
            'mode': {
                'anyOf': [{'$ref': '#/$defs/Mode'}, data_ref('Mode')],
                'default': 'fast',
                'title': 'Mode',
            },
        }

    def test_a_file_or_a_scipp_type_is_a_reference_to_its_type(self) -> None:
        properties = IOFQ.serialize().params_schema['properties']
        assert {n: properties[n] for n in ('sample', 'can', 'beam_centre')} == {
            'sample': {**file_ref('SampleFile'), 'title': 'Sample run'},
            'can': {
                'anyOf': [file_ref('CanFile'), {'type': 'null'}],
                'default': None,
                'title': 'Can',
            },
            'beam_centre': {
                'anyOf': [data_ref('CentreArray'), {'type': 'null'}],
                'default': None,
                'title': 'Beam Centre',
            },
        }

    def test_a_list_parameter_has_the_forms_per_row(self) -> None:
        params = model(
            'P', time_range=(list[TimeRange], Field(min_length=2, title='Slices'))
        )
        properties = spec(params=params).serialize().params_schema['properties']
        assert properties['time_range'] == {
            'type': 'array',
            'items': {
                'anyOf': [inline({'$ref': '#/$defs/TimeRange'}), data_ref('TimeRange')]
            },
            'minItems': 2,
            'title': 'Slices',
        }

    def test_tables_are_arrays_and_listed(self) -> None:
        serialized = IOFQ_MULTI.serialize()
        properties = serialized.params_schema['properties']
        assert properties['sample'] == {
            'type': 'array',
            'items': file_ref('SampleFile'),
            'title': 'Sample run',
            'minItems': 1,
        }
        assert properties['can'] == {
            'type': 'array',
            'items': {'anyOf': [file_ref('CanFile'), {'type': 'null'}]},
            'default': [],
            'title': 'Can',
        }
        assert serialized.tables == ('sample', 'can')
        assert serialized.params_schema['required'] == ['sample']

    def test_outputs_name_the_type_of_each_value(self) -> None:
        assert IOFQ.serialize().outputs_schema['properties'] == {
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

    @pytest.mark.parametrize(
        'params',
        [
            {'sample': DatasetRef(dataset='run:loki/1')},
            {
                'sample': DatasetRef(dataset='run:loki/1'),
                'reduction': Reduction(bins=4),
                'beam_centre': OutputRef(record='r2', output='centre'),
            },
            {
                'sample': DatasetRef(dataset='run:loki/1'),
                'reduction': OutputRef(record='r1', output='reduction', key='0'),
                'time_range': AccumulatorRef(accumulator='a1', output='slices'),
            },
        ],
    )
    def test_the_schema_takes_a_request(self, params: dict[str, Any]) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        schema = IOFQ.serialize().params_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(to_jsonable_python(params), schema)

    @pytest.mark.parametrize(
        'params',
        [
            {'sample': 'run.nxs'},
            {'sample': DatasetRef(dataset='run:loki/1'), 'reduction': {'bins': 'x'}},
            {'sample': {'@ref': 'dataset'}},
            {'sample': {'dataset': 'run:loki/1'}},
            {
                'sample': DatasetRef(dataset='run:loki/1'),
                'beam_centre': DatasetRef(dataset='run:loki/2'),
            },
            {'sample': OutputRef(record='r1', output='file')},
            {'sample': DatasetRef(dataset='run:loki/1'), 'speed': 1.0},
            {
                'sample': DatasetRef(dataset='run:loki/1'),
                'reduction': {'bins': 4, 'extra': 1},
            },
            {'sample': DatasetRef(dataset='run:loki/1'), 'reduction': {'@ref': 'x'}},
        ],
    )
    def test_the_schema_refuses_a_malformed_request(
        self, params: dict[str, Any]
    ) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                to_jsonable_python(params), IOFQ.serialize().params_schema
            )

    def test_the_schema_of_tables_takes_rows_of_both_forms(self) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        schema = RUNS.serialize().params_schema
        file = DatasetRef(dataset='run:loki/1')
        rows = [
            file,
            {'file': file, 'geometry': SampleGeometry(thickness=0.1)},
            {'file': file, 'geometry': OutputRef(record='r1', output='geometry')},
        ]
        jsonschema.validate(to_jsonable_python({'sample': rows}), schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'sample': []}, schema)

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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import re
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path, PosixPath, PurePosixPath
from typing import Annotated, Any, Literal, NewType, Self, TypedDict

import pydantic
import pytest
import scipp as sc
from annotated_types import Ge, Gt, MaxLen, MinLen
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    Field,
    NaiveDatetime,
    RootModel,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python

from ess.spec import (
    AccumulatorRef,
    FileModel,
    FileRef,
    NoParams,
    OutputRef,
    Referenced,
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


class RunFile(FileModel, kind='raw'):
    pass


class MaskFile(FileModel, kind='mask'):
    pass


class GeometryFile(FileModel):
    """A file model without keywords: it takes raw files."""


class SampleRun(FileModel, kind='raw'):
    """A sample run's file."""


class CanRun(FileModel, kind='raw'):
    """An empty can run's file."""


class ByMonitor(BaseModel):
    mode: Literal['monitor']


class ByTime(BaseModel):
    mode: Literal['time']
    seconds: float


By = Annotated[ByMonitor | ByTime, Field(discriminator='mode')]


class ByWindow(BaseModel):
    mode: Literal['window'] = 'window'
    start: float = 0.0
    stop: float = 1.0


# A spec refuses a NewType and a type alias: it names a role by a class.
SampleFile = NewType('SampleFile', RunFile)
ReductionOfCan = NewType('ReductionOfCan', Reduction)
TimeOfCan = NewType('TimeOfCan', ByTime)
IofQ = NewType('IofQ', sc.DataArray)
Seconds = NewType('Seconds', float)
type RawFile = RunFile


class RoleRun(BaseModel):
    """A group with a field typed as a NewType."""

    file: SampleFile


@pydantic.dataclasses.dataclass
class Exposure:
    """A dataclass with a field typed as a NewType."""

    seconds: Seconds


class CalibrationFile(FileModel, kind='calibration'):
    """A file model with a field other than ``file``: refused."""

    run: int


class CheckedMaskFile(MaskFile):
    """A file model with a validator of its own: refused."""

    @field_validator('file')
    @classmethod
    def _xml(cls, file: str) -> str:
        if not file.endswith('.xml'):
            raise ValueError('a mask is an XML file')
        return file


class AttachingMaskFile(MaskFile):
    """A file model that declares a validator of FileModel anew: refused."""

    @model_validator(mode='after')
    def _with_path(self) -> Self:
        return self


class InitMaskFile(MaskFile):
    """A file model with a model_post_init of its own: refused."""

    def model_post_init(self, context: Any) -> None:
        pass


class BeamStopMaskFile(MaskFile):
    """A subclass of a subclass of FileModel that keeps its field ``file``."""


class LocalPath(Path):
    """A subclass of Path."""


class TimeRangeAt(TimeRange):
    """An output type that fits a TimeRange place and holds a Path."""

    where: Path


@pydantic.dataclasses.dataclass
class RunFiles:
    """A dataclass that holds a Path."""

    sample: Path


class RunPaths(TypedDict):
    """A TypedDict that holds a Path."""

    sample: Path


class AliasedA(BaseModel):
    mode: Literal['a'] = Field('a', alias='m')


class AliasedB(BaseModel):
    mode: Literal['b'] = Field('b', alias='m')


class Mode(Enum):
    fast = 'fast'
    slow = 'slow'


class Level(IntEnum):
    low = 1
    high = 2


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


class SerializedReduction(BaseModel):
    bins: int = 2

    @field_serializer('bins')
    def _bins(self, bins: int) -> str:
        return str(bins)


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


class BeamCentre(BaseModel, arbitrary_types_allowed=True):
    """A group that holds a data value: only a reference fills its place."""

    position: sc.Variable


class ByCentre(BaseModel, arbitrary_types_allowed=True):
    """An alternative that holds a data value: refused, a form has no input for it."""

    mode: Literal['centre'] = 'centre'
    centre: sc.Variable


class Centring(BaseModel):
    """
    A group with a field of a group, and of a tuple: data values, so only a
    reference fills its place.
    """

    centre: BeamCentre | None = None
    tolerance: tuple[float, float] = (0.0, 1.0)


class ByPeak(BaseModel):
    """An alternative with a field of a group with a data value."""

    mode: Literal['peak'] = 'peak'
    centre: BeamCentre


@pydantic.dataclasses.dataclass(
    config=pydantic.ConfigDict(arbitrary_types_allowed=True)
)
class CentrePeak:
    """A dataclass that holds a data value: a data value itself."""

    centre: sc.Variable


class IofQSettings(BaseModel):
    reduction: Reduction | Referenced[Reduction] = Field(default_factory=Reduction)
    time_range: TimeRange | Referenced[TimeRange] | None = None
    beam_centre: Referenced[BeamCentre] | None = None


class IofQParams(IofQSettings):
    sample: SampleRun = Field(title='Sample run')
    can: CanRun

    @model_validator(mode='after')
    def _sample_is_not_the_can(self) -> 'IofQParams':
        if self.sample.path == self.can.path:
            raise ValueError('the sample run is not the can run')
        return self


class IofQMultiParams(IofQSettings):
    sample: list[SampleRun] = Field(min_length=1, title='Sample run')
    can: list[CanRun] = Field(min_length=1)


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: sc.DataArray = Field(title='I(Q)', description='Scattering intensity.')
    banks: dict[str, sc.DataArray] | None = None


class Measured(BaseModel):
    """A group with a field typed a file model."""

    run: RunFile


class Loose(BaseModel, extra='allow'):
    """A group that takes any field, ``@ref`` among them."""


class Tagged(Enum):
    """An enum whose value is an object: not a scalar."""

    a = {'@ref': 'a'}  # noqa: RUF012


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


GEOMETRIES = spec(
    name='sans-geometries',
    params=model(
        'P',
        geometry=(
            list[SampleGeometry | Referenced[SampleGeometry]],
            Field(min_length=1),
        ),
    ),
    tables=('geometry',),
)
FILES = spec(
    name='sans-files',
    params=model(
        'P',
        mask=(MaskFile | None, None),
        geometry=(GeometryFile, ...),
        runs=(list[RunFile], Field(min_length=1)),
    ),
    tables=('runs',),
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

    def test_a_group_takes_an_inline_value_unless_it_holds_a_data_value(
        self,
    ) -> None:
        assert [n for n in IofQParams.model_fields if _takes_inline(IOFQ, n)] == [
            'reduction',
            'time_range',
        ]

    @pytest.mark.parametrize('tp', [float, Mode, list[float], datetime])
    def test_a_group_with_a_number_an_enum_or_a_list_field_takes_an_inline_value(
        self, tp: Any
    ) -> None:
        group = model('Group', value=(tp, ...))
        assert _takes_inline(spec(params=model('P', group=(group, ...))), 'group')

    @pytest.mark.parametrize(
        'tp',
        [By, By | None, list[By], Annotated[ByWindow | ByTime, By.__metadata__[0]]],
    )
    def test_a_union_of_groups_takes_an_inline_value(self, tp: Any) -> None:
        assert _takes_inline(spec(params=model('P', value=(tp, ...))), 'value')

    @pytest.mark.parametrize(
        'tp',
        [Referenced[BeamCentre], list[Referenced[BeamCentre]], Referenced[Centring]],
    )
    def test_a_place_marked_referenced_takes_no_inline_value(self, tp: Any) -> None:
        assert not _takes_inline(spec(params=model('P', value=(tp, ...))), 'value')

    @pytest.mark.parametrize(
        'tp',
        [
            Reduction | Referenced[Reduction],
            list[TimeRange | Referenced[TimeRange]],
            By | Referenced[BeamCentre],
        ],
    )
    def test_a_union_with_a_referenced_member_takes_an_inline_value(
        self, tp: Any
    ) -> None:
        assert _takes_inline(spec(params=model('P', value=(tp, ...))), 'value')


def _takes_inline(spec: WorkflowSpec[Any, Any], name: str) -> bool:
    """
    Whether the params schema offers an inline source for the parameter
    ``name``, or each row of it: a source that is neither a reference to an
    output nor a file, each an object with the key ``@ref``.
    """
    schema = spec.serialize().params_schema
    prop = schema['properties'][name]
    return _has_inline_source(prop.get('items', prop), schema.get('$defs', {}))


def _has_inline_source(schema: dict[str, Any], defs: dict[str, Any]) -> bool:
    if 'anyOf' in schema:  # X | None
        return any(_has_inline_source(s, defs) for s in schema['anyOf'])
    if schema.get('type') == 'null':
        return False
    if 'discriminator' in schema:  # a reference to an output, or a union
        return schema['discriminator']['propertyName'] != '@ref'
    if 'oneOf' in schema:  # the sources of a place
        return any(_has_inline_source(s, defs) for s in schema['oneOf'])
    (*_, name) = schema['$ref'].split('/')
    return '@ref' not in defs[name]['properties']


NOT_A_VALUE = 'the default is not a value a request may write'
NOT_A_PARAMETER = re.escape(
    'a parameter is a group, a union of groups, or a list of one, possibly typed '
    'X | None'
)
OPTIONAL_LIST = re.escape(
    'a list parameter is not typed list[X] | None; a list without rows is []'
)
OPTIONAL_ROW = re.escape(
    'the element of a list parameter is not typed X | None; each row is a value'
)
NOT_AN_OUTPUT = re.escape(
    'an output is a type, possibly typed X | None, or a list or a dict of one'
)
OUTSIDE_A_FILE_MODEL = re.escape(
    "a Path is outside a file model; a parameter or a field that takes a file is "
    "typed a file model, such as mask: MaskFile with class MaskFile(FileModel, "
    "kind='mask'), and given by reference"
)

KEEPS_THE_FILE = re.escape(
    'a file model keeps the field file of FileModel, without a default or an alias'
)
FILE_MODEL_OUTPUT = re.escape(
    'is a file model; no output holds one, so that every file comes from the '
    'file source, which checks it'
)


class TestModelRules:
    def test_the_example_spec_is_accepted(self) -> None:
        assert IOFQ.params is IofQParams
        assert IOFQ.outputs is IofQOutputs

    @pytest.mark.parametrize(
        'fields',
        [
            {'params': model('P', reduction=(list[Reduction], []))},
            {'params': model('P', reduction=(Reduction, Reduction(bins=3)))},
            {'params': model('P', runs=(list[SampleRun], []))},
            {'params': model('P', mask=(MaskFile | None, None))},
            {'params': model('P', parts=(BaseModel, ...))},
            {'params': model('P', a=(SampleRun, ...), b=(list[CanRun], []))},
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
                    'P',
                    mask=(Referenced[model('Mask', pixels=(sc.Variable, ...))], ...),
                )
            },
            {'outputs': model('O', iofq=(list[sc.DataArray], ...))},
            {'outputs': model('O', iofq=(dict[str, sc.DataArray | None], ...))},
            {
                'params': model(
                    'P',
                    settings=(
                        model('Settings', bins=(int, 2), mode=(Mode, Mode.fast)),
                        ...,
                    ),
                )
            },
            {'params': model('P', by=(By, ByMonitor(mode='monitor')))},
            {
                'params': model(
                    'P', by=(ByMonitor | ByTime, Field(discriminator='mode'))
                )
            },
            {'params': model('P', by=(By | None, None), each=(list[By], []))},
            {'params': model('P', centre=(validated('Centre', x=(float, ...)), ...))},
            {'params': model('P', mask=(MaskFile, ...))},
            {'params': model('P', mask=(BeamStopMaskFile | None, None))},
            {
                'params': model(
                    'P',
                    centre=(Referenced[BeamCentre] | None, None),
                    centres=(list[Referenced[BeamCentre]], []),
                )
            },
            {
                'params': model(
                    'P',
                    reduction=(Reduction | Referenced[Reduction], Reduction()),
                    window=(ByWindow | Referenced[Reduction] | None, ByWindow()),
                    by=(By | Referenced[BeamCentre], ByTime(mode='time', seconds=1)),
                    centre=(Referenced[BeamCentre] | Referenced[Centring], ...),
                    steps=(list[TimeRange | Referenced[TimeRange]], []),
                    found=(list[By | Referenced[BeamCentre]], []),
                )
            },
        ],
    )
    def test_accepted(self, fields: dict[str, Any]) -> None:
        spec(**fields)

    def test_a_model_validator_of_the_params_model_is_allowed(self) -> None:
        # The framework never calls it (see the backend's tests); a binding may.
        assert IofQParams.__pydantic_decorators__.model_validators
        with pytest.raises(pydantic.ValidationError, match='the sample run is not'):
            IofQParams.model_validate(
                {'sample': SampleRun.local('a.nxs'), 'can': CanRun.local('a.nxs')}
            )

    @pytest.mark.parametrize(
        ('fields', 'reason'),
        [
            (
                {'outputs': model('O', sample=(sc.DataArray, ...))},
                'sample: a field of both IofQParams and O',
            ),
            *(
                (
                    {'params': model('P', value=value)},
                    f'P.value: {name} is not a group; a parameter is a group, a '
                    'union of groups, or a list of one',
                )
                for value, name in (
                    ((int, ...), 'int'),
                    ((int | float, 1), 'int | float'),
                    ((Mode, Mode.fast), 'Mode'),
                    ((sc.Variable, sc.scalar(1.0)), 'Variable'),
                    ((sc.DataArray | None, None), 'DataArray'),
                    ((list[sc.Variable], []), 'Variable'),
                    ((CentrePeak, ...), 'CentrePeak'),
                )
            ),
            *(
                (
                    fields,
                    f'{where}: {name} is a NewType; a spec names a role by a class',
                )
                for fields, where, name in (
                    (
                        {'params': model('P', reduction=(ReductionOfCan, ...))},
                        'P.reduction',
                        'ReductionOfCan',
                    ),
                    (
                        {'params': model('P', runs=(list[SampleFile | None], []))},
                        'P.runs',
                        'SampleFile',
                    ),
                    (
                        {
                            'params': model(
                                'P', run=(model('Run', file=(SampleFile, ...)), ...)
                            )
                        },
                        'P.run: Run.file',
                        'SampleFile',
                    ),
                    (
                        {
                            'params': model(
                                'P',
                                mask=(model('Mask', pixels=(IofQ | None, None)), ...),
                            )
                        },
                        'P.mask: Mask.pixels',
                        'IofQ',
                    ),
                    (
                        {
                            'params': model(
                                'P',
                                both=(
                                    model('Both', a=(list[ReductionOfCan], [])),
                                    ...,
                                ),
                            )
                        },
                        'P.both: Both.a',
                        'ReductionOfCan',
                    ),
                    (
                        {
                            'params': model(
                                'P',
                                by=(
                                    Annotated[
                                        ByMonitor | TimeOfCan,
                                        Field(discriminator='mode'),
                                    ],
                                    ...,
                                ),
                            )
                        },
                        'P.by',
                        'TimeOfCan',
                    ),
                    (
                        {
                            'params': model(
                                'P',
                                run=(
                                    model(
                                        'Run',
                                        parts=(
                                            list[
                                                model('Inner', file=(SampleFile, ...))
                                            ],
                                            [],
                                        ),
                                    ),
                                    ...,
                                ),
                            )
                        },
                        'P.run: Inner.file',
                        'SampleFile',
                    ),
                    (
                        {
                            'params': model(
                                'P', run=(model('Run', exposure=(Exposure, ...)), ...)
                            )
                        },
                        'P.run: Exposure.seconds',
                        'Seconds',
                    ),
                    ({'outputs': model('O', iofq=(IofQ, ...))}, 'O.iofq', 'IofQ'),
                    (
                        {
                            'outputs': model(
                                'O', result=(model('Result', iofq=(IofQ, ...)), ...)
                            )
                        },
                        'O.result: Result.iofq',
                        'IofQ',
                    ),
                    (
                        {'outputs': model('O', banks=(dict[str, IofQ] | None, None))},
                        'O.banks',
                        'IofQ',
                    ),
                )
            ),
            (
                {'params': model('P', run=(model('Run', file=(RawFile, ...)), ...))},
                'P.run: Run.file: RawFile is a type alias; a spec names a role by a '
                'class',
            ),
            (
                {'params': model('P', a=(RoleRun, ...), b=(RoleRun, ...))},
                'P.a: RoleRun.file: SampleFile is a NewType.*; '
                'P.b: RoleRun.file: SampleFile is a NewType',
            ),
            *(
                (
                    {'params': model('P', centre=(tp, ...))},
                    re.escape(
                        'P.centre: BeamCentre.position, of Variable: a form has no '
                        'input for it; give the field a type that a form has an '
                        'input for, or mark the place Referenced[BeamCentre], which '
                        'a reference to an output fills'
                    ),
                )
                for tp in (BeamCentre, BeamCentre | None, list[BeamCentre])
            ),
            (
                {'params': model('P', centring=(Centring, ...))},
                re.escape(
                    'P.centring: Centring.centre, of BeamCentre | None: a form has '
                    'no input for it'
                ),
            ),
            (
                {
                    'params': model(
                        'P',
                        centre=(
                            Referenced[BeamCentre],
                            BeamCentre(position=sc.scalar(1.0)),
                        ),
                    )
                },
                f'P.centre: {NOT_A_VALUE}: centre: .* is not a reference; BeamCentre '
                'is given by reference to an output',
            ),
            (
                {
                    'params': model(
                        'P',
                        centring=(
                            Reduction | Referenced[Centring],
                            Centring(centre=BeamCentre(position=sc.scalar(1.0))),
                        ),
                    )
                },
                f'P.centring: {NOT_A_VALUE}: centring: cannot be stored as JSON',
            ),
            *(
                (
                    {'params': model('P', g=(group, ...))},
                    re.escape(
                        f'P.g: {where}: Referenced[...] is at a parameter or a row, '
                        'not at a field of a group: a group is validated whole when '
                        'the request is submitted, and an output may not exist then'
                    ),
                )
                for group, where in (
                    (model('G', mask=(Referenced[BeamCentre], ...)), 'G.mask'),
                    (
                        model('G', mask=(Referenced[BeamCentre] | None, None)),
                        'G.mask',
                    ),
                    (
                        model('G', masks=(list[Referenced[BeamCentre]], [])),
                        'G.masks',
                    ),
                    (
                        model(
                            'G',
                            inner=(
                                model('H', mask=(Referenced[BeamCentre], ...)),
                                ...,
                            ),
                        ),
                        'H.mask',
                    ),
                    (
                        Referenced[model('G', mask=(Referenced[BeamCentre], ...))],
                        'G.mask',
                    ),
                    (
                        Reduction
                        | Referenced[model('G', mask=(Referenced[BeamCentre], ...))],
                        'G.mask',
                    ),
                )
            ),
            *(
                (
                    {'params': model('P', c=(tp, None))},
                    re.escape(
                        f'P.c: Referenced[{name}]: {name} is not a group; a reference '
                        'fills a place with a group, so that a picker of records '
                        'lists the outputs of a class that names a role'
                    ),
                )
                for tp, name in (
                    (Referenced[BeamCentre | None], 'BeamCentre | None'),
                    (Referenced[BeamCentre | Centring] | None, 'BeamCentre | Centring'),
                    (Referenced[float] | None, 'float'),
                )
            ),
            (
                {'params': model('P', c=(Referenced[list[BeamCentre]], ...))},
                re.escape(
                    'P.c: a reference fills a row of a list parameter, '
                    'list[Referenced[X]], not the list'
                ),
            ),
            *(
                (
                    {'params': model('P', run=(tp, ...))},
                    f'P.run: RunFile {FILE_MODEL_OUTPUT}',
                )
                for tp in (Referenced[RunFile], list[Referenced[RunFile]])
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            Annotated[
                                ByMonitor | Referenced[ByTime],
                                Field(discriminator='mode'),
                            ]
                            | Referenced[BeamCentre],
                            ...,
                        ),
                    )
                },
                re.escape(
                    'P.by: ByMonitor | ByTime: Referenced[...] is at a parameter, a '
                    'row, or a member of a union there, not inside another type'
                ),
            ),
            (
                {
                    'params': model(
                        'P', by=(ByMonitor | ByTime | Referenced[BeamCentre], ...)
                    )
                },
                'P.by: a union of groups has a discriminator',
            ),
            (
                {'params': model('P', reduction=(dict[str, Reduction], {}))},
                f'P.reduction: {NOT_A_PARAMETER}',
            ),
            (
                {'params': model('P', reduction=(list[Reduction] | None, None))},
                f'P.reduction: {OPTIONAL_LIST}',
            ),
            (
                {'params': model('P', reduction=(list[Reduction | None], []))},
                f'P.reduction: {OPTIONAL_ROW}',
            ),
            (
                {'params': model('P', by=(list[ByMonitor | ByTime], []))},
                'P.by: a union of groups has a discriminator',
            ),
            (
                {'params': model('P', by=(ByMonitor | ByTime, ...))},
                'P.by: a union of groups has a discriminator',
            ),
            (
                {'params': model('P', by=(ByMonitor | float, ...))},
                'P.by: a union holds groups only, or no group; a value of another '
                'type is a field of a group, such as of an alternative of the union',
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            Annotated[ByTime | ByCentre, Field(discriminator='mode')],
                            ...,
                        ),
                    )
                },
                re.escape(
                    'P.by: ByCentre.centre, of Variable: a form has no input for it; '
                    'give the field a type that a form has an input for, or take '
                    'ByCentre out of the union written inline, Annotated[A | B, '
                    'Field(discriminator=...)] | Referenced[ByCentre], which a '
                    'reference to an output fills'
                ),
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            list[
                                Annotated[
                                    ByCentre | ByTime, Field(discriminator='mode')
                                ]
                            ],
                            [],
                        ),
                    )
                },
                'P.by: ByCentre.centre, of Variable: a form has no input for it',
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            Annotated[
                                Annotated[ByMonitor, pydantic.Tag('m')]
                                | Annotated[ByTime, pydantic.Tag('t')],
                                pydantic.Discriminator(lambda v: 'm'),
                            ],
                            ...,
                        ),
                    )
                },
                'P.by: a union of groups has a discriminator, the name of its tag',
            ),
            (
                {
                    'params': model(
                        'P', by=(AliasedA | AliasedB, Field(discriminator='mode'))
                    )
                },
                'P.by: AliasedA.mode: a tag has no alias',
            ),
            (
                {'params': model('P', by=(dict[str, By], {}))},
                f'P.by: {NOT_A_PARAMETER}',
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            Annotated[ByTime | ByPeak, Field(discriminator='mode')],
                            ...,
                        ),
                    )
                },
                'P.by: ByPeak.centre, of BeamCentre: a form has no input for it',
            ),
            *(
                (
                    {'params': model('P', **fields)},
                    f'{where}: {OUTSIDE_A_FILE_MODEL}',
                )
                for fields, where in (
                    ({'path': (Path, ...)}, 'P.path'),
                    ({'paths': (list[Path], [])}, 'P.paths'),
                    ({'path': (Path | None, None)}, 'P.path'),
                    ({'path': (PurePosixPath, ...)}, 'P.path'),
                    ({'path': (PosixPath, ...)}, 'P.path'),
                    ({'path': (LocalPath, ...)}, 'P.path'),
                    ({'pair': (tuple[Path, Path], ...)}, 'P.pair'),
                    ({'runs': (RunFiles, ...)}, 'P.runs: RunFiles.sample'),
                    (
                        {'runs': (RootModel[list[Path]], ...)},
                        r'P.runs: RootModel\[.*\]\.root',
                    ),
                    (
                        {'run': (model('Run', path=(Path, ...)), ...)},
                        'P.run: Run.path',
                    ),
                    (
                        {'run': (model('Run', path=(Path | None, None)), ...)},
                        'P.run: Run.path',
                    ),
                    *(
                        (
                            {'run': (model('Run', path=(tp, ...)), ...)},
                            'P.run: Run.path',
                        )
                        for tp in (PurePosixPath, PosixPath, LocalPath)
                    ),
                    (
                        {'run': (model('Run', paths=(list[Path], [])), ...)},
                        'P.run: Run.paths',
                    ),
                    (
                        {'run': (model('Run', files=(RunPaths, ...)), ...)},
                        'P.run: RunPaths.sample',
                    ),
                    (
                        {'run': (model('Run', file=(tuple[Path, float], ...)), ...)},
                        'P.run: Run.file',
                    ),
                    (
                        {'run': (model('Run', files=(dict[str, Path], {})), ...)},
                        'P.run: Run.files',
                    ),
                    (
                        {
                            'run': (
                                model(
                                    'Run', geometry=(model('G', path=(Path, ...)), ...)
                                ),
                                ...,
                            )
                        },
                        'P.run: G.path',
                    ),
                )
            ),
            (
                {'params': model('P', calibration=(CalibrationFile, ...))},
                r"P.calibration: CalibrationFile: a file model holds only the "
                r"field file, not \['run'\]",
            ),
            *(
                (
                    {'params': model('P', mask=(mask, ...))},
                    rf'P.mask: {mask.__name__}.file: {KEEPS_THE_FILE}',
                )
                for mask in (
                    pydantic.create_model(
                        'IntMask', __base__=MaskFile, file=(int, ...)
                    ),
                    pydantic.create_model(
                        'OptionalMask', __base__=MaskFile, file=(str | None, None)
                    ),
                    pydantic.create_model(
                        'DefaultMask', __base__=MaskFile, file=(str, 'run:loki/1')
                    ),
                    pydantic.create_model(
                        'AliasedMask',
                        __base__=MaskFile,
                        file=(str, Field(alias='path')),
                    ),
                    pydantic.create_model(
                        'AnnotatedMask',
                        __base__=MaskFile,
                        file=(Annotated[str, AfterValidator(lambda p: p)], ...),
                    ),
                )
            ),
            (
                {
                    'params': model(
                        'P',
                        mask=(
                            pydantic.create_model(
                                'LabelledMask', __base__=MaskFile, label=(str, ...)
                            ),
                            ...,
                        ),
                    )
                },
                r"P.mask: LabelledMask: a file model holds only the field file, not "
                r"\['label'\]",
            ),
            (
                {'params': model('P', mask=(CheckedMaskFile, ...))},
                r"P.mask: CheckedMaskFile: a file model has no validators or "
                r"serializers of its own",
            ),
            (
                {'params': model('P', mask=(AttachingMaskFile, ...))},
                r"P.mask: AttachingMaskFile: a file model has no validators or "
                r"serializers of its own, \{'model_validators': \['_with_path'\]\}",
            ),
            (
                {'params': model('P', mask=(InitMaskFile, ...))},
                r"P.mask: InitMaskFile: a file model keeps the model_post_init, "
                r"__eq__, and __hash__ of FileModel, not its own "
                r"\['model_post_init'\]",
            ),
            (
                {
                    'params': model(
                        'P', mask=(type('Mutable', (MaskFile,), {}, frozen=False), ...)
                    )
                },
                'P.mask: Mutable: a file model keeps the config of FileModel',
            ),
            *(
                (
                    {'params': model('P', g=(Referenced[group], ...))},
                    f'P.g: {where} {FILE_MODEL_OUTPUT}',
                )
                for group, where in (
                    (
                        model('Held', data=(sc.Variable, ...), file=(RunFile, ...)),
                        'Held.file: RunFile',
                    ),
                    (
                        model(
                            'Held',
                            data=(sc.Variable, ...),
                            files=(list[RunFile], []),
                        ),
                        'Held.files: RunFile',
                    ),
                    (
                        model(
                            'Held',
                            data=(sc.Variable, ...),
                            inner=(model('Inner', file=(RunFile, ...)), ...),
                        ),
                        'Inner.file: RunFile',
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where} {FILE_MODEL_OUTPUT}',
                )
                for fields, where in (
                    ({'raw': (RunFile, ...)}, 'O.raw: RunFile'),
                    ({'raws': (list[RunFile | None], [])}, 'O.raws: RunFile'),
                    (
                        {'g': (model('G', file=(RunFile, ...)), ...)},
                        'O.g: G.file: RunFile',
                    ),
                    (
                        {
                            'g': (
                                model('G', h=(model('H', run=(SampleRun, ...)), ...)),
                                ...,
                            )
                        },
                        'O.g: H.run: SampleRun',
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where}: {OUTSIDE_A_FILE_MODEL}',
                )
                for fields, where in (
                    ({'path': (Path, ...)}, 'O.path'),
                    ({'path': (Path | None, None)}, 'O.path'),
                    ({'paths': (list[PurePosixPath], [])}, 'O.paths'),
                    ({'g': (model('G', path=(Path, ...)), ...)}, 'O.g: G.path'),
                    ({'range': (TimeRangeAt, ...)}, 'O.range: TimeRangeAt.where'),
                )
            ),
            *(
                (
                    {
                        'params': model(
                            'P',
                            by=(
                                Annotated[
                                    model('A', mode=(tag, ...)) | ByTime,
                                    Field(discriminator='mode'),
                                ],
                                ...,
                            ),
                        )
                    },
                    'P.by: A.mode: the tag of a union is a Literal of one string',
                )
                for tag in (Literal['a', 'b'], Literal[Mode.fast], Literal[1])
            ),
            (
                {'params': model('P', reduction=(SerializedReduction, ...))},
                r"P.reduction: SerializedReduction: no serializers or computed "
                r"fields, {'field_serializers': \['_bins'\]}",
            ),
            (
                {
                    'params': model(
                        'P', run=(model('Run', bins=(int, Field(1, alias='b'))), ...)
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
                {'params': model('P', reduction=(Reduction, {'bins': 3}))},
                r"P.reduction: the default {'bins': 3} is not the value validating "
                r'it gives, Reduction\(bins=3\)',
            ),
            (
                {'params': model('P', by=(By, {'mode': 'monitor'}))},
                r"P.by: the default {'mode': 'monitor'} is not the value validating",
            ),
            (
                {
                    'params': model(
                        'P',
                        r=(model('R', mode=(Mode, 'fast')), ...),
                    )
                },
                r"P.r: R.mode: the default 'fast' is not the value validating it "
                'gives, <Mode.fast',
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
                        r=(
                            model(
                                'R',
                                bins=(int, 1),
                                twice=(
                                    int,
                                    Field(default_factory=lambda d: 2 * d['bins']),
                                ),
                            ),
                            ...,
                        ),
                    )
                },
                'P.r: R.twice: the default_factory of a field of a group takes no',
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
                f'P.time_range: {NOT_A_VALUE}: time_range.start: has no JSON form '
                'that reads back',
            ),
            (
                {
                    'params': model(
                        'P', loose=(Loose, Loose(**{'@ref': 'file', 'file': 'a'}))
                    )
                },
                f'P.loose: {NOT_A_VALUE}: loose: file a does not fit Loose, which '
                'is not a file model',
            ),
            (
                {
                    'params': model(
                        'P',
                        r=(
                            model('R', x=(float, {'@ref': 'output', 'output': 'x'})),
                            ...,
                        ),
                    )
                },
                f'P.r: R.x: {NOT_A_VALUE}: x: Input should be a valid number',
            ),
            *(
                (
                    {'params': model('P', r=(model('R', banks=(tp, default)), ...))},
                    rf'P.r: R.banks: {NOT_A_VALUE}: banks: List should have at '
                    f'{need}',
                )
                for tp, default, need in (
                    (Annotated[list[int], MinLen(1)], [], 'least 1 item'),
                    (Annotated[list[int], MaxLen(1)], [1, 2], 'most 1 item'),
                    (list[int], Field([], min_length=1), 'least 1 item'),
                )
            ),
            (
                {'params': model('P', steps=(list[TimeRange], None))},
                f'P.steps: {NOT_A_VALUE}: steps: None is not a list',
            ),
            (
                {'params': model('P', runs=(list[list[SampleRun]], []))},
                f'P.runs: {NOT_A_PARAMETER}',
            ),
            (
                {'params': model('P', runs=(list[SampleRun] | None, None))},
                f'P.runs: {OPTIONAL_LIST}',
            ),
            (
                {'params': model('P', runs=(list, []))},
                f'P.runs: {NOT_A_PARAMETER}',
            ),
            (
                {'params': model('P', sample=(SampleRun, SampleRun.local('run.nxs')))},
                f'P.sample: {NOT_A_VALUE}: sample: file path:run.nxs: a default '
                'cannot name a file',
            ),
            (
                {
                    'params': model(
                        'P', runs=(list[SampleRun], [SampleRun.local('run.nxs')])
                    )
                },
                rf'P.runs: {NOT_A_VALUE}: runs\[0\]: file path:run.nxs: a default '
                'cannot name a file',
            ),
            (
                {
                    'params': model(
                        'P', measured=(Measured, Measured(run=RunFile(file='run-1')))
                    )
                },
                f'P.measured: {NOT_A_VALUE}: measured.run: file run-1: a default '
                'cannot name a file',
            ),
            (
                {
                    'params': model(
                        'P',
                        reduction=(
                            Reduction | Referenced[Reduction],
                            OutputRef(record='r1', output='reduction'),
                        ),
                    )
                },
                f'P.reduction: {NOT_A_VALUE}: reduction: r1.reduction is a '
                'reference; a default is a value',
            ),
            (
                {'outputs': model('O', iofq=(sc.DataArray | sc.Variable, ...))},
                f'O.iofq: {NOT_AN_OUTPUT}',
            ),
            (
                {'outputs': model('O', iofq=(list[list[sc.DataArray]], ...))},
                f'O.iofq: {NOT_AN_OUTPUT}',
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    re.escape(
                        f'{where}: Referenced[...] marks a place of the params, not an '
                        'output'
                    ),
                )
                for fields, where in (
                    ({'mask': (Referenced[BeamCentre], ...)}, 'O.mask'),
                    ({'masks': (list[Referenced[BeamCentre]], ...)}, 'O.masks'),
                    (
                        {
                            'g': (
                                model('G', mask=(Referenced[BeamCentre], ...)),
                                ...,
                            )
                        },
                        'O.g',
                    ),
                )
            ),
        ],
    )
    def test_refused(self, fields: dict[str, Any], reason: str) -> None:
        with pytest.raises(ValueError, match=f'sans-iofq: .*{reason}'):
            spec(**fields)

    def test_every_problem_is_reported_at_once(self) -> None:
        params = model(
            'P',
            centre=(sc.Variable, sc.scalar(1.0)),
            sample=(SampleRun, SampleRun.local('a')),
        )
        with pytest.raises(ValueError, match=r'P\.centre: .*; P\.sample: '):
            spec(params=params)


def _group_with(tp: Any) -> type[BaseModel]:
    """A group with the field ``value`` of type ``tp``, and a tag."""
    return model('G', mode=(Literal['g'], 'g'), value=(tp, ...))


class Limits(TypedDict):
    start: float
    stop: float


@pydantic.dataclasses.dataclass
class Pair:
    start: float
    stop: float


class TestFields:
    @pytest.mark.parametrize(
        'tp',
        [
            int,
            float,
            str,
            bool,
            datetime,
            Mode,
            Level,
            Literal['a', 'b'],
            Literal[1, 2.5, True],
            Annotated[float, Gt(0)],
            Annotated[str, MinLen(1)],
            list[float],
            list[Annotated[int, Ge(0)]],
            list[Mode],
            list[Literal['a', 'b']],
            list[datetime],
            AwareDatetime,
            NaiveDatetime,
            list[AwareDatetime],
            SampleRun,
            SampleRun | None,
            list[SampleRun],
        ],
    )
    def test_a_field_of_a_group_is_a_scalar_a_file_model_or_a_list_of_one(
        self, tp: Any
    ) -> None:
        params = model('P', g=(_group_with(tp), ...))
        assert _takes_inline(spec(params=params), 'g')

    @pytest.mark.parametrize(
        ('tp', 'default'),
        [
            (SampleRun | None, SampleRun.local('run.nxs')),
            (list[SampleRun], [SampleRun.local('run.nxs')]),
        ],
    )
    def test_a_file_model_at_a_field_has_no_default_file(
        self, tp: Any, default: Any
    ) -> None:
        group = model('G', value=(tp, default))
        with pytest.raises(
            ValueError,
            match=r'P\.g: G\.value: a file model at a field has no default but None',
        ):
            spec(params=model('P', g=(group, ...)))

    @pytest.mark.parametrize(
        'tp',
        [
            sc.DataArray,
            list[sc.DataArray],
            sc.Variable | sc.DataArray,
            sc.DataArray | None,
            BeamCentre,
            int | None,
            Mode | None,
            Literal['a', 'b'] | None,
            Literal['a', None],  # noqa: PYI061
            list[Literal['a', None]],  # noqa: PYI061
            list[float] | None,
            list[float | None],
            list[list[float]],
            Reduction,
            Reduction | None,
            list[Reduction],
            By,
            ByMonitor | ByTime,
            Tagged,
            list[Tagged],
            Literal[Tagged.a],
            Literal[Mode.fast],
            tuple[float, float],
            dict[str, float],
            set[float],
            Limits,
            Pair,
            RootModel[list[float]],
            int | float,
            Any,
        ],
    )
    def test_any_other_field_is_a_data_value(self, tp: Any) -> None:
        params = model('P', g=(_group_with(tp), ...))
        with pytest.raises(
            ValueError, match=r'P\.g: G\.value, of .*: a form has no input'
        ):
            spec(params=params)
        marked = model('P', g=(Referenced[_group_with(tp)], ...))
        assert not _takes_inline(spec(params=marked), 'g')

    @pytest.mark.parametrize(
        ('tp', 'value', 'shown'),
        [
            (float | None, None, 'float | None'),
            (tuple[float, float], (0.0, 1.0), 'tuple[float, float]'),
            (TimeRange, TimeRange(start=0.0, stop=1.0), 'TimeRange'),
            (Tagged, Tagged.a, 'Tagged'),
        ],
    )
    def test_a_place_that_only_a_reference_fills_has_no_default_but_none(
        self, tp: Any, value: Any, shown: str
    ) -> None:
        group = model('G', value=(tp, ...))
        with pytest.raises(
            ValueError,
            match=re.escape(
                f'P.g: G.value, of {shown}: a form has no input for it; give the '
                'field a type that a form has an input for, or mark the place '
                'Referenced[G], which a reference to an output fills'
            ),
        ):
            spec(params=model('P', g=(group, group(value=value))))
        with pytest.raises(
            ValueError,
            match=rf'P\.g: {NOT_A_VALUE}: g: .* is not a reference; G is given by '
            'reference to an output',
        ):
            spec(params=model('P', g=(Referenced[group], group(value=value))))
        spec(params=model('P', g=(Referenced[group] | None, None)))

    @pytest.mark.parametrize(
        'params',
        [
            lambda g: model('P', g=(Referenced[g], ...)),
            lambda g: model('P', g=(Referenced[g] | None, None)),
            lambda g: model('P', g=(list[Referenced[g]], [])),
        ],
    )
    def test_a_group_with_a_data_value_is_referenced_at_each_place(
        self, params: Any
    ) -> None:
        group = _group_with(tuple[float, float])
        assert not _takes_inline(spec(params=params(group)), 'g')

    def test_a_referenced_group_holds_any_type_an_output_may(self) -> None:
        group = model(
            'G',
            data=(sc.DataArray, ...),
            limits=(tuple[float, float], (0.0, 1.0)),
            reduction=(Reduction | None, None),
            any=(int | str, 1),
        )
        spec(params=model('P', g=(Referenced[group], ...)))


class TestTables:
    def test_a_spec_may_declare_tables_of_its_list_fields(self) -> None:
        stitch = spec(
            params=model('P', sample=(list[SampleRun], Field(min_length=1))),
            outputs=model(
                'O', iofq=(sc.DataArray, ...), runs=(list[sc.DataArray], ...)
            ),
            tables=['sample'],
        )
        assert stitch.tables == ('sample',)

    def test_a_list_field_is_a_table_only_where_tables_names_it(self) -> None:
        listing = spec(params=model('P', sample=(list[SampleRun], ...)))
        assert listing.tables == ()

    def test_a_table_is_any_list_parameter(self) -> None:
        params = model(
            'P', sample=(list[SampleRun], ...), reduction=(list[Reduction], ...)
        )
        assert spec(params=params, tables=['sample', 'reduction']).tables == (
            'sample',
            'reduction',
        )

    @pytest.mark.parametrize(
        ('params', 'tables', 'reason'),
        [
            (
                {'sample': (SampleRun, ...)},
                ['sample'],
                'sample: a table is a list parameter',
            ),
            (
                {'sample': (list[RunFile], Field(min_length=1))},
                ['run'],
                'run: not a parameter of sans-iofq',
            ),
            (
                {'sample': (list[RunFile], Field(min_length=1))},
                ['sample', 'sample'],
                'sample: a table named twice',
            ),
            (
                {'sample': (list[RunFile], Field(default_factory=list))},
                ['sample'],
                'sample: a table has no default',
            ),
        ],
    )
    def test_refused(self, params: dict[str, Any], tables: Any, reason: str) -> None:
        with pytest.raises(ValueError, match=reason):
            spec(params=model('P', **params), tables=tables)

    def test_a_table_may_be_of_file_models(self) -> None:
        assert FILES.tables == ('runs',)
        assert not _takes_inline(FILES, 'runs')

    def test_the_field_validators_of_a_group_check_each_row(self) -> None:
        assert _takes_inline(GEOMETRIES, 'geometry')
        with pytest.raises(pydantic.ValidationError, match='thickness must be'):
            GEOMETRIES.params(geometry=[{'thickness': -1.0}])

    def test_the_validators_of_the_element_type_check_each_row(self) -> None:
        def known[R: SampleRun | CanRun](run: R) -> R:
            if run.path.name != 'run.nxs':
                raise ValueError('unknown run')
            return run

        params = model(
            'P',
            sample=(list[Annotated[SampleRun, AfterValidator(known)]], ...),
            can=(list[Annotated[CanRun, AfterValidator(known)]], ...),
        )
        runs = spec(params=params, tables=('sample', 'can'))
        known_rows = {
            'sample': [SampleRun.local('run.nxs')],
            'can': [CanRun.local('run.nxs')],
        }
        runs.params(**known_rows)
        for rows in (
            {'sample': [SampleRun.local('other.nxs')]},
            {'can': [CanRun.local('other.nxs')]},
        ):
            with pytest.raises(pydantic.ValidationError, match='unknown run'):
                runs.params(**{**known_rows, **rows})
        assert runs.serialize().params_schema['properties']['sample']['items'] == (
            file_ref('SampleRun')
        )


def file_ref(name: str) -> dict[str, Any]:
    """
    The schema of a file, of the file model ``name``, whose definition holds
    its metadata (see :func:`file_model_def`).
    """
    return {'$ref': f'#/$defs/{name}'}


def file_model_def(name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """The definition of the file model ``name``: a reference to a file."""
    schema = FileRef.model_json_schema()
    del schema['description']  # a file model's own docstring, if any, describes it
    return {
        **schema,
        'title': name,
        'dataType': name,
        'metadata': metadata,
    }


def data_ref(data_type: str) -> dict[str, Any]:
    """The schema of a reference to a group: an output."""
    kinds = {'output': 'OutputRef', 'accumulator': 'AccumulatorRef'}
    return {
        'oneOf': [{'$ref': f'#/$defs/{k}'} for k in kinds.values()],
        'discriminator': {
            'propertyName': '@ref',
            'mapping': {tag: f'#/$defs/{k}' for tag, k in kinds.items()},
        },
        'dataType': data_type,
    }


SAMPLE = FileRef(file='run:loki/1')
CAN = {'can': FileRef(file='run:loki/3')}
"""The can that each request of ``IOFQ`` gives, left out of the cases below."""


class Scaling(BaseModel):
    scale: float = Field(default=1.0, gt=0, title='Scale', description='A factor.')
    mode: Mode = Mode.fast


class TestSerialization:
    def test_an_unmarked_group_is_its_fields(self) -> None:
        params = model('P', reduction=(Reduction, Reduction()))
        schema = spec(params=params).serialize().params_schema
        assert schema['properties']['reduction'] == {
            '$ref': '#/$defs/Reduction',
            'default': {'bins': 2},
            'title': 'Reduction',
        }

    def test_a_group_or_a_reference_is_its_fields_or_a_reference(self) -> None:
        schema = IOFQ.serialize().params_schema
        assert schema['title'] == 'IofQParams'
        assert list(schema['properties']) == list(IofQParams.model_fields)
        assert schema['properties']['reduction'] == {
            'oneOf': [{'$ref': '#/$defs/Reduction'}, data_ref('Reduction')],
            'default': {'bins': 2},
            'title': 'Reduction',
        }
        assert schema['$defs']['Reduction'] == {
            'title': 'Reduction',
            'type': 'object',
            'properties': {'bins': {'type': 'integer', 'default': 2, 'title': 'Bins'}},
            'additionalProperties': False,
        }
        assert schema['required'] == ['sample', 'can']
        assert schema['additionalProperties'] is False

    def test_a_reference_is_an_object_with_its_kind_under_at_ref(self) -> None:
        defs = IOFQ.serialize().params_schema['$defs']
        for kind in (OutputRef, AccumulatorRef):
            assert defs[kind.__name__] == kind.model_json_schema()
            assert '@ref' in defs[kind.__name__]['required']
        assert defs['SampleRun'] == {
            **file_model_def('SampleRun', {'kind': 'raw'}),
            'description': "A sample run's file.",
        }
        assert '@ref' in defs['SampleRun']['required']

    def test_a_float_and_an_enum_are_their_json_schema_only(self) -> None:
        params = model('P', scaling=(Scaling, Scaling()))
        schema = spec(params=params).serialize().params_schema
        assert schema['$defs']['Scaling']['properties'] == {
            'scale': {
                'type': 'number',
                'exclusiveMinimum': 0,
                'default': 1.0,
                'title': 'Scale',
                'description': 'A factor.',
            },
            'mode': {'$ref': '#/$defs/Mode', 'default': 'fast'},
        }

    def test_the_tag_of_an_alternative_is_titled_by_its_value_unless_given(
        self,
    ) -> None:
        class ByCount(BaseModel):
            mode: Literal['by_count'] = Field(
                title='Per count', description='By the counts of the run.'
            )

        by = (Annotated[ByCount | ByTime, Field(discriminator='mode')], ...)
        schema = spec(params=model('P', by=by)).serialize().params_schema
        assert schema['properties']['by']['oneOf'] == [
            {'$ref': '#/$defs/ByCount', 'title': 'Per count', 'required': ['mode']},
            {'$ref': '#/$defs/ByTime', 'title': 'Time', 'required': ['mode']},
        ]
        # The definition of a group is its own, which another place may share.
        assert schema['$defs']['ByCount']['properties']['mode'] == {
            'const': 'by_count',
            'type': 'string',
            'title': 'Per count',
            'description': 'By the counts of the run.',
        }

    def test_a_group_that_is_an_alternative_and_a_parameter_keeps_its_default_tag(
        self,
    ) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        by = (Annotated[ByWindow | ByTime, Field(discriminator='mode')], ...)
        params = model('P', by=by, window=(ByWindow, ByWindow()))
        schema = spec(params=params).serialize().params_schema
        jsonschema.validate({'by': {'mode': 'window'}, 'window': {}}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'by': {}, 'window': {}}, schema)

    def test_a_file_model_or_a_referenced_group_is_a_reference_to_its_type(
        self,
    ) -> None:
        schema = IOFQ.serialize().params_schema
        properties = schema['properties']
        assert {n: properties[n] for n in ('sample', 'can', 'beam_centre')} == {
            'sample': {
                **file_ref('SampleRun'),
                'title': 'Sample run',
            },
            'can': {**file_ref('CanRun'), 'title': 'Can'},
            'beam_centre': {
                'anyOf': [data_ref('BeamCentre'), {'type': 'null'}],
                'default': None,
                'title': 'Beam Centre',
            },
        }
        assert 'BeamCentre' not in schema['$defs']

    def test_a_file_model_is_a_reference_to_a_file_with_its_metadata(
        self,
    ) -> None:
        schema = FILES.serialize().params_schema
        properties = schema['properties']
        assert properties['mask']['anyOf'] == [file_ref('MaskFile'), {'type': 'null'}]
        mask = file_model_def('MaskFile', {'kind': 'mask'})
        assert schema['$defs']['MaskFile'] == mask
        assert properties['geometry'] == {
            **file_ref('GeometryFile'),
            'title': 'Geometry',
        }
        assert properties['runs']['items'] == file_ref('RunFile')

    def test_a_file_model_at_a_field_is_a_reference_to_a_file(self) -> None:
        group = model(
            'G',
            run=(SampleRun, ...),
            masks=(list[MaskFile], []),
            thickness=(float, 1.0),
        )
        schema = spec(params=model('P', g=(group, ...))).serialize().params_schema
        assert schema['properties']['g'] == {
            '$ref': '#/$defs/G',
            'title': 'G',
        }
        properties = schema['$defs']['G']['properties']
        assert properties['run'] == file_ref('SampleRun')
        assert properties['masks']['items'] == file_ref('MaskFile')

    def test_an_unmarked_union_is_its_alternatives_inline(self) -> None:
        params = model(
            'P',
            by=(By, ...),
            maybe=(By | None, None),
            each=(list[By], []),
            named=(Annotated[ByMonitor | ByTime, pydantic.Discriminator('mode')], ...),
        )
        properties = spec(params=params).serialize().params_schema['properties']
        alternatives = {
            'oneOf': [
                {'$ref': f'#/$defs/{name}', 'title': title, 'required': ['mode']}
                for name, title in (('ByMonitor', 'Monitor'), ('ByTime', 'Time'))
            ],
            'discriminator': {
                'propertyName': 'mode',
                'mapping': {
                    'monitor': '#/$defs/ByMonitor',
                    'time': '#/$defs/ByTime',
                },
            },
        }
        assert properties['by'] == {**alternatives, 'title': 'By'}
        assert properties['maybe']['anyOf'] == [alternatives, {'type': 'null'}]
        assert properties['each']['items'] == alternatives
        assert properties['named'] == {**alternatives, 'title': 'Named'}

    def test_a_union_with_referenced_members_is_its_alternatives_or_a_reference(
        self,
    ) -> None:
        params = model(
            'P',
            by=(By | Referenced[BeamCentre], ...),
            either=(Referenced[BeamCentre] | Referenced[Centring] | None, None),
        )
        properties = spec(params=params).serialize().params_schema['properties']
        (written, ref) = properties['by']['oneOf']
        assert [a['$ref'] for a in written['oneOf']] == [
            '#/$defs/ByMonitor',
            '#/$defs/ByTime',
        ]
        assert written['discriminator']['propertyName'] == 'mode'
        assert ref == data_ref('BeamCentre')
        assert properties['either']['anyOf'] == [
            data_ref('BeamCentre | Centring'),
            {'type': 'null'},
        ]

    def test_a_list_parameter_has_the_forms_per_row(self) -> None:
        params = model(
            'P',
            time_range=(
                list[TimeRange | Referenced[TimeRange]],
                Field(min_length=2, title='Slices'),
            ),
        )
        properties = spec(params=params).serialize().params_schema['properties']
        assert properties['time_range'] == {
            'type': 'array',
            'items': {'oneOf': [{'$ref': '#/$defs/TimeRange'}, data_ref('TimeRange')]},
            'minItems': 2,
            'title': 'Slices',
        }

    def test_tables_are_arrays_and_listed(self) -> None:
        serialized = IOFQ_MULTI.serialize()
        properties = serialized.params_schema['properties']
        assert properties['sample'] == {
            'type': 'array',
            'items': file_ref('SampleRun'),
            'title': 'Sample run',
            'minItems': 1,
        }
        assert properties['can'] == {
            'type': 'array',
            'items': file_ref('CanRun'),
            'title': 'Can',
            'minItems': 1,
        }
        assert serialized.tables == ('sample', 'can')
        assert serialized.params_schema['required'] == ['sample', 'can']

    def test_outputs_name_the_type_of_each_value(self) -> None:
        assert IOFQ.serialize().outputs_schema['properties'] == {
            'iofq': {
                'dataType': 'DataArray',
                'title': 'I(Q)',
                'description': 'Scattering intensity.',
            },
            'banks': {
                'anyOf': [
                    {
                        'type': 'object',
                        'additionalProperties': {'dataType': 'DataArray'},
                    },
                    {'type': 'null'},
                ],
                'default': None,
            },
        }

    @pytest.mark.parametrize(
        'params',
        [
            {'sample': SAMPLE},
            {
                'sample': SAMPLE,
                'reduction': Reduction(bins=4),
                'beam_centre': OutputRef(record='r2', output='centre'),
            },
            {
                'sample': SAMPLE,
                'reduction': OutputRef(record='r1', output='reduction', key='0'),
                'time_range': AccumulatorRef(accumulator='a1', output='slices'),
            },
        ],
    )
    def test_the_schema_takes_a_request(self, params: dict[str, Any]) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        schema = IOFQ.serialize().params_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(to_jsonable_python({**CAN, **params}), schema)

    @pytest.mark.parametrize(
        'params',
        [
            {'sample': 'run.nxs'},
            {'sample': {'path': 'run.nxs'}},
            {'sample': SAMPLE, 'reduction': {'bins': 'x'}},
            {'sample': {'@ref': 'file'}},
            {'sample': {'file': 'run:loki/1'}},
            {'sample': SAMPLE, 'beam_centre': FileRef(file='run:loki/2')},
            {'sample': OutputRef(record='r1', output='sample')},
            {
                'sample': SAMPLE,
                'reduction': {'bins': OutputRef(record='r1', output='b')},
            },
            {'sample': SAMPLE, 'speed': 1.0},
            {'sample': SAMPLE, 'reduction': {'bins': 4, 'extra': 1}},
            {'sample': SAMPLE, 'reduction': {'@ref': 'x'}},
        ],
    )
    def test_the_schema_refuses_a_malformed_request(
        self, params: dict[str, Any]
    ) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                to_jsonable_python({**CAN, **params}), IOFQ.serialize().params_schema
            )

    def test_the_schema_takes_a_reference_at_a_parameter_only(self) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        params = model(
            'P',
            reduction=(Reduction | Referenced[Reduction], Reduction()),
            settings=(Reduction, Reduction()),
            by=(By, ...),
            gate=(Annotated[ByMonitor | ByWindow, Field(discriminator='mode')], ...),
            sample=(SampleRun, ...),
            ranges=(list[TimeRange | Referenced[TimeRange]], []),
        )
        schema = spec(params=params).serialize().params_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        ref = OutputRef(record='r1', output='x')
        file = FileRef(file='run:loki/1')
        request = {
            'reduction': ref,
            'by': {'mode': 'monitor'},
            'gate': {'mode': 'window', 'start': 1.0},
            'sample': file,
            'ranges': [{'start': 0.0, 'stop': 1.0}, ref],
        }
        jsonschema.validate(to_jsonable_python(request), schema)
        for refused in (
            {'reduction': {'bins': ref}},  # a scalar is given inline only
            {'settings': ref},  # an unmarked group takes no reference
            {'by': ref},  # an unmarked union takes no reference
            {'by': {'mode': 'time', 'seconds': ref}},
            {'by': {'mode': ref, 'seconds': 1.0}},  # at a tag
            {'gate': {'mode': 'window', 'start': ref}},
            {'sample': ref},  # a file model takes a reference to a file only
            {'ranges': ref},  # a list is not given by one reference
            {'ranges': [{'start': ref, 'stop': 1.0}]},
        ):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(to_jsonable_python({**request, **refused}), schema)

    def test_the_schema_of_tables_takes_rows_of_groups(self) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        schema = GEOMETRIES.serialize().params_schema
        rows = [
            {'thickness': 0.1},
            SampleGeometry(thickness=0.2),
            OutputRef(record='r1', output='geometry'),
        ]
        jsonschema.validate(to_jsonable_python({'geometry': rows}), schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'geometry': []}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                to_jsonable_python({'geometry': [FileRef(file='run:loki/1')]}),
                schema,
            )

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

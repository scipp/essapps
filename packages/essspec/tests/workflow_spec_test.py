# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import re
from datetime import datetime
from enum import Enum
from pathlib import Path, PosixPath, PurePosixPath
from typing import Annotated, Any, Literal, NewType, TypedDict

import pydantic
import pytest
import scipp as sc
from annotated_types import Ge, Gt
from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    RootModel,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python

from ess.spec import (
    AccumulatorRef,
    DatasetModel,
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


class RunFile(DatasetModel, kind='raw'):
    pass


class MaskFile(DatasetModel, kind='mask'):
    pass


class GeometryFile(DatasetModel):
    """A dataset model without keywords: it takes raw datasets."""


class SampleRun(BaseModel):
    """A sample run and the geometry given next to it: a group that holds a file."""

    file: RunFile
    geometry: SampleGeometry | None = None


class CanRun(BaseModel):
    file: RunFile


class Node(BaseModel):
    """A recursive group."""

    name: str
    children: list['Node'] = []


class Tree(BaseModel):
    """A group that holds itself through another group."""

    branch: 'Branch | None' = None


class Branch(BaseModel):
    tree: Tree | None = None


Tree.model_rebuild()


class ByMonitor(BaseModel):
    mode: Literal['monitor']


class ByTime(BaseModel):
    mode: Literal['time']
    seconds: float


class Normalization(BaseModel):
    """A group that holds a union of groups: refused."""

    by: ByMonitor | ByTime = Field(ByMonitor(mode='monitor'), discriminator='mode')


By = Annotated[ByMonitor | ByTime, Field(discriminator='mode')]


class ByRun(BaseModel):
    mode: Literal['run'] = 'run'
    run: RunFile


class ByWindow(BaseModel):
    """An alternative with a group field."""

    mode: Literal['window'] = 'window'
    window: TimeRange


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


class CentredRun(BaseModel, arbitrary_types_allowed=True):
    """A group that holds a file and a data value, which no request writes."""

    file: RunFile
    centre: sc.Variable


class CalibrationFile(DatasetModel, kind='calibration'):
    """A dataset model with a field other than its path: refused."""

    run: int


class XmlMaskFile(MaskFile):
    """A dataset model that checks its path."""

    @field_validator('path')
    @classmethod
    def _xml(cls, path: Path) -> Path:
        if path.suffix != '.xml':
            raise ValueError('a mask is an XML file')
        return path


class BeamStopMaskFile(XmlMaskFile):
    """A subclass of a subclass of DatasetModel that keeps its path."""


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


class BeamCentre(BaseModel, arbitrary_types_allowed=True):
    """A group that holds a data value: given by reference only."""

    position: sc.Variable


class ByCentre(BaseModel, arbitrary_types_allowed=True):
    """An alternative that holds a data value: refused, no request writes it."""

    mode: Literal['centre'] = 'centre'
    centre: sc.Variable


class Centring(BaseModel):
    """A group given inline, with a place for a group given by reference only."""

    centre: BeamCentre | None = None


class ByPeak(BaseModel):
    """An alternative written inline, with a place for a group given by reference."""

    mode: Literal['peak'] = 'peak'
    centre: BeamCentre


@pydantic.dataclasses.dataclass(
    config=pydantic.ConfigDict(arbitrary_types_allowed=True)
)
class CentrePeak:
    """A dataclass that holds a data value: a data value itself."""

    centre: sc.Variable


class IofQSettings(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    time_range: TimeRange | None = None
    beam_centre: BeamCentre | None = None


class IofQParams(IofQSettings):
    sample: SampleRun = Field(title='Sample run')
    can: CanRun

    @model_validator(mode='after')
    def _sample_is_not_the_can(self) -> 'IofQParams':
        if self.sample.file == self.can.file:
            raise ValueError('the sample run is not the can run')
        return self


class IofQMultiParams(IofQSettings):
    sample: list[SampleRun] = Field(min_length=1, title='Sample run')
    can: list[CanRun] = Field(min_length=1)


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: sc.DataArray = Field(title='I(Q)', description='Scattering intensity.')
    banks: dict[str, sc.DataArray] | None = None


class Loose(BaseModel, extra='allow'):
    """A group that takes any field, ``@ref`` among them."""


class Tagged(Enum):
    """An enum whose JSON form is an object with the key ``@ref``."""

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


RUNS = spec(
    name='sans-runs',
    params=model('P', sample=(list[SampleRun], Field(min_length=1))),
    tables=('sample',),
)
DATASETS = spec(
    name='sans-datasets',
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
            'sample',
            'can',
        ]

    @pytest.mark.parametrize(
        'tp',
        [float, Mode, list[float], Reduction | None],
    )
    def test_a_group_with_a_number_an_enum_or_a_list_field_takes_an_inline_value(
        self, tp: Any
    ) -> None:
        group = model('Group', value=(tp, ...))
        assert _takes_inline(spec(params=model('P', group=(group, ...))), 'group')

    @pytest.mark.parametrize(
        'tp', [By, list[By], Annotated[ByRun | ByTime, Field(discriminator='mode')]]
    )
    def test_a_union_of_groups_takes_an_inline_value(self, tp: Any) -> None:
        assert _takes_inline(spec(params=model('P', value=(tp, ...))), 'value')

    @pytest.mark.parametrize('tp', [BeamCentre, list[BeamCentre | None]])
    def test_a_group_that_holds_a_data_value_takes_no_inline_value(
        self, tp: Any
    ) -> None:
        assert not _takes_inline(spec(params=model('P', value=(tp, ...))), 'value')


def _takes_inline(spec: WorkflowSpec[Any, Any], name: str) -> bool:
    """
    Whether the params schema offers an inline form for the parameter
    ``name``, or each row of it: a form that is not a reference, which names
    its type in ``dataType``, or ``null``.
    """
    prop = spec.serialize().params_schema['properties'][name]
    prop = prop.get('items', prop)
    if 'dataType' in prop:
        return False
    forms = prop.get('anyOf', [prop])
    return any('dataType' not in f and f.get('type') != 'null' for f in forms)


NOT_A_VALUE = 'the default is not a value a request may write'
NOT_A_PARAMETER = re.escape(
    'a parameter is a group, a union of groups, or a list of one, possibly typed '
    'X | None'
)
NOT_AN_OUTPUT = re.escape(
    'an output is a type, possibly typed X | None, or a list or a dict of one'
)
OUTSIDE_A_DATASET_MODEL = re.escape(
    "a Path is outside a dataset model; a file is a dataset model, such as class "
    "MaskFile(DatasetModel, kind='mask'), given by reference to a dataset"
)

KEEPS_THE_PATH = re.escape(
    'a dataset model keeps the path of DatasetModel, path: Path, without a '
    'default or an alias; a check of the path is a field validator'
)
DATASET_MODEL_OUTPUT = re.escape(
    'is a dataset model; no output holds one, so that a file comes from a '
    'dataset, which the dataset source checks'
)


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
            {'params': model('P', runs=(list[SampleRun], []))},
            {'params': model('P', run=(model('Run', file=(RunFile, ...)), ...))},
            {'params': model('P', runs=(list[SampleRun | None], []))},
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
                    'P', mask=(model('Mask', pixels=(sc.Variable, ...)), ...)
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
            {
                'params': model(
                    'P',
                    reduction=(Reduction, Reduction()),
                    both=(model('Both', a=(Reduction, ...), b=(Reduction, ...)), ...),
                )
            },
            {'params': model('P', by=(By, ByMonitor(mode='monitor')))},
            {
                'params': model(
                    'P',
                    by=(ByMonitor | ByTime, Field(discriminator='mode')),
                    run=(Annotated[ByRun | ByTime, Field(discriminator='mode')], ...),
                )
            },
            {'params': model('P', by=(By | None, None), each=(list[By], []))},
            {'params': model('P', centre=(validated('Centre', x=(float, ...)), ...))},
            {'params': model('P', run=(validated('Run', file=(RunFile, ...)), ...))},
            {'params': model('P', mask=(XmlMaskFile, ...))},
            {'params': model('P', mask=(BeamStopMaskFile | None, None))},
            {
                'params': model(
                    'P', runs=(model('Runs', files=(list[RunFile], [])), ...)
                )
            },
            {
                'params': model(
                    'P',
                    run=(
                        model('Run', geometry=(model('G', file=(RunFile, ...)), ...)),
                        ...,
                    ),
                )
            },
            {
                'params': model(
                    'P', reduction=(model('R', centre=(BeamCentre | None, None)), ...)
                )
            },
            {
                'params': model(
                    'P',
                    run=(
                        model('Run', file=(RunFile, ...), centre=(BeamCentre, ...)),
                        ...,
                    ),
                )
            },
            {
                'params': model(
                    'P',
                    centre=(BeamCentre | None, None),
                    centres=(list[BeamCentre], []),
                )
            },
            {
                'params': model(
                    'P',
                    by=(Annotated[ByTime | ByPeak, Field(discriminator='mode')], ...),
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
                {
                    'sample': {'file': {'path': 'a.nxs'}},
                    'can': {'file': {'path': 'a.nxs'}},
                }
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
            (
                {'params': model('P', a=(CentredRun, ...), b=(CentredRun, ...))},
                'P.a: CentredRun.centre: no request writes it.*; '
                'P.b: CentredRun.centre: no request writes it',
            ),
            (
                {
                    'params': model(
                        'P',
                        both=(
                            model('Both', a=(CentredRun, ...), b=(CentredRun, ...)),
                            ...,
                        ),
                    )
                },
                'P.both: Both.a: CentredRun.centre: no request writes it.*; '
                'P.both: Both.b: CentredRun.centre: no request writes it',
            ),
            *(
                (
                    {'params': model('P', n=(model('N', by=by), ...))},
                    'P.n: N.by: a union of groups is a parameter, never a field of a '
                    'group',
                )
                for by in (
                    (By, ...),
                    (By | None, None),
                    (list[By], []),
                    (list[By] | None, None),
                    (dict[str, By], {}),
                    (Annotated[ByRun | ByTime, Field(discriminator='mode')], ...),
                )
            ),
            (
                {'params': model('P', normalization=(Normalization, ...))},
                'P.normalization: Normalization.by: a union of groups is a parameter',
            ),
            (
                {
                    'params': model(
                        'P', centre=(BeamCentre, BeamCentre(position=sc.scalar(1.0)))
                    )
                },
                f'P.centre: {NOT_A_VALUE}: centre: BeamCentre is given by reference: '
                'no request writes its field position, of Variable',
            ),
            (
                {
                    'params': model(
                        'P',
                        centring=(
                            Centring,
                            Centring(centre=BeamCentre(position=sc.scalar(1.0))),
                        ),
                    )
                },
                f'P.centring: {NOT_A_VALUE}: centring.centre: BeamCentre is given by '
                'reference: no request writes its field position, of Variable',
            ),
            (
                {
                    'params': model(
                        'P',
                        r=(
                            model(
                                'R',
                                x=(
                                    float,
                                    {'@ref': 'output', 'record': 'r1', 'output': 'x'},
                                ),
                            ),
                            ...,
                        ),
                    )
                },
                f'P.r: R.x: {NOT_A_VALUE}: x: r1.x: float is given inline; a '
                'reference stands for a group only',
            ),
            *(
                (
                    {
                        'params': model(
                            'P', run=(model('Run', file=(RunFile, ...), **centre), ...)
                        )
                    },
                    f'P.run: {where}: no request writes it, since a group that holds '
                    'a dataset model is given field by field',
                )
                for centre, where in (
                    ({'centre': (sc.Variable, ...)}, 'Run.centre'),
                    ({'shifts': (list[sc.Variable], [])}, 'Run.shifts'),
                    (
                        {
                            'can': (
                                model(
                                    'Can',
                                    file=(RunFile, ...),
                                    centre=(sc.Variable, ...),
                                ),
                                ...,
                            )
                        },
                        'Run.can: Can.centre',
                    ),
                )
            ),
            (
                {'params': model('P', reduction=(dict[str, Reduction], {}))},
                f'P.reduction: {NOT_A_PARAMETER}',
            ),
            (
                {'params': model('P', reduction=(list[Reduction] | None, None))},
                f'P.reduction: {NOT_A_PARAMETER}',
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
                'P.by: a union holds groups only, or no group',
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
                'P.by: ByCentre.centre: no request writes it, since an alternative '
                'of a union is written inline, with its tag, and no reference fits a '
                'union',
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
                'P.by: ByCentre.centre: no request writes it',
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
                {'params': model('P', tree=(Node, ...))},
                'P.tree: Node.children: Node: a group is not at a place inside itself',
            ),
            (
                {'params': model('P', tree=(Tree, Tree()))},
                'P.tree: Tree.branch: Branch.tree: Tree: a group is not at a place '
                'inside itself',
            ),
            *(
                (
                    {'params': model('P', **fields)},
                    f'{where}: {OUTSIDE_A_DATASET_MODEL}',
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
            *(
                (
                    {'params': model('P', **fields)},
                    rf"{where}: CalibrationFile: a dataset model holds only path, not "
                    r"\['run'\]",
                )
                for fields, where in (
                    ({'calibration': (CalibrationFile, ...)}, 'P.calibration'),
                    (
                        {'run': (model('Run', file=(CalibrationFile, ...)), ...)},
                        'P.run: Run.file',
                    ),
                )
            ),
            *(
                (
                    {'params': model('P', mask=(mask, ...))},
                    rf'P.mask: {mask.__name__}.path: {KEEPS_THE_PATH}',
                )
                for mask in (
                    pydantic.create_model(
                        'StrMask', __base__=MaskFile, path=(str, ...)
                    ),
                    pydantic.create_model(
                        'OptionalMask', __base__=MaskFile, path=(Path | None, None)
                    ),
                    pydantic.create_model(
                        'DefaultMask', __base__=MaskFile, path=(Path, Path('mask.xml'))
                    ),
                    pydantic.create_model(
                        'AliasedMask',
                        __base__=MaskFile,
                        path=(Path, Field(alias='file')),
                    ),
                    pydantic.create_model(
                        'CheckedMask',
                        __base__=MaskFile,
                        path=(Annotated[Path, AfterValidator(lambda p: p)], ...),
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where} {DATASET_MODEL_OUTPUT}',
                )
                for fields, where in (
                    ({'raw': (RunFile, ...)}, 'O.raw: RunFile'),
                    ({'raws': (list[RunFile | None], [])}, 'O.raws: RunFile'),
                    (
                        {'g': (model('G', file=(RunFile, ...)), ...)},
                        'O.g: G.file: RunFile',
                    ),
                    (
                        {'g': (model('G', run=(SampleRun, ...)), ...)},
                        'O.g: SampleRun.file: RunFile',
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where}: {OUTSIDE_A_DATASET_MODEL}',
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
                {
                    'params': model(
                        'P', outer=(model('Outer', inner=(SerializedParams, ...)), ...)
                    )
                },
                r"P.outer: Outer.inner: SerializedParams: no serializers or "
                r"computed fields, {'field_serializers': \['_bins'\]}",
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model(
                                'Run',
                                file=(RunFile, ...),
                                bins=(int, Field(1, alias='b')),
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
                        'P', n=(model('N', reduction=(Reduction, {'bins': 3})), ...)
                    )
                },
                r"P.n: N.reduction: the default {'bins': 3} is not the value",
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
                        'P', loose=(Loose, Loose(**{'@ref': 'dataset', 'dataset': 'a'}))
                    )
                },
                rf'P.loose: {NOT_A_VALUE}: loose.@ref: '
                'Extra inputs are not permitted',
            ),
            (
                {'params': model('P', r=(model('R', kind=(str, {'@ref': 'a'})), ...))},
                f'P.r: R.kind: {NOT_A_VALUE}: kind: .*is not a kind of reference',
            ),
            (
                {
                    'params': model(
                        'P', r=(model('R', kind=(str, {'@ref': 'dataset'})), ...)
                    )
                },
                f'P.r: R.kind: {NOT_A_VALUE}: kind: .*not a reference to dataset',
            ),
            (
                {
                    'params': model(
                        'P',
                        r=(model('R', kind=(list[str], [{'@ref': 'output'}])), ...),
                    )
                },
                rf'P.r: R.kind: {NOT_A_VALUE}: kind\[0\]: .*not a reference to output',
            ),
            (
                {
                    'params': model(
                        'P', r=(model('R', kinds=(list[Tagged], [Tagged.a])), ...)
                    )
                },
                rf'P.r: R.kinds: {NOT_A_VALUE}: kinds\[0\]: has a JSON form that '
                'reads as a reference',
            ),
            (
                {
                    'params': model(
                        'P',
                        run=(
                            model(
                                'Run', file=(RunFile, ...), kind=(str, {'@ref': 'a'})
                            ),
                            ...,
                        ),
                    )
                },
                f'P.run: Run.kind: {NOT_A_VALUE}: kind: .*is not a kind of reference',
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
                f'P.runs: {NOT_A_PARAMETER}',
            ),
            (
                {'params': model('P', runs=(list, []))},
                f'P.runs: {NOT_A_PARAMETER}',
            ),
            (
                {
                    'params': model(
                        'P', sample=(SampleRun, SampleRun(file=RunFile(path='run.nxs')))
                    )
                },
                f'P.sample: {NOT_A_VALUE}: sample.file: .* is not a dataset; RunFile '
                r'is given by reference to a dataset, such as dataset\(path=...\)',
            ),
            (
                {
                    'params': model(
                        'P',
                        runs=(
                            list[SampleRun],
                            [SampleRun(file=RunFile(path='run.nxs'))],
                        ),
                    )
                },
                rf'P.runs: {NOT_A_VALUE}: runs\[0\].file: .* is not a dataset',
            ),
            (
                {'outputs': model('O', iofq=(sc.DataArray | sc.Variable, ...))},
                f'O.iofq: {NOT_AN_OUTPUT}',
            ),
            (
                {'outputs': model('O', iofq=(list[list[sc.DataArray]], ...))},
                f'O.iofq: {NOT_AN_OUTPUT}',
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
            sample=(SampleRun, SampleRun(file=RunFile(path='a'))),
        )
        with pytest.raises(ValueError, match=r'P\.centre: .*; P\.sample: '):
            spec(params=params)


OPTIONAL_VALUE = re.escape('typed X | None with X not a group')


def _group_with(tp: Any) -> type[BaseModel]:
    """A group with the field ``value`` of type ``tp``, and a tag."""
    return model('G', mode=(Literal['g'], 'g'), value=(tp, ...))


class TestOptionalFields:
    @pytest.mark.parametrize(
        'tp',
        [
            int | None,
            str | None,
            Mode | None,
            Literal['a', 'b'] | None,
            list[float] | None,
            list[Reduction] | None,
            sc.DataArray | None,
            list[float | None],
        ],
    )
    def test_a_field_of_a_group_is_typed_x_or_none_only_where_x_is_a_group(
        self, tp: Any
    ) -> None:
        with pytest.raises(ValueError, match=f'P.g: G.value: {OPTIONAL_VALUE}'):
            spec(params=model('P', g=(_group_with(tp), ...)))

    @pytest.mark.parametrize(
        ('params', 'where'),
        [
            (lambda g: model('P', g=(g, ...)), 'P.g: G.value'),
            (lambda g: model('P', g=(g | None, None)), 'P.g: G.value'),
            (lambda g: model('P', rows=(list[g], [])), 'P.rows: G.value'),
            (
                lambda g: model(
                    'P', by=(Annotated[g | ByTime, By.__metadata__[0]], ...)
                ),
                'P.by: G.value',
            ),
            (
                lambda g: model('P', o=(model('O', inner=(g | None, None)), ...)),
                'P.o: O.inner: G.value',
            ),
            (
                lambda g: model('P', o=(model('O', rows=(list[g], [])), ...)),
                'P.o: O.rows: G.value',
            ),
        ],
    )
    def test_x_or_none_is_refused_at_a_field_of_a_group_at_any_depth(
        self, params: Any, where: str
    ) -> None:
        with pytest.raises(ValueError, match=f'{where}: {OPTIONAL_VALUE}'):
            spec(params=params(_group_with(float | None)))

    @pytest.mark.parametrize(
        'tp',
        [
            Reduction | None,
            list[Reduction | None],
            list[SampleRun | None],
            Annotated[TimeRange | None, 'a note'],
        ],
    )
    def test_a_group_typed_x_or_none_is_a_field_of_a_group(self, tp: Any) -> None:
        spec(params=model('P', g=(_group_with(tp), ...)))
        spec(params=model('P', rows=(list[_group_with(tp)], [])))


class Limits(TypedDict):
    start: float
    stop: float


@pydantic.dataclasses.dataclass
class Pair:
    start: float
    stop: float


NOT_A_SCALAR = re.escape(
    'is not a scalar; a field of a group holds a group, a data value, or a '
    'scalar (int, float, str, bool, an enum, or a Literal), or a list of one'
)


class TestFieldKinds:
    @pytest.mark.parametrize(
        ('tp', 'name'),
        [
            (dict[str, float], 'dict'),
            (dict[str, sc.DataArray], 'dict'),
            (tuple[float, float], 'tuple'),
            (set[float], 'set'),
            (frozenset[Mode], 'frozenset'),
            (Limits, 'Limits'),
            (Pair, 'Pair'),
            (RootModel[list[float]], re.escape('RootModel[list[float]]')),
            (int | float, re.escape('int | float')),
            (datetime, 'datetime'),
            (Any, 'Any'),
            (list[list[float]], 'list'),
            (list[tuple[float, float]], 'tuple'),
            (list[Limits], 'Limits'),
        ],
    )
    def test_a_field_of_a_group_holds_only_what_a_form_shows(
        self, tp: Any, name: str
    ) -> None:
        with pytest.raises(ValueError, match=f'P.g: G.value: {name} {NOT_A_SCALAR}'):
            spec(params=model('P', g=(_group_with(tp), ...)))

    @pytest.mark.parametrize(
        ('params', 'where'),
        [
            (lambda g: model('P', rows=(list[g], [])), 'P.rows: G.value'),
            (
                lambda g: model(
                    'P', by=(Annotated[g | ByTime, By.__metadata__[0]], ...)
                ),
                'P.by: G.value',
            ),
            (
                lambda g: model('P', o=(model('O', inner=(g | None, None)), ...)),
                'P.o: O.inner: G.value',
            ),
            (
                lambda g: model('P', o=(model('O', rows=(list[g], [])), ...)),
                'P.o: O.rows: G.value',
            ),
        ],
    )
    def test_a_value_of_several_parts_is_refused_at_any_depth(
        self, params: Any, where: str
    ) -> None:
        with pytest.raises(ValueError, match=f'{where}: tuple {NOT_A_SCALAR}'):
            spec(params=params(_group_with(tuple[float, float])))

    @pytest.mark.parametrize(
        'tp',
        [
            int,
            float,
            str,
            bool,
            Mode,
            Literal['a', 'b'],
            Annotated[float, Gt(0)],
            list[float],
            list[Annotated[int, Ge(0)]],
            list[Mode],
            RunFile,
            list[RunFile],
            RunFile | None,
            Reduction,
            Reduction | None,
            list[Reduction],
            sc.DataArray,
            list[sc.DataArray],
            sc.Variable | sc.DataArray,
        ],
    )
    def test_a_field_of_a_group_holds_a_group_a_scalar_or_a_data_value(
        self, tp: Any
    ) -> None:
        spec(params=model('P', g=(_group_with(tp), ...)))

    def test_a_group_with_a_data_value_takes_no_inline_value(self) -> None:
        params = model('P', g=(_group_with(sc.Variable | sc.DataArray), ...))
        assert not _takes_inline(spec(params=params), 'g')


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
            'P',
            sample=(list[SampleRun | None], ...),
            reduction=(list[Reduction], ...),
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

    def test_a_table_may_be_of_dataset_models(self) -> None:
        assert DATASETS.tables == ('runs',)
        assert not _takes_inline(DATASETS, 'runs')
        assert not DATASETS.has_json_form('runs')
        assert IOFQ_MULTI.has_json_form('time_range')

    def test_a_table_may_be_of_groups_that_hold_a_dataset_model(self) -> None:
        assert _takes_inline(RUNS, 'sample')
        params = RUNS.params(
            sample=[
                {'file': {'path': Path('a.nxs')}},
                {'file': {'path': Path('b.nxs')}, 'geometry': {'thickness': 0.2}},
            ]
        )
        assert [row.geometry for row in params.sample] == [
            None,
            SampleGeometry(thickness=0.2),
        ]

    def test_the_field_validators_of_a_group_check_each_row(self) -> None:
        with pytest.raises(pydantic.ValidationError, match='thickness must be'):
            RUNS.params(
                sample=[
                    {'file': {'path': Path('a.nxs')}, 'geometry': {'thickness': -1.0}}
                ]
            )

    def test_the_validators_of_the_element_type_check_each_row(self) -> None:
        def known[R: SampleRun | CanRun](run: R) -> R:
            if run.file.path.name != 'run.nxs':
                raise ValueError('unknown run')
            return run

        params = model(
            'P',
            sample=(list[Annotated[SampleRun, AfterValidator(known)]], ...),
            can=(list[Annotated[CanRun, AfterValidator(known)]], ...),
        )
        runs = spec(params=params, tables=('sample', 'can'))
        row = {'file': {'path': Path('run.nxs')}}
        other = {'file': {'path': Path('other.nxs')}}
        known_rows = {'sample': [row], 'can': [row]}
        runs.params(**known_rows)
        for rows in ({'sample': [other]}, {'can': [other]}):
            with pytest.raises(pydantic.ValidationError, match='unknown run'):
                runs.params(**{**known_rows, **rows})
        assert runs.serialize().params_schema['properties']['sample']['items'] == (
            inline({'$ref': '#/$defs/SampleRun'})
        )


def dataset_ref(data_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """The schema of a reference to a dataset, of a dataset model."""
    return {'$ref': '#/$defs/DatasetRef', 'dataType': data_type, 'metadata': metadata}


def data_ref(data_type: str) -> dict[str, Any]:
    """The schema of a reference to a group: an output."""
    kinds = ('OutputRef', 'AccumulatorRef')
    return {'anyOf': [{'$ref': f'#/$defs/{k}'} for k in kinds], 'dataType': data_type}


def inline(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema of an inline value: an object with the key ``@ref`` is not one."""
    return {**schema, 'not': {'required': ['@ref']}}


SAMPLE = {'file': DatasetRef(dataset='run:loki/1')}
CAN = {'can': {'file': DatasetRef(dataset='run:loki/3')}}
"""The can that each request of ``IOFQ`` gives, left out of the cases below."""


class Scaling(BaseModel):
    scale: float = Field(default=1.0, gt=0, title='Scale', description='A factor.')
    mode: Mode = Mode.fast


class TestSerialization:
    def test_a_group_is_its_fields_or_a_reference(self) -> None:
        schema = IOFQ.serialize().params_schema
        assert schema['title'] == 'IofQParams'
        assert list(schema['properties']) == list(IofQParams.model_fields)
        assert schema['properties']['reduction'] == {
            'anyOf': [inline({'$ref': '#/$defs/Reduction'}), data_ref('Reduction')],
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
        for kind in (OutputRef, DatasetRef, AccumulatorRef):
            assert defs[kind.__name__] == kind.model_json_schema()
            assert '@ref' in defs[kind.__name__]['required']

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
        defs = spec(params=model('P', by=by)).serialize().params_schema['$defs']
        assert defs['ByCount']['properties']['mode'] == {
            'const': 'by_count',
            'type': 'string',
            'title': 'Per count',
            'description': 'By the counts of the run.',
        }
        assert defs['ByTime']['properties']['mode'] == {
            'const': 'time',
            'type': 'string',
            'title': 'Time',
        }

    def test_a_dataset_model_or_a_group_with_a_data_value_is_a_reference_to_its_type(
        self,
    ) -> None:
        schema = IOFQ.serialize().params_schema
        properties = schema['properties']
        assert {n: properties[n] for n in ('sample', 'can', 'beam_centre')} == {
            'sample': {**inline({'$ref': '#/$defs/SampleRun'}), 'title': 'Sample run'},
            'can': {**inline({'$ref': '#/$defs/CanRun'}), 'title': 'Can'},
            'beam_centre': {
                'anyOf': [data_ref('BeamCentre'), {'type': 'null'}],
                'default': None,
                'title': 'Beam Centre',
            },
        }
        assert schema['$defs']['CanRun']['properties'] == {
            'file': dataset_ref('RunFile', {'kind': 'raw'})
        }
        assert 'BeamCentre' not in schema['$defs']

    def test_a_dataset_model_is_a_reference_to_a_dataset_with_its_metadata(
        self,
    ) -> None:
        properties = DATASETS.serialize().params_schema['properties']
        assert properties['mask']['anyOf'] == [
            {
                '$ref': '#/$defs/DatasetRef',
                'dataType': 'MaskFile',
                'metadata': {'kind': 'mask'},
            },
            {'type': 'null'},
        ]
        assert properties['geometry'] == {
            **dataset_ref('GeometryFile', {'kind': 'raw'}),
            'title': 'Geometry',
        }
        assert properties['runs']['items'] == dataset_ref('RunFile', {'kind': 'raw'})

    def test_a_union_is_its_alternatives_inline_never_a_reference(self) -> None:
        params = model('P', by=(By, ...), maybe=(By | None, None), each=(list[By], []))
        properties = spec(params=params).serialize().params_schema['properties']
        alternatives = inline(
            {
                'oneOf': [{'$ref': '#/$defs/ByMonitor'}, {'$ref': '#/$defs/ByTime'}],
                'discriminator': {
                    'propertyName': 'mode',
                    'mapping': {
                        'monitor': '#/$defs/ByMonitor',
                        'time': '#/$defs/ByTime',
                    },
                },
            }
        )
        assert properties['by'] == {**alternatives, 'title': 'By'}
        assert properties['maybe']['anyOf'] == [alternatives, {'type': 'null'}]
        assert properties['each']['items'] == alternatives

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
            'items': inline({'$ref': '#/$defs/SampleRun'}),
            'title': 'Sample run',
            'minItems': 1,
        }
        assert properties['can'] == {
            'type': 'array',
            'items': inline({'$ref': '#/$defs/CanRun'}),
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
                'sample': {**SAMPLE, 'geometry': {'thickness': 0.1}},
                'reduction': Reduction(bins=4),
                'beam_centre': OutputRef(record='r2', output='centre'),
            },
            {
                'sample': {**SAMPLE, 'geometry': OutputRef(record='r1', output='g')},
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
            {'sample': {'file': 'run.nxs'}},
            {'sample': {'file': {'path': 'run.nxs'}}},
            {'sample': SAMPLE, 'reduction': {'bins': 'x'}},
            {'sample': {'file': {'@ref': 'dataset'}}},
            {'sample': {'file': {'dataset': 'run:loki/1'}}},
            {'sample': DatasetRef(dataset='run:loki/1')},
            {'sample': SAMPLE, 'beam_centre': DatasetRef(dataset='run:loki/2')},
            {'sample': {'file': OutputRef(record='r1', output='file')}},
            {'sample': OutputRef(record='r1', output='sample')},
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

    def test_the_schema_takes_a_reference_at_a_group_at_any_depth(self) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        params = model(
            'P',
            reduction=(Reduction, Reduction()),
            by=(By, ...),
            gate=(Annotated[ByMonitor | ByWindow, Field(discriminator='mode')], ...),
            sample=(SampleRun, ...),
        )
        schema = spec(params=params).serialize().params_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        ref = OutputRef(record='r1', output='x')
        request = {
            'reduction': ref,
            'by': {'mode': 'monitor'},
            'gate': {'mode': 'window', 'window': ref},  # in an alternative
            'sample': {'file': DatasetRef(dataset='run:loki/1'), 'geometry': ref},
        }
        jsonschema.validate(to_jsonable_python(request), schema)
        for refused in (
            {'reduction': {'bins': ref}},  # a number is given inline only
            {'by': ref},  # no reference fits a union
            {'by': {'mode': 'time', 'seconds': ref}},
            {'by': {'mode': ref, 'seconds': 1.0}},  # at a tag
            {'gate': {'mode': 'window', 'window': {'start': ref, 'stop': 1.0}}},
            {
                'sample': {
                    'file': DatasetRef(dataset='run:loki/1'),
                    'geometry': {'thickness': ref},
                }
            },
            {'sample': {'file': ref}},  # a file is given by a dataset only
            {'sample': ref},  # an output of a group that holds a file
        ):
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(to_jsonable_python({**request, **refused}), schema)
        assert schema['properties']['sample'] == {
            **inline({'$ref': '#/$defs/SampleRun'}),
            'title': 'Sample',
        }

    def test_the_schema_of_tables_takes_rows_of_groups(self) -> None:
        jsonschema = pytest.importorskip('jsonschema')
        schema = RUNS.serialize().params_schema
        file = DatasetRef(dataset='run:loki/1')
        rows = [
            {'file': file},
            {'file': file, 'geometry': SampleGeometry(thickness=0.1)},
            {'file': file, 'geometry': OutputRef(record='r1', output='geometry')},
        ]
        jsonschema.validate(to_jsonable_python({'sample': rows}), schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'sample': []}, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(to_jsonable_python({'sample': [file]}), schema)

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

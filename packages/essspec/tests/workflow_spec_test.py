# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import re
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path, PosixPath, PurePosixPath
from typing import Annotated, Any, Literal, NewType, TypedDict

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
    File,
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


RunFile = Annotated[Path, File(kind='raw')]
MaskFile = Annotated[Path, File(kind='mask')]
GeometryFile = Annotated[Path, File()]
"""A file field without metadata: it takes raw files."""
SampleRun = Annotated[Path, File(kind='raw')]
CanRun = Annotated[Path, File(kind='raw')]


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
type RawFile = RunFile


class RoleRun(BaseModel):
    """A group with a field typed as a NewType."""

    file: SampleFile


class LocalPath(Path):
    """A subclass of Path."""


class TimeRangeAt(TimeRange):
    """An output type that fits a TimeRange and holds a Path."""

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
    """A group with a field that is not a field type: only a reference fills it."""

    position: sc.Variable


class ByCentre(BaseModel, arbitrary_types_allowed=True):
    """An alternative with a field that is not a field type: refused."""

    mode: Literal['centre'] = 'centre'
    centre: sc.Variable


class Centring(BaseModel):
    """
    A group with a field of a group that is not a field type, and of a tuple:
    only a reference fills it.
    """

    centre: BeamCentre | None = None
    tolerance: tuple[float, float] = (0.0, 1.0)


class ByPeak(BaseModel):
    """An alternative with a field of a group that is not a field type."""

    mode: Literal['peak'] = 'peak'
    centre: BeamCentre


@pydantic.dataclasses.dataclass(
    config=pydantic.ConfigDict(arbitrary_types_allowed=True)
)
class CentrePeak:
    """A dataclass: not a field type."""

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
        if self.sample == self.can:
            raise ValueError('the sample run is not the can run')
        return self


class IofQMultiParams(IofQSettings):
    sample: list[SampleRun] = Field(min_length=1, title='Sample run')
    can: list[CanRun] = Field(min_length=1)


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: sc.DataArray = Field(title='I(Q)', description='Scattering intensity.')
    banks: dict[str, sc.DataArray] | None = None


class Measured(BaseModel):
    """A group with a file field."""

    run: RunFile


class Loose(BaseModel, extra='allow'):
    """A group that takes any field, ``@ref`` among them."""


class Tagged(Enum):
    """An enum whose value is an object: not a scalar."""

    a = {'@ref': 'a'}  # noqa: RUF012


class SelfContained(BaseModel):
    """A group that contains itself: refused."""

    child: 'SelfContained | None' = None


class ContainsA(BaseModel):
    """A group that contains itself by another group: refused."""

    b: 'ContainsB | None' = None


class ContainsB(BaseModel):
    a: ContainsA | None = None


ContainsA.model_rebuild()


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

    def test_a_parameter_takes_an_inline_value_unless_only_a_reference_fills_it(
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
    def test_a_referenced_parameter_takes_no_inline_value(self, tp: Any) -> None:
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
    if 'oneOf' in schema:  # the sources of a parameter
        return any(_has_inline_source(s, defs) for s in schema['oneOf'])
    (*_, name) = schema['$ref'].split('/')
    return '@ref' not in defs[name]['properties']


NOT_A_VALUE = 'the default is not a value a request may write'
NO_FILE_DEFAULT = re.escape(
    'a default names no file; a file field has no default but None or []'
)
NOT_A_FILE = re.escape("'run.nxs' is not a file; a file field is written as file(...)")
OPTIONAL_LIST = re.escape(
    'a list is not typed list[X] | None; a list without elements is []'
)
OPTIONAL_ELEMENT = re.escape(
    'the element of a list is not typed X | None; each element is a value'
)
LIST_OF_LISTS = re.escape('a list is not of lists; ')
NOT_AN_OUTPUT = re.escape(
    'an output is a type, possibly typed X | None, or a list or a dict of one'
)
UNMARKED_PATH = re.escape(
    "a Path without File(...); a parameter or a field that takes a file is a "
    "file field, such as mask: MaskFile with MaskFile = Annotated[Path, File("
    "kind='mask')], written as file(...)"
)


def not_a_field_type(where: str, name: str) -> str:
    """The regex of the refusal of ``name``, the type of the field ``where``."""
    return re.escape(
        f'{where}: {name} is not a field type, which is a scalar, a file field, a '
        'group, a union of groups, Referenced[T], X | None, or list[X]; give the '
        'field one of these types, '
    )


PATH_OUTPUT = re.escape(
    'a Path is in the data of an output; no output holds a file or a Path, so '
    'that every file comes from the file source, which checks it'
)


class TestModelRules:
    def test_the_example_spec_is_accepted(self) -> None:
        assert IOFQ.params is IofQParams
        assert IOFQ.outputs is IofQOutputs

    @pytest.mark.parametrize(
        'fields',
        [
            {'params': model('P', reduction=(list[Reduction], []))},
            {
                'params': model(
                    'P',
                    bins=(int, 10),
                    mode=(Mode, Mode.fast),
                    values=(list[float], []),
                    level=(float | None, None),
                )
            },
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
                    steps=(Annotated[list[TimeRange], AfterValidator(lambda r: r)], []),
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
            {'params': model('P', mask=(Annotated[Path | None, File()], None))},
            {
                'params': model(
                    'P', mask=(MaskFile | Referenced[BeamCentre] | None, None)
                )
            },
            {
                'params': model(
                    'P',
                    mask=(Annotated[Path, File(kind='mask'), Field(title='M')], ...),
                )
            },
            {
                'params': model(
                    'P',
                    g=(
                        model(
                            'G',
                            mask=(Referenced[BeamCentre], ...),
                            maybe=(Referenced[BeamCentre] | None, None),
                            masks=(list[Referenced[BeamCentre]], []),
                            inner=(
                                model('H', mask=(Referenced[BeamCentre], ...)),
                                ...,
                            ),
                        ),
                        ...,
                    ),
                )
            },
            {
                'params': model(
                    'P',
                    by=(
                        Annotated[ByMonitor | ByTime, Field(discriminator='mode')]
                        | Referenced[ByWindow],
                        ...,
                    ),
                )
            },
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
            IofQParams.model_validate({'sample': Path('a.nxs'), 'can': Path('a.nxs')})

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
                    not_a_field_type('P.value', name),
                )
                for value, name in (
                    ((int | float, 1), 'int | float'),
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
                        {'params': model('P', runs=(list[SampleFile], []))},
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
                        'P.run: Run.parts: Inner.file',
                        'SampleFile',
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
                    not_a_field_type('P.centre: BeamCentre.position', 'Variable')
                    + re.escape(
                        'or, if the data of an output fill BeamCentre, type the '
                        'field that holds it Referenced[BeamCentre]'
                    ),
                )
                for tp in (BeamCentre, BeamCentre | None, list[BeamCentre])
            ),
            (
                {'params': model('P', centring=(Centring, ...))},
                not_a_field_type(
                    'P.centring: Centring.centre: BeamCentre.position', 'Variable'
                )
                + '.*'
                + not_a_field_type(
                    'P.centring: Centring.tolerance', 'tuple[float, float]'
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
                f'P.centre: {NOT_A_VALUE}: centre: is not a reference to an output, '
                r'as the field is typed Referenced\[\.\.\.\]',
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
                        'P.g: Referenced[G]: G holds Referenced[...]; a reference is '
                        'never inside the data of an output'
                    ),
                )
                for group in (
                    Referenced[model('G', mask=(Referenced[BeamCentre], ...))],
                    Reduction
                    | Referenced[model('G', mask=(Referenced[BeamCentre], ...))],
                    Referenced[
                        model(
                            'G',
                            inner=(
                                model('H', masks=(list[Referenced[BeamCentre]], [])),
                                ...,
                            ),
                        )
                    ],
                )
            ),
            *(
                (
                    {'params': model('P', c=(tp, None))},
                    re.escape(
                        f'P.c: Referenced[{name}]: {name} is not a group; a reference '
                        'fills a field with a group, so that a picker of records '
                        'lists the outputs of a class that names a role'
                    ),
                )
                for tp, name in (
                    (Referenced[BeamCentre | None], 'BeamCentre | None'),
                    (Referenced[BeamCentre | Centring] | None, 'BeamCentre | Centring'),
                    (Referenced[float] | None, 'float'),
                )
            ),
            *(
                (
                    {'params': params},
                    re.escape(
                        f'{where}: Referenced[list[BeamCentre]]: list[BeamCentre] is '
                        'not a group'
                    ),
                )
                for params, where in (
                    (model('P', c=(Referenced[list[BeamCentre]], ...)), 'P.c'),
                    (
                        model(
                            'P',
                            g=(
                                model('G', c=(Referenced[list[BeamCentre]], ...)),
                                ...,
                            ),
                        ),
                        'P.g: G.c',
                    ),
                )
            ),
            *(
                (
                    {'params': model('P', run=(tp, ...))},
                    f'P.run: Measured.run: {PATH_OUTPUT}',
                )
                for tp in (Referenced[Measured], list[Referenced[Measured]])
            ),
            *(
                (
                    {'params': model('P', by=(tp, ...))},
                    re.escape(
                        'P.by: the discriminator of a union with a member '
                        'Referenced[...] is on the union written inline, '
                        "Annotated[A | B, Field(discriminator='kind')] | "
                        'Referenced[C], since a reference to an output has no tag'
                    ),
                )
                for tp in (
                    # A member Referenced[...] inside the union written inline.
                    Annotated[
                        ByMonitor | Referenced[ByTime], Field(discriminator='mode')
                    ]
                    | Referenced[BeamCentre],
                )
            ),
            (
                {
                    'params': model(
                        'P',
                        by=(
                            Annotated[
                                ByMonitor | ByTime | Referenced[ByWindow],
                                Field(discriminator='mode'),
                            ],
                            ...,
                        ),
                    )
                },
                re.escape(
                    'P.by: the discriminator of a union with a member Referenced[...] '
                    'is on the union written inline, Annotated[A | B, '
                    "Field(discriminator='kind')] | Referenced[C], since a reference "
                    'to an output has no tag'
                ),
            ),
            (
                {
                    'params': model(
                        'P',
                        g=(
                            model(
                                'G',
                                by=(
                                    Annotated[
                                        ByMonitor | ByTime | Referenced[ByWindow],
                                        Field(discriminator='mode'),
                                    ],
                                    ...,
                                ),
                            ),
                            ...,
                        ),
                    )
                },
                'P.g: G.by: the discriminator of a union with a member Referenced',
            ),
            (
                {'params': model('P', g=(SelfContained, ...))},
                'P.g: SelfContained.child: SelfContained contains itself; a group '
                'holds no field of its own type, at any depth',
            ),
            (
                {'params': model('P', g=(list[ContainsB], []))},
                'P.g: ContainsB.a: ContainsA.b: ContainsB contains itself',
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
                not_a_field_type('P.reduction', 'dict[str, Reduction]'),
            ),
            (
                {'params': model('P', reduction=(list[Reduction] | None, None))},
                f'P.reduction: {OPTIONAL_LIST}',
            ),
            (
                {'params': model('P', reduction=(list[Reduction | None], []))},
                f'P.reduction: {OPTIONAL_ELEMENT}',
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
                not_a_field_type('P.by: ByCentre.centre', 'Variable'),
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
                not_a_field_type('P.by: ByCentre.centre', 'Variable'),
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
                not_a_field_type('P.by', 'dict[str, ByMonitor | ByTime]'),
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
                not_a_field_type(
                    'P.by: ByPeak.centre: BeamCentre.position', 'Variable'
                ),
            ),
            *(
                (
                    {'params': model('P', **fields)},
                    f'{where}: {UNMARKED_PATH}',
                )
                for fields, where in (
                    ({'path': (Path, ...)}, 'P.path'),
                    ({'paths': (list[Path], [])}, 'P.paths'),
                    ({'path': (Path | None, None)}, 'P.path'),
                    ({'path': (PurePosixPath, ...)}, 'P.path'),
                    ({'path': (PosixPath, ...)}, 'P.path'),
                    ({'path': (LocalPath, ...)}, 'P.path'),
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
                        {
                            'run': (
                                model(
                                    'Run', geometry=(model('G', path=(Path, ...)), ...)
                                ),
                                ...,
                            )
                        },
                        'P.run: Run.geometry: G.path',
                    ),
                )
            ),
            *(
                (
                    {'params': model('P', **fields)},
                    not_a_field_type(where, name),
                )
                for fields, where, name in (
                    ({'pair': (tuple[Path, Path], ...)}, 'P.pair', 'tuple[Path, Path]'),
                    ({'runs': (RunFiles, ...)}, 'P.runs', 'RunFiles'),
                    (
                        {'runs': (RootModel[list[Path]], ...)},
                        'P.runs',
                        'RootModel[list[Path]]',
                    ),
                    (
                        {'run': (model('Run', files=(RunPaths, ...)), ...)},
                        'P.run: Run.files',
                        'RunPaths',
                    ),
                    (
                        {'run': (model('Run', file=(tuple[Path, float], ...)), ...)},
                        'P.run: Run.file',
                        'tuple[Path, float]',
                    ),
                    (
                        {'run': (model('Run', files=(dict[str, Path], {})), ...)},
                        'P.run: Run.files',
                        'dict[str, Path]',
                    ),
                )
            ),
            *(
                (
                    {'params': model('P', **fields)},
                    rf'{where}: File\(\.\.\.\) marks a Path, not {re.escape(name)}',
                )
                for fields, where, name in (
                    (
                        {
                            'calibration': (
                                Annotated[int, File(kind='calibration')],
                                ...,
                            )
                        },
                        'P.calibration',
                        'int',
                    ),
                    (
                        {'masks': (Annotated[list[Path], File(kind='mask')], [])},
                        'P.masks',
                        'list[Path]',
                    ),
                    (
                        {'g': (Annotated[Measured, File()], ...)},
                        'P.g',
                        'Measured',
                    ),
                    (
                        {
                            'by': (
                                Annotated[
                                    ByMonitor | ByTime,
                                    Field(discriminator='mode'),
                                    File(),
                                ],
                                ...,
                            )
                        },
                        'P.by',
                        'ByMonitor | ByTime',
                    ),
                    (
                        {
                            'run': (
                                model('Run', n=(Annotated[int, File()], ...)),
                                ...,
                            )
                        },
                        'P.run: Run.n',
                        'int',
                    ),
                )
            ),
            (
                {'params': model('P', mask=(Annotated[MaskFile, File()], ...))},
                re.escape(
                    'P.mask: a file field has one File(...) marker, not '
                    "[File(kind='mask'), File(kind='raw')]"
                ),
            ),
            *(
                (
                    {'params': model('P', g=(Referenced[group], ...))},
                    f'P.g: {where}: {PATH_OUTPUT}',
                )
                for group, where in (
                    (
                        model('Held', data=(sc.Variable, ...), file=(RunFile, ...)),
                        'Held.file',
                    ),
                    (
                        model(
                            'Held',
                            data=(sc.Variable, ...),
                            files=(list[RunFile], []),
                        ),
                        'Held.files',
                    ),
                    (
                        model(
                            'Held',
                            data=(sc.Variable, ...),
                            inner=(model('Inner', file=(RunFile, ...)), ...),
                        ),
                        'Inner.file',
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where}: {PATH_OUTPUT}',
                )
                for fields, where in (
                    ({'raw': (RunFile, ...)}, 'O.raw'),
                    ({'raws': (list[RunFile | None], [])}, 'O.raws'),
                    (
                        {'g': (model('G', file=(RunFile, ...)), ...)},
                        'O.g: G.file',
                    ),
                    (
                        {
                            'g': (
                                model('G', h=(model('H', run=(SampleRun, ...)), ...)),
                                ...,
                            )
                        },
                        'O.g: H.run',
                    ),
                )
            ),
            *(
                (
                    {'outputs': model('O', **fields)},
                    f'{where}: {PATH_OUTPUT}',
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
                'P.reduction: the default_factory takes no other values',
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
                'P.r: R.twice: the default_factory takes no other values',
            ),
            (
                {
                    'params': model(
                        'P',
                        steps=(Annotated[list[TimeRange], AfterValidator(len)], ...),
                    ),
                    'tables': ('steps',),
                },
                'steps: a table carries only length constraints',
            ),
            (
                {
                    'params': model('P', steps=(list[TimeRange], Field(strict=True))),
                    'tables': ('steps',),
                },
                'steps: a table carries only length constraints',
            ),
            (
                {
                    'params': model(
                        'P', steps=(list[TimeRange], Field([], min_length=2))
                    )
                },
                rf'P.steps: {NOT_A_VALUE}: steps: List should have at least 2 items',
            ),
            *(
                (
                    {'params': model('P', r=(model('R', x=(float, excluded)), ...))},
                    'P.r: R.x: a field of a group has no exclude or exclude_if',
                )
                for excluded in (
                    Field(1.0, exclude=True),
                    Field(1.0, exclude_if=lambda x: x == 0),
                )
            ),
            (
                {'params': model('P', x=(float, Field(1.0, exclude=True)))},
                'P.x: a parameter has no exclude or exclude_if',
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
                f'P.loose: {NOT_A_VALUE}: loose: file a: the field is not a file field',
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
                rf'P.r: R.x: {NOT_A_VALUE}: x: .*not a reference to output: record: '
                'Field required',
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
                f'P.steps: {NOT_A_VALUE}: steps: Input should be a valid array',
            ),
            (
                {'params': model('P', runs=(list[list[SampleRun]], []))},
                f'P.runs: {LIST_OF_LISTS}',
            ),
            (
                {'params': model('P', runs=(list[SampleRun] | None, None))},
                f'P.runs: {OPTIONAL_LIST}',
            ),
            (
                {'params': model('P', runs=(list, []))},
                not_a_field_type('P.runs', 'list'),
            ),
            (
                {'params': model('P', sample=(SampleRun, Path('run.nxs')))},
                f'P.sample: {NOT_A_VALUE}: sample: {NOT_A_FILE}',
            ),
            (
                {'params': model('P', sample=(SampleRun, FileRef(file='run:1')))},
                f'P.sample: {NOT_A_VALUE}: sample: {NO_FILE_DEFAULT}',
            ),
            (
                {'params': model('P', runs=(list[SampleRun], [Path('run.nxs')]))},
                rf'P.runs: {NOT_A_VALUE}: runs\[0\]: {NOT_A_FILE}',
            ),
            (
                {
                    'params': model(
                        'P', measured=(Measured, Measured(run=Path('run.nxs')))
                    )
                },
                f'P.measured: {NOT_A_VALUE}: measured.run: {NOT_A_FILE}',
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
                        f'{where}: Referenced[...] marks a field of the params, not an '
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
            sample=(SampleRun, Path('a')),
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
            int | None,
            float | None,
            Mode | None,
            Literal['a', 'b'] | None,
            SampleRun,
            SampleRun | None,
            list[SampleRun],
            Reduction,
            Reduction | None,
            list[Reduction],
            By,
            By | None,
            list[By],
            model('Inner', group=(Reduction, ...), runs=(list[SampleRun], [])),
            Referenced[Reduction],
            Referenced[Reduction] | None,
            list[Referenced[Reduction]],
            Reduction | Referenced[Reduction],
            Referenced[BeamCentre],
            By | Referenced[BeamCentre],
        ],
    )
    def test_a_field_of_a_group_is_of_a_field_type(self, tp: Any) -> None:
        params = model('P', g=(_group_with(tp), ...))
        assert _takes_inline(spec(params=params), 'g')

    @pytest.mark.parametrize(
        ('tp', 'default'),
        [
            (Reduction, Reduction(bins=3)),
            (Reduction | None, None),
            (list[Reduction], []),
            (Mode, Mode.fast),
            (float | None, None),
            (SampleRun | None, None),
            (list[SampleRun], []),
            (Referenced[Reduction] | None, None),
        ],
    )
    def test_a_default_at_a_field_is_a_value(self, tp: Any, default: Any) -> None:
        group = model('G', value=(tp, default))
        spec(params=model('P', g=(group, ...)))

    @pytest.mark.parametrize(
        ('tp', 'default', 'reason'),
        [
            (
                Reduction,
                {'bins': 3},
                r"the default {'bins': 3} is not the value validating it gives",
            ),
            (SampleRun | None, Path('run.nxs'), NOT_A_FILE),
            (SampleRun | None, FileRef(file='run:1'), NO_FILE_DEFAULT),
            (list[SampleRun], [Path('run.nxs')], NOT_A_FILE),
            (
                Referenced[Reduction],
                Reduction(),
                r'is not a reference to an output, as the field is typed Referenced',
            ),
        ],
    )
    def test_a_default_at_a_field_that_is_not_a_value_is_refused(
        self, tp: Any, default: Any, reason: str
    ) -> None:
        group = model('G', value=(tp, default))
        with pytest.raises(ValueError, match=rf'P\.g: G\.value: .*{reason}'):
            spec(params=model('P', g=(group, ...)))

    @pytest.mark.parametrize(
        ('tp', 'name'),
        [
            (sc.DataArray, 'DataArray'),
            (list[sc.DataArray], 'DataArray'),
            (sc.Variable | sc.DataArray, 'Variable | DataArray'),
            (sc.DataArray | None, 'DataArray'),
            (Literal['a', None], "Literal['a', None]"),  # noqa: PYI061
            (list[Literal['a', None]], "Literal['a', None]"),  # noqa: PYI061
            (Tagged, 'Tagged'),
            (list[Tagged], 'Tagged'),
            (Literal[Tagged.a], "Literal[<Tagged.a: {'@ref': 'a'}>]"),
            (Literal[Mode.fast], "Literal[<Mode.fast: 'fast'>]"),
            (tuple[float, float], 'tuple[float, float]'),
            (dict[str, float], 'dict[str, float]'),
            (set[float], 'set[float]'),
            (Limits, 'Limits'),
            (Pair, 'Pair'),
            (RootModel[list[float]], 'RootModel[list[float]]'),
            (int | float, 'int | float'),
            (Any, 'Any'),
        ],
    )
    def test_any_other_field_is_refused_unless_a_reference_fills_its_group(
        self, tp: Any, name: str
    ) -> None:
        with pytest.raises(ValueError, match=not_a_field_type('P.g: G.value', name)):
            spec(params=model('P', g=(_group_with(tp), ...)))
        # The framework does not look inside the T of Referenced[T].
        referenced = model('P', g=(Referenced[_group_with(tp)], ...))
        assert not _takes_inline(spec(params=referenced), 'g')

    @pytest.mark.parametrize(
        ('tp', 'reason'),
        [
            (list[float] | None, OPTIONAL_LIST),
            (list[float | None], OPTIONAL_ELEMENT),
            (list[list[float]], LIST_OF_LISTS),
            (ByMonitor | ByTime, re.escape('a union of groups has a discriminator')),
        ],
    )
    def test_the_shape_rules_hold_at_a_field(self, tp: Any, reason: str) -> None:
        with pytest.raises(ValueError, match=rf'P\.g: G\.value: {reason}'):
            spec(params=model('P', g=(_group_with(tp), ...)))

    @pytest.mark.parametrize('tp', [BeamCentre, BeamCentre | None, list[BeamCentre]])
    def test_the_fields_of_a_group_at_a_field_are_checked_in_turn(
        self, tp: Any
    ) -> None:
        reason = not_a_field_type('P.g: G.value: BeamCentre.position', 'Variable')
        with pytest.raises(ValueError, match=reason):
            spec(params=model('P', g=(_group_with(tp), ...)))

    @pytest.mark.parametrize(
        'params',
        [
            lambda g: model('P', g=(Referenced[g], ...)),
            lambda g: model('P', g=(Referenced[g] | None, None)),
            lambda g: model('P', g=(list[Referenced[g]], [])),
        ],
    )
    def test_a_group_of_any_field_types_is_referenced_at_each_parameter_or_row(
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

    def test_a_table_may_be_of_files(self) -> None:
        assert FILES.tables == ('runs',)
        assert not _takes_inline(FILES, 'runs')

    def test_the_field_validators_of_a_group_check_each_row(self) -> None:
        assert _takes_inline(GEOMETRIES, 'geometry')
        with pytest.raises(pydantic.ValidationError, match='thickness must be'):
            GEOMETRIES.params(geometry=[{'thickness': -1.0}])

    def test_the_validators_of_the_element_type_check_each_row(self) -> None:
        def known(run: Path) -> Path:
            if run.name != 'run.nxs':
                raise ValueError('unknown run')
            return run

        params = model(
            'P',
            sample=(list[Annotated[SampleRun, AfterValidator(known)]], ...),
            can=(list[Annotated[CanRun, AfterValidator(known)]], ...),
        )
        runs = spec(params=params, tables=('sample', 'can'))
        known_rows = {'sample': [Path('run.nxs')], 'can': [Path('run.nxs')]}
        runs.params(**known_rows)
        for rows in ({'sample': [Path('other.nxs')]}, {'can': [Path('other.nxs')]}):
            with pytest.raises(pydantic.ValidationError, match='unknown run'):
                runs.params(**{**known_rows, **rows})
        assert runs.serialize().params_schema['properties']['sample']['items'] == (
            file_ref()
        )


def file_ref(metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    The schema of a file at a file field whose marker has ``metadata``, raw
    files by default: a reference to a file, with the metadata.
    """
    return {'$ref': '#/$defs/FileRef', 'metadata': metadata or {'kind': 'raw'}}


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
        assert defs['FileRef'] == FileRef.model_json_schema()
        assert '@ref' in defs['FileRef']['required']

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

    def test_a_file_field_or_a_referenced_group_is_a_reference(
        self,
    ) -> None:
        schema = IOFQ.serialize().params_schema
        properties = schema['properties']
        assert {n: properties[n] for n in ('sample', 'can', 'beam_centre')} == {
            'sample': {
                **file_ref(),
                'title': 'Sample run',
            },
            'can': {**file_ref(), 'title': 'Can'},
            'beam_centre': {
                'anyOf': [data_ref('BeamCentre'), {'type': 'null'}],
                'default': None,
                'title': 'Beam Centre',
            },
        }
        assert 'BeamCentre' not in schema['$defs']

    def test_a_file_field_is_a_reference_to_a_file_with_its_metadata(
        self,
    ) -> None:
        schema = FILES.serialize().params_schema
        properties = schema['properties']
        assert properties['mask']['anyOf'] == [
            file_ref({'kind': 'mask'}),
            {'type': 'null'},
        ]
        assert schema['$defs']['FileRef'] == FileRef.model_json_schema()
        assert properties['geometry'] == {
            **file_ref(),
            'title': 'Geometry',
        }
        assert properties['runs']['items'] == file_ref()

    def test_a_file_field_in_a_group_is_a_reference_to_a_file(self) -> None:
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
        assert properties['run'] == {**file_ref(), 'title': 'Run'}
        assert properties['masks']['items'] == file_ref({'kind': 'mask'})

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
            'items': file_ref(),
            'title': 'Sample run',
            'minItems': 1,
        }
        assert properties['can'] == {
            'type': 'array',
            'items': file_ref(),
            'title': 'Can',
            'minItems': 1,
        }
        assert serialized.tables == ('sample', 'can')
        assert serialized.params_schema['required'] == ['sample', 'can']

    def test_the_params_keep_their_docstring_examples_and_extra_schema(self) -> None:
        class P(BaseModel):
            """The params of a reduction."""

            width: float = Field(1.0, examples=[2.0], json_schema_extra={'unit': 'mm'})

        schema = spec(params=P).serialize().params_schema
        assert schema['description'] == 'The params of a reduction.'
        assert schema['properties']['width'] == {
            'default': 1.0,
            'examples': [2.0],
            'title': 'Width',
            'type': 'number',
            'unit': 'mm',
        }

    def test_groups_of_one_name_that_hold_references_keep_their_own_names(
        self,
    ) -> None:
        class Sample(BaseModel):
            class Row(BaseModel):
                sample: Referenced[Reduction]

        class Can(BaseModel):
            class Row(BaseModel):
                can: Referenced[Reduction]

        params = model('P', samples=(list[Sample.Row], []), cans=(list[Can.Row], []))
        properties = spec(params=params).serialize().params_schema['properties']
        samples, cans = (properties[n]['items']['$ref'] for n in ('samples', 'cans'))
        assert samples.endswith('Sample__Row')
        assert cans.endswith('Can__Row')

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
            {'sample': ref},  # a file field takes a reference to a file only
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

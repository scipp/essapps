# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The digest of a spec changes with the name, type, or requiredness of a field
of its params or outputs, at any depth, and with the ``extra`` setting of a
group, and with nothing else.

Each test places a group ``Centre``, or a variant of it, in a spec at one of
``PLACES``: a parameter, inside a parameter, and the same in the outputs.
"""

import uuid
from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any, Literal

import pytest
import scipp as sc
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    create_model,
    field_validator,
)

from ess.spec import MaskFile, Referenced, RunFile, WorkflowSpec


class Centre(BaseModel):
    x: float
    y: float = 0.0


class Outputs(BaseModel):
    n: int


ANY_TYPE = ConfigDict(arbitrary_types_allowed=True)


def _spec(
    params: dict[str, Any] | None = None, outputs: dict[str, Any] | None = None
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='Finds the centre.',
        params=create_model('Params', **(params or {})),
        outputs=create_model('Outputs', __config__=ANY_TYPE, **outputs)
        if outputs
        else Outputs,
    )


def _outer(group: type[BaseModel]) -> type[BaseModel]:
    return create_model('Outer', centre=(group, ...))


PLACES: dict[str, Callable[[type[BaseModel]], WorkflowSpec[Any, Any]]] = {
    'parameter': lambda g: _spec(params={'centre': (g, ...)}),
    'group in a parameter': lambda g: _spec(params={'outer': (_outer(g), ...)}),
    'list parameter': lambda g: _spec(params={'centres': (list[g], ...)}),  # type: ignore[valid-type]
    'reference': lambda g: _spec(params={'centre': (Referenced[g], ...)}),
    'output': lambda g: _spec(outputs={'centre': (g, ...)}),
    'group in an output': lambda g: _spec(outputs={'outer': (_outer(g), ...)}),
    'dict output': lambda g: _spec(outputs={'centres': (dict[str, g] | None, ...)}),  # type: ignore[valid-type]
}


class Described(BaseModel):
    """Another class, with titles, descriptions, defaults, a constraint, a
    validator, and the fields in another order."""

    y: float = Field(default=1.0, title='Y', description='Vertical, in mm.')
    x: Annotated[float, AfterValidator(abs)] = Field(gt=0, title='X')

    @field_validator('y')
    @classmethod
    def _finite(cls, value: float) -> float:
        return value


class Renamed(BaseModel):
    x_mm: float
    y: float = 0.0


class Retyped(BaseModel):
    x: int
    y: float = 0.0


class XOptional(BaseModel):
    x: float = 0.0
    y: float = 0.0


class YRequired(BaseModel):
    x: float
    y: float


class Added(BaseModel):
    x: float
    y: float = 0.0
    z: float = 1.0


class Removed(BaseModel):
    x: float


@pytest.mark.parametrize('place', PLACES)
def test_titles_descriptions_defaults_and_order_do_not_change_the_digest(
    place: str,
) -> None:
    assert PLACES[place](Described).digest == PLACES[place](Centre).digest


@pytest.mark.parametrize('place', PLACES)
@pytest.mark.parametrize(
    'variant', [Renamed, Retyped, XOptional, YRequired, Added, Removed]
)
def test_a_changed_field_changes_the_digest(
    place: str, variant: type[BaseModel]
) -> None:
    assert PLACES[place](variant).digest != PLACES[place](Centre).digest


class Unit(Enum):
    MM = 'mm'
    M = 'm'


class Metre(Enum):
    M = 'm'


class UnitRenamed(Enum):
    MILLIMETRE = 'mm'
    M = 'm'


class Given(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class Find(BaseModel):
    kind: Literal['find'] = 'find'


@pytest.mark.parametrize(
    ('a', 'b'),
    [
        (float, float | None),
        (Literal['a', 'b'], Literal['a']),
        (Unit, Metre),
        (Centre, Referenced[Centre]),
        (RunFile, MaskFile),
        (list[Centre], Centre),
    ],
)
def test_a_changed_type_changes_the_digest(a: Any, b: Any) -> None:
    assert _spec(params={'p': (a, ...)}).digest != _spec(params={'p': (b, ...)}).digest


@pytest.mark.parametrize(
    ('a', 'b'),
    [
        (Unit, UnitRenamed),
        (Unit, Literal['m', 'mm']),
        (Literal['mm', 'm'], Literal['m', 'mm']),
    ],
)
def test_an_enum_or_literal_of_the_same_values_keeps_the_digest(a: Any, b: Any) -> None:
    assert _spec(params={'p': (a, ...)}).digest == _spec(params={'p': (b, ...)}).digest


class Forbidding(Centre):
    model_config = ConfigDict(extra='forbid')


def test_the_extra_setting_of_a_group_changes_the_digest() -> None:
    assert PLACES['parameter'](Forbidding).digest != PLACES['parameter'](Centre).digest


class IntMasks(RootModel[dict[str, int]]):
    pass


class StrMasks(RootModel[dict[str, str]]):
    pass


def test_a_root_model_is_described_by_its_root_type() -> None:
    assert (
        _spec(outputs={'masks': (IntMasks, ...)}).digest
        != _spec(outputs={'masks': (StrMasks, ...)}).digest
    )


def test_a_class_that_is_not_a_group_is_described_by_its_module() -> None:
    here = type('Mask', (), {'__module__': 'here'})
    there = type('Mask', (), {'__module__': 'there'})
    assert (
        _spec(outputs={'mask': (here, ...)}).digest
        != _spec(outputs={'mask': (there, ...)}).digest
    )


def test_an_unresolved_type_has_no_digest() -> None:
    class Late(BaseModel):
        later: 'Later'  # type: ignore[name-defined]  # noqa: F821

    spec = _spec(outputs={'late': (Late, ...)})
    with pytest.raises(ValueError, match=r"^Late\.later: ForwardRef\('Later'\)"):
        _ = spec.digest


def test_the_order_of_the_members_of_a_union_does_not_change_the_digest() -> None:
    a = Annotated[Given | Find, Field(discriminator='kind')]
    b = Annotated[Find | Given, Field(discriminator='kind')]
    assert _spec(params={'p': (a, ...)}).digest == _spec(params={'p': (b, ...)}).digest


def test_a_field_moved_from_the_params_to_the_outputs_changes_the_digest() -> None:
    assert (
        _spec(params={'centre': (Centre, ...)}).digest
        != _spec(outputs={'centre': (Centre, ...), 'n': (int, ...)}).digest
    )


class Node(BaseModel):
    value: float
    children: list['Node']


def test_an_output_group_that_contains_itself_has_a_digest() -> None:
    assert _spec(outputs={'tree': (Node, ...)}).digest != _spec().digest


# Literals, so that a change of the digest's rules, or of what pydantic or Python
# report of a type, fails here rather than in a workspace's log. One per kind of
# description.
STABLE = {
    'scalar': (lambda: _spec(params={'p': (float, ...)}), 'dae9f48e5eab709f'),
    'group': (lambda: PLACES['parameter'](Centre), '9c81351d3a6df500'),
    'nested group': (
        lambda: PLACES['group in a parameter'](Centre),
        '5e679ffa780f085e',
    ),
    'union with None': (
        lambda: _spec(params={'p': (float | None, None)}),
        'a0fe99b0f1b0f016',
    ),
    'Literal': (
        lambda: _spec(params={'p': (Literal['a', 'b'], ...)}),
        'def1a06088db55d0',
    ),
    'enum': (lambda: _spec(params={'p': (Unit, ...)}), '32e94d99d33720a4'),
    'file field': (lambda: _spec(params={'p': (MaskFile, ...)}), 'bf81c990bc493a95'),
    'Referenced[T]': (lambda: PLACES['reference'](Centre), '8fd19f5a95e8a310'),
    'list': (lambda: PLACES['list parameter'](Centre), '1d5ed60276784d20'),
    'dict': (lambda: _spec(outputs={'o': (dict[str, float], ...)}), '016544ebacea2bae'),
    'scipp type': (
        lambda: _spec(outputs={'o': (sc.DataArray, ...)}),
        '8c37efc2323ffa18',
    ),
    'RootModel': (lambda: _spec(outputs={'o': (IntMasks, ...)}), '1cc5f4c89f907304'),
    'class by module': (
        lambda: _spec(outputs={'o': (uuid.UUID, ...)}),
        'b73ba80b6447254a',
    ),
}


@pytest.mark.parametrize('kind', STABLE)
def test_the_digest_is_the_same_in_every_process(kind: str) -> None:
    make, expected = STABLE[kind]
    assert make().digest == expected

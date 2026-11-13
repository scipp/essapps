# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The digest of a spec changes with the name, type, or requiredness of a field
of its params or outputs, at any depth, and with nothing else.

Each test places a group ``Centre``, or a variant of it, in a spec at one of
``PLACES``: a parameter, inside a parameter, and the same in the outputs.
"""

from collections.abc import Callable
from enum import Enum
from typing import Annotated, Any, Literal

import pytest
from pydantic import AfterValidator, BaseModel, Field, create_model, field_validator

from ess.spec import MaskFile, Referenced, RunFile, WorkflowSpec


class Centre(BaseModel):
    x: float
    y: float = 0.0


class Outputs(BaseModel):
    n: int


def _spec(
    params: dict[str, Any] | None = None, outputs: dict[str, Any] | None = None
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='Finds the centre.',
        params=create_model('Params', **(params or {})),
        outputs=create_model('Outputs', **outputs) if outputs else Outputs,
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


def test_the_digest_is_the_same_in_every_process() -> None:
    # A literal, so that a change of the digest's rules, or of what pydantic or
    # Python report of a type, fails here rather than in a workspace's log.
    assert PLACES['parameter'](Centre).digest == '9c81351d3a6df500'

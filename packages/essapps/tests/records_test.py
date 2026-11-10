# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Requests: what a request made without a client holds of its params."""

from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from ess.apps import Request, SubmitError
from ess.spec import OutputRef, Referenced, WorkflowSpec


class Amount(BaseModel):
    value: float


class Value(BaseModel):
    value: Amount


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=Value,
    )


class Step(BaseModel):
    size: float


class Scale(BaseModel):
    factor: float = 1.0


class WalkParams(BaseModel):
    scale: Scale = Field(default_factory=Scale)
    steps: list[Step] = Field(min_length=2)


WALK = _spec('walk', WalkParams)


class ByMonitor(BaseModel):
    mode: Literal['monitor'] = 'monitor'


class ByTime(BaseModel):
    mode: Literal['time'] = 'time'
    seconds: float = Field(gt=0)
    scale: float = 1.0


class NormalizedParams(BaseModel):
    """
    A union discriminated through ``Field``, and a list of unions
    discriminated through ``Annotated``.
    """

    normalization: ByMonitor | ByTime = Field(ByMonitor(), discriminator='mode')
    stops: list[Annotated[ByMonitor | ByTime, Field(discriminator='mode')]] = Field(
        default_factory=list
    )


NORMALIZED = _spec('normalized', NormalizedParams)


class AddParams(BaseModel):
    a: Referenced[Amount]
    b: Referenced[Amount]


ADD = _spec('add', AddParams)


def test_a_params_model_makes_the_request_of_its_dict() -> None:
    steps = [Step(size=1.0), Step(size=2.0)]
    by_model = Request(WALK, params=WalkParams(steps=steps))
    by_dict = Request(WALK, params={'steps': steps})
    scaled = Request(WALK, params=WalkParams(scale=Scale(factor=2.0), steps=steps))

    assert by_model == by_dict  # defaults left to the workspace's spec
    assert scaled.params['scale'] == {'factor': 2.0}


def test_other_defaults_are_left_out_beside_a_discriminator() -> None:
    request = Request(
        NORMALIZED, params=NormalizedParams(normalization=ByTime(seconds=1.0))
    )

    assert request.params == {'normalization': {'mode': 'time', 'seconds': 1.0}}


TIMED = {'mode': 'time', 'seconds': 1.0, 'scale': 2.0}


@pytest.mark.parametrize(
    ('params', 'kept'),
    [
        # a union parameter, in a params model or in a dict
        (
            NormalizedParams(normalization=ByMonitor()),
            {'normalization': {'mode': 'monitor'}},
        ),
        ({'normalization': ByMonitor()}, {'normalization': {'mode': 'monitor'}}),
        (
            NormalizedParams(normalization=ByTime(seconds=1.0, scale=2.0)),
            {'normalization': TIMED},
        ),
        ({'normalization': ByTime(seconds=1.0, scale=2.0)}, {'normalization': TIMED}),
        # a list of unions
        (
            NormalizedParams(stops=[ByTime(seconds=1.0), ByMonitor()]),
            {'stops': [{'mode': 'time', 'seconds': 1.0}, {'mode': 'monitor'}]},
        ),
    ],
)
def test_a_discriminator_is_kept_though_it_is_a_default(
    params: NormalizedParams | dict[str, Any], kept: dict[str, Any]
) -> None:
    assert Request(NORMALIZED, params=params).params == kept


def test_upstream_lists_each_reference_in_the_order_of_the_params() -> None:
    first, second = (OutputRef(record=r, output='value') for r in ('r1', 'r2'))
    request = Request(ADD, params={'a': second, 'b': first})
    twice = Request(ADD, params={'a': first, 'b': first})

    assert request.upstream() == [second, first]
    assert twice.upstream() == [first, first]


def test_a_reference_of_an_unknown_kind_is_refused() -> None:
    unknown = {'@ref': 'record', 'record': 'r1', 'output': 'value'}

    with pytest.raises(
        SubmitError, match=r"^normalization: .*'record' is not a kind of reference"
    ) as raised:
        Request(NORMALIZED, params={'normalization': unknown, 'stops': [unknown]})
    assert [e.loc for e in raised.value.errors] == [('normalization',)]

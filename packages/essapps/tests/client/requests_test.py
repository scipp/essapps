# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Submitting requests: what is refused, what a record holds of its request."""

from typing import Annotated, Literal, Self

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.apps import (
    Backend,
    Client,
    Request,
    SpecId,
    SubmitError,
    Template,
    file,
    local,
)
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows
from ess.spec import Binding, function_binding

from .conftest import (
    LOAD,
    SCALE,
    WALK,
    Amount,
    PositiveScale,
    Step,
    StepsParams,
    Value,
    WalkParams,
    _load,
    _spec,
    _value,
    load_run,
)


class Unchecked(StepsParams):
    """Its model validator always raises, which the framework never calls."""

    @model_validator(mode='after')
    def _raises(self) -> Self:
        raise ValueError('a model validator was called')


UNCHECKED_PLAIN = _spec('unchecked-plain', params=Unchecked, tables=('steps',))


UNCHECKED_STAGED = _spec('unchecked-staged', params=Unchecked, tables=('steps',))


UNCHECKED_ROWS = _spec('unchecked-rows', params=Unchecked, tables=('steps',))


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


NORMALIZED = _spec('normalized', params=NormalizedParams)


def test_a_params_model_makes_the_request_of_its_dict() -> None:
    steps = [Step(size=1.0), Step(size=2.0)]
    by_model = Request(WALK, params=WalkParams(steps=steps))
    by_dict = Request(WALK, params={'steps': steps})
    scaled = Request(
        WALK, params=WalkParams(scale=PositiveScale(factor=2.0), steps=steps)
    )

    assert by_model == by_dict  # defaults left to the backend's spec
    assert scaled.params['scale'] == {'factor': 2.0}


def test_other_defaults_are_left_out_beside_a_discriminator() -> None:
    request = Request(
        NORMALIZED, params=NormalizedParams(normalization=ByTime(seconds=1.0))
    )

    assert request.params == {'normalization': {'mode': 'time', 'seconds': 1.0}}


def test_a_discriminator_is_kept_though_it_is_a_default(
    files: FakeFiles,
) -> None:
    def describe(params: NormalizedParams) -> Value:
        by = params.normalization
        return _value(by.seconds if isinstance(by, ByTime) else -1.0)

    with local(
        proposal='p1', files=files, bind=[function_binding(NORMALIZED, describe)]
    ) as client:
        timed = client.compute(
            NORMALIZED, params={'normalization': ByTime(seconds=2.0)}
        )
        as_dict = client.compute(
            NORMALIZED, params={'normalization': {'mode': 'time', 'seconds': 2}}
        )

        assert client.output(timed, 'value') == Amount(value=2.0)
        assert client.output(as_dict, 'value') == Amount(value=2.0)


def test_a_discriminator_in_a_list_of_unions_is_kept() -> None:
    stops = [ByTime(seconds=1.0), ByMonitor()]
    request = Request(NORMALIZED, params=NormalizedParams(stops=stops))

    assert request.params['stops'] == [
        {'mode': 'time', 'seconds': 1.0},
        {'mode': 'monitor'},
    ]


@pytest.mark.parametrize('by', [ByMonitor(), ByTime(seconds=1.0, scale=2.0)])
def test_a_discriminator_of_a_union_parameter_is_kept(by: ByMonitor | ByTime) -> None:
    by_model = Request(NORMALIZED, params=NormalizedParams(normalization=by))
    by_dict = Request(NORMALIZED, params={'normalization': by})

    assert by_model.params == by_dict.params == {'normalization': by.model_dump()}


def test_a_group_with_a_field_its_model_lacks_is_refused(client: Client) -> None:
    walk = client.accumulator(Template(WALK, blanks=('steps',)))

    with pytest.raises(SubmitError, match=r'scaling\.speed: Extra inputs are not'):
        client.submit(
            SCALE,
            params={
                'scaling': {'factor': 2.0, 'speed': 2.0},
                'sample': file(run=1),
            },
        )
    with pytest.raises(SubmitError, match=r'steps\[0\]\.speed: Extra inputs are not'):
        walk.push(steps=[{'size': 1.0, 'speed': 2.0}])


def test_the_framework_never_calls_a_model_validator(files: FakeFiles) -> None:
    def sizes(params: StepsParams) -> Value:
        return _value(params.scale.factor * sum(s.size for s in params.steps))

    bind = [
        function_binding(UNCHECKED_PLAIN, sizes),
        StagedFunction(UNCHECKED_STAGED, sizes),
        kept_rows(UNCHECKED_ROWS, sizes),
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        plain = client.compute(UNCHECKED_PLAIN, params={'steps': [Step(size=1.0)]})
        stage = client.stage(
            Template(
                UNCHECKED_STAGED, params={'steps': [Step(size=2.0)]}, blanks=('scale',)
            )
        )
        staged = client.compute(stage, params={'scale': {'factor': 2.0}})
        rows = client.accumulator(Template(UNCHECKED_ROWS, blanks=('steps',)))
        rows.push(steps=[Step(size=3.0)])

        assert client.output(plain, 'value') == Amount(value=1.0)
        assert client.output(staged, 'value') == Amount(value=4.0)
        assert client.output(rows, 'value') == Amount(value=3.0)


def test_bind_is_a_list_of_bindings(files: FakeFiles) -> None:
    for bind in ({LOAD: load_run}, [load_run]):
        with pytest.raises(TypeError, match='bind is a list of bindings'):
            Backend(files, bind)  # type: ignore[arg-type]


def test_values_go_with_a_spec_or_a_stage(client: Client) -> None:
    with pytest.raises(TypeError, match='params go with a spec or a stage'):
        client.submit(_load(1), params={'sample': file(run=2)})


def test_a_client_gives_the_specs_the_backend_runs(
    client: Client, bindings: list[Binding]
) -> None:
    load = client.compute(_load(1))

    assert client.spec(load.spec) is LOAD
    assert client.specs() == [b.spec for b in bindings]
    with pytest.raises(KeyError, match='unknown spec load/v2'):
        client.spec(SpecId(name='load', version=2))

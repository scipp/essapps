# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Requests through a client: model validators, bindings, and specs."""

from typing import Self

import pytest
from pydantic import model_validator

from ess.apps import (
    Client,
    SpecId,
    Template,
    file,
    local,
)
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows
from ess.spec import Binding, function_binding

from .conftest import (
    LOAD,
    Amount,
    Step,
    StepsParams,
    Value,
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
            local(proposal='p1', files=files, bind=bind)  # type: ignore[arg-type]


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

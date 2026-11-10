# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""The backend's checks that no client can reach."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ess.apps import Backend, Request, SubmitError, Template, file
from ess.apps.protocol import BackendProtocol, Entry
from ess.apps.testing import FakeFiles, StagedFunction, read_file
from ess.spec import RunFile, WorkflowSpec, function_binding


class Amount(BaseModel):
    value: float


class Value(BaseModel):
    value: Amount


class Sample(BaseModel):
    sample: RunFile


class Factor(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    scaling: Factor = Field(default_factory=Factor)
    sample: RunFile


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=Value,
    )


LOAD = _spec('load', Sample)


SCALE = _spec('scale', ScaleParams)


def load(params: Sample) -> Value:
    return Value(value=Amount(value=read_file(params.sample)))


def scale(params: ScaleParams) -> Value:
    return Value(value=Amount(value=read_file(params.sample) * params.scaling.factor))


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[Backend]:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    for n in (1, 2):
        files.measure(n, float(n))
    backend = Backend(
        proposal='p1',
        files=files,
        bind=[function_binding(LOAD, load), StagedFunction(SCALE, scale)],
    )
    yield backend
    backend.close()


def test_a_request_through_a_stage_must_have_the_stage_s_values(
    backend: Backend,
) -> None:
    stage, template = backend.open_stage(
        Template(SCALE, params={'sample': file(run=1)}, blanks=('scaling',))
    )
    twice = {'scaling': {'factor': 2.0}}
    refused = {
        'the stage holds scale': Request(LOAD, params={'sample': file(run=1)}),
        r"\['sample'\]: differ from the stage's values": Request(
            SCALE, params={**twice, 'sample': file(run=2)}
        ),
    }
    for reason, request in refused.items():
        with pytest.raises(SubmitError, match=reason):
            backend.submit([Entry(request, stage=stage)], 'anna')

    (record,) = backend.submit(
        [Entry(template.fill(params=twice), stage=stage)], 'anna'
    )
    assert record.request.params['sample'] == template.params['sample']


def test_the_backend_offers_only_the_protocol_and_its_own_close() -> None:
    def public(cls: type) -> set[str]:
        return {name for name in vars(cls) if not name.startswith('_')}

    assert public(Backend) == public(BackendProtocol) | {'close'}


def test_a_refused_request_is_named_by_its_index(backend: Backend) -> None:
    accepted = Request(LOAD, params={'sample': file(run=1)})

    with pytest.raises(SubmitError) as raised:
        backend.submit([Entry(accepted), Entry(Request(LOAD))], 'anna')

    assert {error.entry for error in raised.value.errors} == {1}

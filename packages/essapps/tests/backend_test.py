# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""The backend's checks that no client can reach."""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field

from ess.apps import Backend, Request, SubmitError, Template, file
from ess.apps.backend import Entry
from ess.apps.testing import FakeFiles, StagedFunction, read_file
from ess.spec import File, WorkflowSpec, function_binding

SampleRun = Annotated[Path, File()]


class Amount(BaseModel):
    value: float


class Value(BaseModel):
    value: Amount


class Sample(BaseModel):
    sample: SampleRun


class Factor(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    scaling: Factor = Field(default_factory=Factor)
    sample: SampleRun


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
        files, [function_binding(LOAD, load), StagedFunction(SCALE, scale)]
    )
    yield backend
    backend.close()


def test_a_request_through_a_stage_must_have_the_stage_s_values(
    backend: Backend,
) -> None:
    client = backend.open_client('p1', 'anna')
    stage, template = backend.open_stage(
        Template(SCALE, params={'sample': file(run=1)}, blanks=('scaling',)),
        client=client,
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
            backend.submit([Entry(request, stage=stage)], client=client)

    (record,) = backend.submit(
        [Entry(template.fill(params=twice), stage=stage)], client=client
    )
    assert record.request.params['sample'] == template.params['sample']

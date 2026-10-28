# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A file model at a field of a group: given, checked, and read."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.apps import Backend, Client, SubmitError, Template, file
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows, read_file
from ess.spec import FileModel, WorkflowSpec, function_binding


class SampleRun(FileModel): ...


class MaskFile(FileModel, kind='mask'): ...


class Sample(BaseModel):
    run: SampleRun
    thickness: float = Field(1.0, gt=0)
    mask: MaskFile | None = None


class Channels(BaseModel):
    up: SampleRun
    down: SampleRun

    @model_validator(mode='after')
    def _distinct(self) -> Self:
        if self.up == self.down:
            raise ValueError('the channels share a run')
        return self


class Checked(BaseModel):
    run: SampleRun

    @model_validator(mode='after')
    def _suffix(self) -> Self:
        if self.run.path.suffix != '.json':
            raise ValueError('not JSON')
        return self


class CheckedParams(BaseModel):
    checked: Checked


class Factor(BaseModel):
    factor: float = 1.0


class SampleParams(BaseModel):
    sample: Sample
    scale: Factor = Factor()


class TableParams(BaseModel):
    samples: list[Sample]


class ChannelParams(BaseModel):
    channels: Channels


class Total(BaseModel):
    value: float


class Outputs(BaseModel):
    total: Total


def _spec(name: str, params: type[BaseModel], **kwargs: Any) -> Any:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=Outputs,
        **kwargs,
    )


SAMPLE = _spec('sample', SampleParams)
TABLE = _spec('table', TableParams, tables=('samples',))
CHANNELS = _spec('channels', ChannelParams)
CHECKED = _spec('checked', CheckedParams)


def _weighed(sample: Sample) -> float:
    masked = 0.0 if sample.mask is None else read_file(sample.mask.path)
    return (read_file(sample.run.path) - masked) * sample.thickness


def _sample(params: SampleParams) -> Outputs:
    return Outputs(total=Total(value=_weighed(params.sample) * params.scale.factor))


def _table(params: TableParams) -> Outputs:
    return Outputs(total=Total(value=sum(map(_weighed, params.samples))))


def _channels(params: ChannelParams) -> Outputs:
    up, down = (read_file(r.path) for r in (params.channels.up, params.channels.down))
    return Outputs(total=Total(value=up - down))


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    files.measure(1, 10.0)
    files.measure(2, 20.0)
    files.measure(3, 1.0, kind='mask')
    return files


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    bindings = [
        StagedFunction(SAMPLE, _sample),
        kept_rows(TABLE, _table),
        function_binding(CHANNELS, _channels),
        function_binding(CHECKED, lambda p: Outputs(total=Total(value=0.0))),
    ]
    backend = Backend(files, bindings)
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def _total(client: Client, record: Any) -> float:
    client.wait(record)
    return client.output(record, 'total').value


def test_a_file_at_a_field_reaches_the_workflow_with_its_local_path(
    client: Client, files: FakeFiles
) -> None:
    params = {'sample': {'run': file(run=1), 'thickness': 2.0, 'mask': None}}
    record = client.submit(SAMPLE, params=params)

    assert _total(client, record) == 20.0
    held = record.request.params['sample']
    assert held['run'] == files.resolve(file(run=1))
    assert held['thickness'] == 2.0


def test_an_optional_file_at_a_field_is_read_when_given(client: Client) -> None:
    sample = {'run': file(run=2), 'mask': file(run=3)}
    assert _total(client, client.submit(SAMPLE, params={'sample': sample})) == 19.0


def test_a_value_next_to_a_file_is_checked_at_submit(client: Client) -> None:
    sample = {'run': file(run=1), 'thickness': -1.0}
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': sample})
    (error,) = raised.value.errors
    assert error.loc == ('sample', 'thickness')
    assert 'greater than 0' in error.msg


@pytest.mark.parametrize(
    ('sample', 'loc', 'msg'),
    [
        ({'run': file(run=9)}, ('sample', 'run'), 'unknown file run:9'),
        (
            {'run': file(run=1), 'mask': file(run=2)},
            ('sample', 'mask'),
            "does not fit MaskFile, which takes files with {'kind': 'mask'}",
        ),
        (
            {'run': '/data/run-1.json'},
            ('sample', 'run'),
            "'/data/run-1.json' is not a file; SampleRun is given by reference",
        ),
    ],
)
def test_a_file_at_a_field_is_checked_as_one_at_a_parameter(
    client: Client, sample: dict[str, Any], loc: tuple[Any, ...], msg: str
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': sample})
    (error,) = raised.value.errors
    assert error.loc == loc
    assert msg in error.msg


def test_a_reference_to_an_output_at_a_file_field_is_refused(
    client: Client,
) -> None:
    record = client.submit(SAMPLE, params={'sample': {'run': file(run=1)}})
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': {'run': record.ref('total')}})
    (error,) = raised.value.errors
    assert error.loc == ('sample', 'run')
    assert 'is not a file; SampleRun is given by reference to a file' in error.msg


def test_a_check_across_files_runs_at_submit_on_their_identities(
    client: Client, files: FakeFiles
) -> None:
    identity = files.resolve(file(run=1))
    channels = {'up': file(run=1), 'down': identity}
    with pytest.raises(SubmitError, match='the channels share a run'):
        client.submit(CHANNELS, params={'channels': channels})

    channels = {'up': file(run=2), 'down': identity}
    assert _total(client, client.submit(CHANNELS, params={'channels': channels})) == 10


def test_a_table_holds_rows_each_with_its_file(client: Client) -> None:
    samples = [
        {'run': file(run=1), 'thickness': 1.0},
        {'run': file(run=2), 'thickness': 2.0},
    ]
    assert _total(client, client.submit(TABLE, params={'samples': samples})) == 50.0


def test_rows_naming_one_file_by_two_names_are_one_row(
    client: Client, files: FakeFiles
) -> None:
    samples = [
        {'run': file(run=1)},
        {'run': files.resolve(file(run=1))},
    ]
    with pytest.raises(SubmitError) as raised:
        client.submit(TABLE, params={'samples': samples})
    (error,) = raised.value.errors
    assert error.loc == ('samples', 1)
    assert error.msg.startswith('repeats samples[0]')


def test_a_stage_fixes_a_group_holding_a_file(client: Client) -> None:
    template = Template(
        SAMPLE,
        params={'sample': {'run': file(run=1), 'thickness': 2.0}},
        blanks=('scale',),
    )
    stage = client.stage(template)
    record = client.submit(stage, params={'scale': {'factor': 3.0}})
    assert _total(client, record) == 60.0


def test_an_accumulator_takes_rows_each_with_its_file(client: Client) -> None:
    accumulator = client.accumulator(Template(TABLE, blanks=('samples',)))
    accumulator.push(samples=[{'run': file(run=1), 'thickness': 1.0}])
    accumulator.push(samples=[{'run': file(run=2), 'thickness': 2.0}])
    assert client.output(accumulator, 'total') == Total(value=50.0)


def test_a_validator_that_reads_a_local_path_refuses_the_request_at_submit(
    client: Client,
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(CHECKED, params={'checked': {'run': file(run=1)}})
    (error,) = raised.value.errors
    assert error.loc == ('checked',)
    assert 'a file has a local path only when the request runs' in error.msg

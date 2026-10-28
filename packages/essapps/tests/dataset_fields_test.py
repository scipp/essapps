# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A dataset model at a field of a group: given, checked, and read."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.apps import Backend, Client, SubmitError, Template, dataset
from ess.apps.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import DatasetModel, WorkflowSpec, function_binding


class SampleRun(DatasetModel): ...


class MaskFile(DatasetModel, kind='mask'): ...


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


def _weighed(sample: Sample) -> float:
    masked = 0.0 if sample.mask is None else read_dataset(sample.mask.path)
    return (read_dataset(sample.run.path) - masked) * sample.thickness


def _sample(params: SampleParams) -> Outputs:
    return Outputs(total=Total(value=_weighed(params.sample) * params.scale.factor))


def _table(params: TableParams) -> Outputs:
    return Outputs(total=Total(value=sum(map(_weighed, params.samples))))


def _channels(params: ChannelParams) -> Outputs:
    up, down = (
        read_dataset(r.path) for r in (params.channels.up, params.channels.down)
    )
    return Outputs(total=Total(value=up - down))


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    datasets.measure(1, 10.0)
    datasets.measure(2, 20.0)
    datasets.measure(3, 1.0, kind='mask')
    return datasets


@pytest.fixture
def client(datasets: FakeDatasets) -> Iterator[Client]:
    bindings = [
        StagedFunction(SAMPLE, _sample),
        kept_rows(TABLE, _table),
        function_binding(CHANNELS, _channels),
    ]
    backend = Backend(datasets, bindings)
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def _total(client: Client, record: Any) -> float:
    client.wait(record)
    return client.output(record, 'total').value


def test_a_dataset_at_a_field_reaches_the_workflow_with_its_local_path(
    client: Client, datasets: FakeDatasets
) -> None:
    params = {'sample': {'run': dataset(run=1), 'thickness': 2.0, 'mask': None}}
    record = client.submit(SAMPLE, params=params)

    assert _total(client, record) == 20.0
    held = record.request.params['sample']
    assert held['run'] == datasets.resolve(dataset(run=1))
    assert held['thickness'] == 2.0


def test_an_optional_dataset_at_a_field_is_read_when_given(client: Client) -> None:
    sample = {'run': dataset(run=2), 'mask': dataset(run=3)}
    assert _total(client, client.submit(SAMPLE, params={'sample': sample})) == 19.0


def test_a_value_next_to_a_dataset_is_checked_at_submit(client: Client) -> None:
    sample = {'run': dataset(run=1), 'thickness': -1.0}
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': sample})
    (error,) = raised.value.errors
    assert error.loc == ('sample', 'thickness')
    assert 'greater than 0' in error.msg


@pytest.mark.parametrize(
    ('sample', 'loc', 'msg'),
    [
        ({'run': dataset(run=9)}, ('sample', 'run'), 'unknown dataset run:9'),
        (
            {'run': dataset(run=1), 'mask': dataset(run=2)},
            ('sample', 'mask'),
            "does not fit MaskFile, which takes datasets with {'kind': 'mask'}",
        ),
        (
            {'run': '/data/run-1.json'},
            ('sample', 'run'),
            "'/data/run-1.json' is not a dataset; SampleRun is given by reference",
        ),
    ],
)
def test_a_dataset_at_a_field_is_checked_as_one_at_a_parameter(
    client: Client, sample: dict[str, Any], loc: tuple[Any, ...], msg: str
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': sample})
    (error,) = raised.value.errors
    assert error.loc == loc
    assert msg in error.msg


def test_a_reference_to_an_output_at_a_dataset_field_is_refused(
    client: Client,
) -> None:
    record = client.submit(SAMPLE, params={'sample': {'run': dataset(run=1)}})
    with pytest.raises(SubmitError) as raised:
        client.submit(SAMPLE, params={'sample': {'run': record.ref('total')}})
    (error,) = raised.value.errors
    assert error.loc == ('sample', 'run')
    assert 'is not a dataset; SampleRun is given by reference to a dataset' in error.msg


def test_a_check_across_datasets_runs_at_submit_on_their_identities(
    client: Client, datasets: FakeDatasets
) -> None:
    identity = datasets.resolve(dataset(run=1))
    channels = {'up': dataset(run=1), 'down': identity}
    with pytest.raises(SubmitError, match='the channels share a run'):
        client.submit(CHANNELS, params={'channels': channels})

    channels = {'up': dataset(run=2), 'down': identity}
    assert _total(client, client.submit(CHANNELS, params={'channels': channels})) == 10


def test_a_table_holds_rows_each_with_its_dataset(client: Client) -> None:
    samples = [
        {'run': dataset(run=1), 'thickness': 1.0},
        {'run': dataset(run=2), 'thickness': 2.0},
    ]
    assert _total(client, client.submit(TABLE, params={'samples': samples})) == 50.0


def test_rows_naming_one_dataset_by_two_names_are_one_row(
    client: Client, datasets: FakeDatasets
) -> None:
    samples = [
        {'run': dataset(run=1)},
        {'run': datasets.resolve(dataset(run=1))},
    ]
    with pytest.raises(SubmitError) as raised:
        client.submit(TABLE, params={'samples': samples})
    (error,) = raised.value.errors
    assert error.loc == ('samples', 1)
    assert error.msg.startswith('repeats samples[0]')


def test_a_stage_fixes_a_group_holding_a_dataset(client: Client) -> None:
    template = Template(
        SAMPLE,
        params={'sample': {'run': dataset(run=1), 'thickness': 2.0}},
        blanks=('scale',),
    )
    stage = client.stage(template)
    record = client.submit(stage, params={'scale': {'factor': 3.0}})
    assert _total(client, record) == 60.0


def test_an_accumulator_takes_rows_each_with_its_dataset(client: Client) -> None:
    accumulator = client.accumulator(Template(TABLE, blanks=('samples',)))
    accumulator.push(samples=[{'run': dataset(run=1), 'thickness': 1.0}])
    accumulator.push(samples=[{'run': dataset(run=2), 'thickness': 2.0}])
    assert client.output(accumulator, 'total') == Total(value=50.0)

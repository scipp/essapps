# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What an output is, without its data."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import (
    ArrayInfo,
    Backend,
    Client,
    NotReadable,
    RecordFailed,
)
from ess.apps.testing import FakeFiles, FakeStore
from ess.spec import WorkflowSpec, function_binding


class Settings(BaseModel):
    scale: float = 1.0


class Image(BaseModel):
    """Counts per pixel, their total, the scale they used, and that scale's group."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    counts: sc.DataArray
    total: sc.Variable
    scale: float
    used: Settings


class Params(BaseModel):
    settings: Settings = Field(default_factory=Settings)


class Outputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    image: Image
    profile: sc.DataArray
    mean: sc.Variable
    note: Settings
    extra: Image | None = None


IMAGE = WorkflowSpec(
    name='image',
    version=1,
    title='Image',
    description='A small image, waiting for the gate.',
    params=Params,
    outputs=Outputs,
)


class Held(BaseModel):
    """Arrays inside a dict, a list, and a group, as ``PixelMasks`` holds its masks."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    masks: dict[str, sc.Variable]
    parts: list[sc.Variable]
    image: Image


class HeldOutputs(BaseModel):
    held: Held


HELD = WorkflowSpec(
    name='held',
    version=1,
    title='Held',
    description='Arrays inside the fields of a group.',
    params=Params,
    outputs=HeldOutputs,
)


def held(params: Params) -> HeldOutputs:
    return HeldOutputs(
        held=Held(
            masks={'a': sc.zeros(dims=['y', 'x'], shape=[2, 3], dtype=bool)},
            parts=[sc.zeros(dims=['t'], shape=[4], unit='s')],
            image=Image(
                counts=sc.DataArray(sc.zeros(dims=['y'], shape=[5])),
                total=sc.scalar(1.0),
                scale=2.0,
                used=Settings(scale=2.0),
            ),
        )
    )


@pytest.fixture
def gate() -> threading.Event:
    opened = threading.Event()
    opened.set()
    return opened


@pytest.fixture
def backend(tmp_path: Path, gate: threading.Event) -> Iterator[Backend]:
    def image(params: Params) -> Outputs:
        gate.wait()
        scale = params.settings.scale
        if scale == 0:
            raise ZeroDivisionError('scale 0')
        counts = sc.DataArray(
            sc.ones(dims=['y', 'x'], shape=[2, 3], unit='counts'),
            coords={'x': sc.arange('x', 3.0, unit='m'), 'y': sc.arange('y', 2.0)},
        )
        return Outputs(
            image=Image(
                counts=counts,
                total=counts.data.sum(),
                scale=scale,
                used=params.settings,
            ),
            profile=counts['y', 0].copy(),
            mean=sc.scalar(1.5, unit='m'),
            note=params.settings,
        )

    backend = Backend(
        FakeFiles('p1', tmp_path),
        [function_binding(IMAGE, image), function_binding(HELD, held)],
        store=FakeStore(),
    )
    yield backend
    gate.set()  # a test that failed may leave a record pending
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def test_a_group_is_its_arrays_by_what_they_are_and_its_other_fields_by_value(
    client: Client,
) -> None:
    record = client.compute(IMAGE, params={'settings': {'scale': 2.0}})

    described = client.describe(record, 'image')

    assert list(described) == ['counts', 'total', 'scale', 'used']
    assert described['counts'] == ArrayInfo(
        sizes={'y': 2, 'x': 3}, unit='counts', coords=('x', 'y')
    )
    assert sc.identical(described['total'], sc.scalar(6.0, unit='counts'))
    assert described['scale'] == 2.0
    assert described['used'] == {'scale': 2.0}


def test_an_output_that_is_an_array_is_described_by_its_name(client: Client) -> None:
    record = client.compute(IMAGE)

    assert client.describe(record, 'profile') == {
        'profile': ArrayInfo(sizes={'x': 3}, unit='counts', coords=('x', 'y'))
    }


def test_a_0d_array_output_is_given_as_it_is(client: Client) -> None:
    record = client.compute(IMAGE)

    described = client.describe(record, 'mean')

    assert list(described) == ['mean']
    assert sc.identical(described['mean'], sc.scalar(1.5, unit='m'))


def test_a_group_without_an_array_is_its_fields(client: Client) -> None:
    record = client.compute(IMAGE, params={'settings': {'scale': 3.0}})

    assert client.describe(record, 'note') == {'scale': 3.0}


def test_what_is_described_shares_nothing_with_the_value(client: Client) -> None:
    record = client.compute(IMAGE)
    described = client.describe(record, 'image')

    described['total'] += sc.scalar(1.0, unit='counts')
    described['used']['scale'] = 9.0
    described['counts'].sizes['x'] = 99  # type: ignore[index]
    with pytest.raises(AttributeError):
        described['counts'].unit = 'm'  # type: ignore[misc]
    mean = client.describe(record, 'mean')['mean']
    mean += sc.scalar(1.0, unit='m')

    value = client.output(record, 'image')
    assert value.total.value == 6.0
    assert value.used.scale == 1.0
    assert client.describe(record, 'image')['counts'].sizes == {'y': 2, 'x': 3}
    assert client.output(record, 'mean').value == 1.5


def test_an_optional_output_the_workflow_did_not_return_is_not_readable(
    client: Client,
) -> None:
    record = client.compute(IMAGE)

    with pytest.raises(NotReadable, match='did not return it'):
        client.describe(record, 'extra')
    with pytest.raises(NotReadable, match='did not return it'):
        client.output(record, 'extra')


def test_a_failed_record_raises_as_reading_does(client: Client) -> None:
    record = client.compute(IMAGE, params={'settings': {'scale': 0.0}})

    with pytest.raises(RecordFailed, match='ZeroDivisionError') as raised:
        client.describe(record, 'image')
    assert raised.value.reason == client.failure(record)


def test_a_pending_record_is_waited_for(client: Client, gate: threading.Event) -> None:
    gate.clear()
    record = client.submit(IMAGE)
    timer = threading.Timer(0.1, gate.set)
    timer.start()
    try:
        assert client.describe(record, 'image')['scale'] == 1.0
    finally:
        timer.cancel()
        gate.set()


def test_a_value_released_by_the_client_is_not_readable(client: Client) -> None:
    record = client.compute(IMAGE)
    client.release(record)

    with pytest.raises(NotReadable):
        client.describe(record, 'image')


def test_a_value_another_client_keeps_is_not_readable_until_it_is_persisted(
    backend: Backend, client: Client
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    record = other.compute(IMAGE)

    with pytest.raises(NotReadable):
        client.describe(record, 'image')

    other.persist(record, 'image')
    other.release(record)
    assert client.describe(record, 'image')['counts'].sizes == {'y': 2, 'x': 3}


def test_an_array_inside_a_field_is_described_and_not_copied(client: Client) -> None:
    record = client.compute(HELD)

    described = client.describe(record, 'held')

    assert described['masks'] == {
        'a': ArrayInfo(sizes={'y': 2, 'x': 3}, unit=None, coords=())
    }
    assert described['parts'] == [ArrayInfo(sizes={'t': 4}, unit='s', coords=())]
    assert described['image']['counts'] == ArrayInfo(
        sizes={'y': 5}, unit='dimensionless', coords=()
    )
    assert described['image']['used'] == {'scale': 2.0}

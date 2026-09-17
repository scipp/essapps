# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a warm workflow reuses (D8)."""

from typing import Any, NewType

import pytest
import sciline
import scipp as sc

from ess.apps.client import Client
from ess.apps.examples import (
    HISTOGRAM,
    LOAD,
    Bins,
    Filtered,
    Histogram,
    HistogramParams,
    RawData,
    Threshold,
    filter_data,
    histogram,
    histogram_workflow,
)
from ess.apps.spec import OutputRef
from ess.apps.testing import assert_warm_equals_cold
from ess.apps.warm import WarmPipeline

DATA_REF = OutputRef(record='raw', output='data')
DOUBLED_REF = OutputRef(record='raw', output='doubled')


def raw() -> sc.DataArray:
    return sc.DataArray(
        sc.array(dims=['x'], values=[1.0, 5.0, 2.0, 6.0], unit='counts'),
        coords={'x': sc.arange('x', 4.0, unit='m')},
    )


class ArrayInputs:
    """Inputs whose array references resolve to values held in memory."""

    def __init__(self, arrays: dict[Any, Any]) -> None:
        self._arrays = arrays

    def path(self, ref: Any) -> Any:
        raise NotImplementedError

    def array(self, ref: Any) -> Any:
        return self._arrays[ref]


@pytest.fixture
def inputs() -> ArrayInputs:
    return ArrayInputs({DATA_REF: raw(), DOUBLED_REF: raw() * 2})


def test_a_change_to_an_input_does_not_rerun_the_held_part(
    inputs: ArrayInputs,
) -> None:
    calls: list[float] = []

    def counted(data: RawData, threshold: Threshold) -> Filtered:
        calls.append(threshold)
        return filter_data(data, threshold)

    workflow = WarmPipeline(
        sciline.Pipeline([counted, histogram]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram},
        resolve={'data': 'array'},
        stage_inputs=['bins'],
    )
    workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=2), inputs)
    workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=4), inputs)
    assert calls == [1.5]
    workflow(HistogramParams(data=DATA_REF, threshold=3.0, bins=4), inputs)
    assert calls == [1.5, 3.0]


def test_warm_pipeline_reuses_only_when_held_params_are_unchanged(
    inputs: ArrayInputs,
) -> None:
    workflow = histogram_workflow()
    first = workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=2), inputs)
    assert not workflow.reused
    second = workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=4), inputs)
    assert workflow.reused
    assert second['histogram'].sizes == {'x': 4}
    workflow(HistogramParams(data=DATA_REF, threshold=3.0, bins=4), inputs)
    assert not workflow.reused
    assert first['histogram'].sum().value == 13.0


def test_warm_equals_cold_for_the_example(inputs: ArrayInputs) -> None:
    assert_warm_equals_cold(
        histogram_workflow,
        [
            HistogramParams(data=DATA_REF, threshold=1.5, bins=2),
            HistogramParams(data=DATA_REF, threshold=1.5, bins=4),
            HistogramParams(data=DATA_REF, threshold=3.0, bins=4),
            HistogramParams(data=DOUBLED_REF, threshold=3.0, bins=4),
        ],
        inputs,
    )


def test_an_input_without_a_key_is_refused() -> None:
    pipeline = sciline.Pipeline([filter_data, histogram])
    with pytest.raises(ValueError, match='bins'):
        WarmPipeline(pipeline, keys={}, targets={}, stage_inputs=['bins'])


def test_an_input_the_targets_do_not_need_is_refused() -> None:
    pipeline = sciline.Pipeline([filter_data, histogram])
    with pytest.raises(ValueError, match='not needed'):
        WarmPipeline(
            pipeline,
            keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
            targets={'filtered': Filtered},
            stage_inputs=['bins'],
        )


def test_session_reruns_record_reuse_of_the_expensive_part_not_of_the_callable(
    client: Client, run_ref: OutputRef
) -> None:
    """``reused`` is what D11 reads: the result came out of held state."""
    loaded = client.run(LOAD, {'run': run_ref})
    data = loaded.ref('data')
    first = client.run(HISTOGRAM, {'data': data, 'bins': 2}, label='hist')
    rebinned = client.run(HISTOGRAM, {'data': data, 'bins': 8}, label='hist')
    refiltered = client.run(
        HISTOGRAM, {'data': data, 'bins': 8, 'threshold': 2.0}, label='hist'
    )
    assert not first.reused
    assert rebinned.reused
    assert not refiltered.reused
    assert client.output(rebinned).sizes == {'x': 8}
    assert client.latest('hist').id == refiltered.id


def test_reuse_keeps_outputs_that_no_input_feeds(inputs: ArrayInputs) -> None:
    Total = NewType('Total', float)

    def total(data: RawData) -> Total:
        return Total(float(data.sum().value))

    pipeline = sciline.Pipeline([filter_data, histogram, total])
    workflow = WarmPipeline(
        pipeline,
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram, 'total': Total},
        resolve={'data': 'array'},
        stage_inputs=['bins'],
    )
    first = workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=2), inputs)
    second = workflow(HistogramParams(data=DATA_REF, threshold=1.5, bins=4), inputs)
    assert workflow.reused
    assert first['total'] == second['total'] == 14.0

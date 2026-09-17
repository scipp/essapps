# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a warm workflow reuses (D8)."""

from typing import NewType

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
from ess.apps.spec import Ref
from ess.apps.testing import assert_warm_equals_cold
from ess.apps.warm import WarmPipeline


def raw() -> sc.DataArray:
    return sc.DataArray(
        sc.array(dims=['x'], values=[1.0, 5.0, 2.0, 6.0], unit='counts'),
        coords={'x': sc.arange('x', 4.0, unit='m')},
    )


def test_a_cheap_change_does_not_rerun_the_expensive_part() -> None:
    calls: list[float] = []

    def counted(data: RawData, threshold: Threshold) -> Filtered:
        calls.append(threshold)
        return filter_data(data, threshold)

    workflow = WarmPipeline(
        sciline.Pipeline([counted, histogram]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram},
        cheap={'bins'},
    )
    workflow(HistogramParams(data=raw(), threshold=1.5, bins=2))
    workflow(HistogramParams(data=raw(), threshold=1.5, bins=4))
    assert calls == [1.5]
    workflow(HistogramParams(data=raw(), threshold=3.0, bins=4))
    assert calls == [1.5, 3.0]


def test_warm_pipeline_reuses_only_when_expensive_params_are_unchanged() -> None:
    workflow = histogram_workflow()
    first = workflow(HistogramParams(data=raw(), threshold=1.5, bins=2))
    assert not workflow.reused
    second = workflow(HistogramParams(data=raw(), threshold=1.5, bins=4))
    assert workflow.reused
    assert second['histogram'].sizes == {'x': 4}
    workflow(HistogramParams(data=raw(), threshold=3.0, bins=4))
    assert not workflow.reused
    assert first['histogram'].sum().value == 13.0


def test_warm_equals_cold_for_the_example() -> None:
    assert_warm_equals_cold(
        histogram_workflow,
        [
            HistogramParams(data=raw(), threshold=1.5, bins=2),
            HistogramParams(data=raw(), threshold=1.5, bins=4),
            HistogramParams(data=raw(), threshold=3.0, bins=4),
            HistogramParams(data=raw() * 2, threshold=3.0, bins=4),
        ],
    )


def test_cheap_parameter_without_a_key_is_refused() -> None:
    pipeline = sciline.Pipeline([filter_data, histogram])
    with pytest.raises(ValueError, match='bins'):
        WarmPipeline(pipeline, keys={}, targets={}, cheap={'bins'})


def test_cheap_parameter_the_targets_do_not_need_is_refused() -> None:
    pipeline = sciline.Pipeline([filter_data, histogram])
    with pytest.raises(ValueError, match='not needed'):
        WarmPipeline(
            pipeline,
            keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
            targets={'filtered': Filtered},
            cheap={'bins'},
        )


def test_session_reruns_of_a_sciline_workflow_record_reuse(
    client: Client, run_ref: Ref
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    data = loaded.ref('data')
    first = client.run(HISTOGRAM, {'data': data, 'bins': 2}, label='hist')
    second = client.run(HISTOGRAM, {'data': data, 'bins': 8}, label='hist')
    assert not first.reused
    assert second.reused
    assert client.output(second).sizes == {'x': 8}
    assert client.latest('hist').id == second.id


def test_reuse_keeps_outputs_that_no_cheap_parameter_feeds() -> None:
    Total = NewType('Total', float)

    def total(data: RawData) -> Total:
        return Total(float(data.sum().value))

    pipeline = sciline.Pipeline([filter_data, histogram, total])
    workflow = WarmPipeline(
        pipeline,
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram, 'total': Total},
        cheap={'bins'},
    )
    first = workflow(HistogramParams(data=raw(), threshold=1.5, bins=2))
    second = workflow(HistogramParams(data=raw(), threshold=1.5, bins=4))
    assert workflow.reused
    assert first['total'] == second['total'] == 14.0

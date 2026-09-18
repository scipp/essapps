# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a session holds between runs, and what it saves (D8)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, NewType

import pytest
import sciline
import scipp as sc
from pydantic import BaseModel

from ess.apps.adapter import PipelineAdapter
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
    write_run,
)
from ess.apps.spec import OutputRef, SpecId, dataset_ref
from ess.apps.stages import Stages
from ess.apps.testing import assert_stage_equals_workflow, equal

DATA_REF = OutputRef(record='raw', output='data')
DOUBLED_REF = OutputRef(record='raw', output='doubled')
SPEC = SpecId(name='histogram', version=1)


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


class Session:
    """One session's stage store, driven by hand: a request in, the outputs out."""

    def __init__(self, workflow: Any, inputs: ArrayInputs, *, limit: int = 4) -> None:
        self._workflow = workflow
        self._inputs = inputs
        self._stages = Stages(limit=limit)
        self.reused: list[bool] = []

    def run(
        self,
        params: BaseModel,
        label: str | None = None,
        member_key: str | None = None,
    ) -> dict[str, Any]:
        called, reused = self._stages.workflow_for(
            SPEC, self._workflow, params, self._inputs, label, member_key
        )
        self.reused.append(reused)
        return dict(called(params, self._inputs))


@pytest.fixture
def inputs() -> ArrayInputs:
    return ArrayInputs({DATA_REF: raw(), DOUBLED_REF: raw() * 2})


@pytest.fixture
def filtered() -> list[float]:
    """The thresholds the expensive provider was called with; its length is the cost."""
    return []


@pytest.fixture
def adapter(filtered: list[float]) -> Callable[..., PipelineAdapter]:
    """Histogram of a filtered run, with the filtering counted."""

    def counted(data: RawData, threshold: Threshold) -> Filtered:
        filtered.append(threshold)
        return filter_data(data, threshold)

    def make(*default_stage_inputs: str) -> PipelineAdapter:
        return PipelineAdapter(
            sciline.Pipeline([counted, histogram]),
            keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
            targets={'histogram': Histogram},
            resolve={'data': 'array'},
            default_stage_inputs=default_stage_inputs,
        )

    return make


def params(threshold: float = 1.5, bins: int = 2) -> HistogramParams:
    return HistogramParams(data=DATA_REF, threshold=threshold, bins=bins)


# The workflow itself


def test_the_callable_holds_nothing_between_calls(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    workflow = adapter('bins')
    workflow(params(bins=2), inputs)
    workflow(params(bins=4), inputs)
    assert filtered == [1.5, 1.5]


def test_a_default_stage_input_without_a_key_is_refused() -> None:
    pipeline = sciline.Pipeline([filter_data, histogram])
    with pytest.raises(ValueError, match='bins'):
        PipelineAdapter(pipeline, keys={}, targets={}, default_stage_inputs=['bins'])


def test_a_stage_input_the_targets_do_not_need_is_held(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    """Which fields a stage feeds is a cache decision, so it cannot make a run fail."""
    workflow = PipelineAdapter(
        sciline.Pipeline([filter_data, histogram]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'filtered': Filtered},
        resolve={'data': 'array'},
    )
    stage = workflow.stage(params(bins=2), {'bins'}, inputs)
    assert equal(
        stage(params(bins=99), inputs)['filtered'],
        workflow(params(bins=99), inputs)['filtered'],
    )


def test_a_stage_holds_an_output_that_no_input_feeds(inputs: ArrayInputs) -> None:
    Total = NewType('Total', float)

    def total(data: RawData) -> Total:
        return Total(float(data.sum().value))

    workflow = PipelineAdapter(
        sciline.Pipeline([filter_data, histogram, total]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram, 'total': Total},
        resolve={'data': 'array'},
    )
    stage = workflow.stage(params(bins=2), {'bins'}, inputs)
    assert stage(params(bins=2), inputs)['total'] == 14.0
    assert stage(params(bins=4), inputs)['total'] == 14.0


def test_a_stage_returns_what_the_workflow_returns(inputs: ArrayInputs) -> None:
    assert_stage_equals_workflow(
        histogram_workflow,
        [
            params(bins=2),
            params(bins=4),
            params(threshold=3.0, bins=4),
            HistogramParams(data=DOUBLED_REF, threshold=3.0, bins=4),
        ],
        inputs,
    )


# What the session decides to hold


def test_the_first_move_builds_a_stage_and_the_later_ones_are_cheap(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    session = Session(adapter(), inputs)
    session.run(params(bins=2), 'tune')
    assert filtered == [1.5]  # nothing held yet: one whole computation
    session.run(params(bins=4), 'tune')
    assert filtered == [1.5, 1.5]  # the move names the parameter, and builds a stage
    session.run(params(bins=8), 'tune')
    session.run(params(bins=16), 'tune')
    assert filtered == [1.5, 1.5]  # the stage recomputes only the histogram
    assert session.reused == [False, False, True, True]


def test_a_default_stage_input_makes_the_first_move_cheap(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    session = Session(adapter('bins'), inputs)
    session.run(params(bins=2), 'tune')
    session.run(params(bins=4), 'tune')
    assert filtered == [1.5]
    assert session.reused == [False, True]


def test_two_labels_tuning_the_same_parameter_share_one_stage(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    """Stages are keyed by what they hold, so a label only scopes the previous run."""
    session = Session(adapter('bins'), inputs)
    session.run(params(bins=2), 'first')
    session.run(params(bins=4), 'second')
    assert filtered == [1.5]
    assert session.reused == [False, True]


def test_alternating_between_two_parameters_rebuilds_per_switch(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    """
    Only the fields a request moved become stage inputs, so a switch finds no
    stage and builds one. A setting that was visited before is found again, as
    long as its stage has not been dropped.
    """
    session = Session(adapter(), inputs)
    session.run(params(threshold=1.5, bins=2), 'tune')
    session.run(params(threshold=1.5, bins=4), 'tune')
    session.run(params(threshold=3.0, bins=4), 'tune')
    session.run(params(threshold=3.0, bins=2), 'tune')
    assert filtered == [1.5, 1.5, 3.0, 3.0]
    session.run(params(threshold=1.5, bins=2), 'tune')
    assert filtered == [1.5, 1.5, 3.0, 3.0]
    assert session.reused == [False, False, False, False, True]


def test_the_cap_drops_the_least_recently_used_stage(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    workflow = adapter('bins')
    session = Session(workflow, inputs, limit=2)
    for threshold, label in ((1.0, 'a'), (2.0, 'b'), (3.0, 'c')):
        session.run(params(threshold=threshold, bins=2), label)
    assert filtered == [1.0, 2.0, 3.0]

    # The stage of label 'a' was dropped, so its request is computed again.
    again = session.run(params(threshold=1.0, bins=4), 'a')
    assert filtered == [1.0, 2.0, 3.0, 1.0]
    assert session.reused == [False, False, False, False]
    expected = workflow(params(threshold=1.0, bins=4), inputs)
    assert equal(again['histogram'], expected['histogram'])


def test_a_new_member_of_a_batch_differs_from_the_previous_member(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    """
    A request that supersedes nothing has the latest earlier request under its
    label as predecessor, which for a batch is the member before it.
    """
    session = Session(adapter(), inputs)
    session.run(params(threshold=1.0), 'batch', '1')
    session.run(params(threshold=2.0), 'batch', '2')
    session.run(params(threshold=3.0), 'batch', '3')
    # The second member names the threshold; the third is served from its stage.
    assert session.reused == [False, False, True]
    assert filtered == [1.0, 2.0, 3.0]


def test_a_corrected_member_differs_from_the_record_it_supersedes(
    adapter: Callable[..., PipelineAdapter], inputs: ArrayInputs, filtered: list[float]
) -> None:
    """Under one member key a request supersedes the previous one, not the member
    that happened to run last."""
    session = Session(adapter(), inputs)
    session.run(params(threshold=1.0, bins=2), 'batch', '1')
    session.run(params(threshold=2.0, bins=2), 'batch', '2')
    session.run(params(threshold=1.0, bins=8), 'batch', '1')
    session.run(params(threshold=1.0, bins=16), 'batch', '1')
    # The correction moved the bin count against member 1, not the threshold as
    # well against member 2, so the stage it builds holds the filtered run.
    assert filtered == [1.0, 2.0, 1.0]
    assert session.reused == [False, False, False, True]


# The Amor pattern: a series of runs against one reference, then a binning slider

SampleRun = NewType('SampleRun', str)
ReferenceRun = NewType('ReferenceRun', str)
ReducedReference = NewType('ReducedReference', str)
QNumBins = NewType('QNumBins', int)
Curve = NewType('Curve', str)


class CurveParams(BaseModel):
    sample_run: str
    reference_run: str = 'ref'
    q_num_bins: int = 200


def test_the_expensive_part_runs_per_stage_build_and_not_per_move(
    inputs: ArrayInputs,
) -> None:
    """
    The reduced reference depends on the reference run alone, so a stage over any
    other field holds it.
    """
    reductions: list[str] = []

    def reduce_reference(run: ReferenceRun) -> ReducedReference:
        reductions.append(run)
        return ReducedReference(run.upper())

    def curve(sample: SampleRun, reference: ReducedReference, bins: QNumBins) -> Curve:
        return Curve(f'{sample}/{reference}/{bins}')

    session = Session(
        PipelineAdapter(
            sciline.Pipeline([reduce_reference, curve]),
            keys={
                'sample_run': SampleRun,
                'reference_run': ReferenceRun,
                'q_num_bins': QNumBins,
            },
            targets={'curve': Curve},
        ),
        inputs,
    )
    for sample in ('a', 'b', 'c'):
        session.run(CurveParams(sample_run=sample), 'amor')
    assert reductions == ['ref', 'ref']  # the first run, then the stage over the run

    for bins in (100, 50, 25):
        session.run(CurveParams(sample_run='c', q_num_bins=bins), 'amor')
    assert reductions == ['ref', 'ref', 'ref']  # the stage over the bin count
    assert session.reused == [False, False, True, False, True, True]
    assert session.run(CurveParams(sample_run='c', q_num_bins=25), 'amor') == {
        'curve': 'c/REF/25'
    }


# Through a session


def test_session_reruns_record_reuse_of_the_stage_not_of_the_callable(
    client: Client, run_ref: OutputRef
) -> None:
    """``reused`` is what D11 reads: the result came out of a held stage."""
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


def test_a_file_that_changed_on_disk_does_not_find_the_stage_built_from_its_bytes(
    client: Client, run_file: Path
) -> None:
    """
    A dataset reference is the same when the bytes behind it are not, so the
    checksums of the datasets a stage fixed are part of its address.
    """
    run = dataset_ref(instrument='dream', run=1)
    first = client.run(HISTOGRAM, {'data': run, 'bins': 2}, label='hist')
    served = client.run(HISTOGRAM, {'data': run, 'bins': 4}, label='hist')
    assert not first.reused
    assert served.reused

    write_run(run_file, [9.0] * 6)
    again = client.run(HISTOGRAM, {'data': run, 'bins': 4}, label='hist')
    assert not again.reused
    assert client.output(again).sum().value == 54.0

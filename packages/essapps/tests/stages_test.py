# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a stage computes and holds, and which stages a session keeps.

See docs/developer/stages.md.
"""

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
    NORMALIZE,
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
from ess.apps.records import Status, Template
from ess.apps.spec import DatasetRef, OutputRef, dataset_ref, submodel
from ess.apps.stages import Stages
from ess.apps.testing import assert_stage_equals_workflow, equal

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


def values(model: type[BaseModel] = HistogramParams, **given: Any) -> BaseModel:
    """The named fields of a params model: what a stage is built over or called with."""
    return submodel(model, given, 'Values').model_validate(given)


@pytest.fixture
def inputs() -> ArrayInputs:
    return ArrayInputs({DATA_REF: raw(), DOUBLED_REF: raw() * 2})


@pytest.fixture
def filtered() -> list[float]:
    """The thresholds the expensive provider was called with; its length is the cost."""
    return []


@pytest.fixture
def counted(filtered: list[float]) -> PipelineAdapter:
    """Histogram of a filtered run, with the filtering counted."""

    def counted_filter(data: RawData, threshold: Threshold) -> Filtered:
        filtered.append(threshold)
        return filter_data(data, threshold)

    return PipelineAdapter(
        sciline.Pipeline([counted_filter, histogram]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'histogram': Histogram},
        resolve={'data': 'array'},
    )


# What a stage computes and what it holds


def test_a_stage_computes_only_what_its_inputs_affect(
    counted: PipelineAdapter, inputs: ArrayInputs, filtered: list[float]
) -> None:
    stage = counted.stage(
        values(data=DATA_REF, threshold=1.5), ['bins'], ['histogram'], inputs
    )
    results = [stage(values(bins=bins), {}, inputs) for bins in (2, 4, 8)]
    assert filtered == [1.5]
    assert [r['histogram'].sizes for r in results] == [{'x': 2}, {'x': 4}, {'x': 8}]


def test_a_stage_input_the_outputs_do_not_need_is_held(inputs: ArrayInputs) -> None:
    """Which fields a stage takes cannot make a run fail."""
    workflow = PipelineAdapter(
        sciline.Pipeline([filter_data, histogram]),
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        targets={'filtered': Filtered},
        resolve={'data': 'array'},
    )
    stage = workflow.stage(
        values(data=DATA_REF, threshold=1.5), ['bins'], ['filtered'], inputs
    )
    plain = workflow.stage(
        values(data=DATA_REF, threshold=1.5, bins=99), [], ['filtered'], inputs
    )
    assert equal(
        stage(values(bins=99), {}, inputs)['filtered'],
        plain(values(), {}, inputs)['filtered'],
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
    stage = workflow.stage(
        values(data=DATA_REF, threshold=1.5), ['bins'], ['histogram', 'total'], inputs
    )
    assert stage(values(bins=2), {}, inputs)['total'] == 14.0
    assert stage(values(bins=4), {}, inputs)['total'] == 14.0


@pytest.mark.parametrize(
    ('params', 'stage_inputs'),
    [
        ({'data': DATA_REF}, [{'bins': 2}, {'bins': 4}]),
        (
            {'data': DATA_REF},
            [{'threshold': 1.5, 'bins': 2}, {'threshold': 3.0, 'bins': 4}],
        ),
        ({'bins': 4}, [{'data': DATA_REF}, {'data': DOUBLED_REF}]),
    ],
    ids=['bins', 'threshold-and-bins', 'data'],
)
def test_a_stage_returns_what_a_plain_run_returns(
    inputs: ArrayInputs, params: dict[str, Any], stage_inputs: list[dict[str, Any]]
) -> None:
    assert_stage_equals_workflow(
        histogram_workflow(), HISTOGRAM, params, stage_inputs, inputs
    )


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


def test_the_expensive_part_runs_once_per_stage(inputs: ArrayInputs) -> None:
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

    workflow = PipelineAdapter(
        sciline.Pipeline([reduce_reference, curve]),
        keys={
            'sample_run': SampleRun,
            'reference_run': ReferenceRun,
            'q_num_bins': QNumBins,
        },
        targets={'curve': Curve},
    )
    members = workflow.stage(
        values(CurveParams, reference_run='ref', q_num_bins=200),
        ['sample_run'],
        ['curve'],
        inputs,
    )
    for sample in ('a', 'b', 'c'):
        members(values(CurveParams, sample_run=sample), {}, inputs)
    assert reductions == ['ref']

    slider = workflow.stage(
        values(CurveParams, sample_run='c', reference_run='ref'),
        ['q_num_bins'],
        ['curve'],
        inputs,
    )
    results = [
        slider(values(CurveParams, q_num_bins=bins), {}, inputs)
        for bins in (100, 50, 25)
    ]
    assert reductions == ['ref', 'ref']
    assert results[-1] == {'curve': 'c/REF/25'}


# The session's store


def test_the_cap_drops_the_least_recently_used_stage() -> None:
    built: list[str] = []

    def build(name: str) -> Any:
        built.append(name)
        return name

    stages = Stages(limit=2)
    for name in ('a', 'b', 'a', 'c'):
        stages.stage(name, lambda name=name: build(name))
    assert built == ['a', 'b', 'c']  # 'a' was used again, so 'b' went first
    assert stages.stage('a', lambda: build('a')) == ('a', True)
    assert stages.stage('b', lambda: build('b')) == ('b', False)


# Through a session: the stage a caller names


@pytest.fixture
def data(client: Client, run_ref: DatasetRef) -> OutputRef:
    return client.run(LOAD, {'run': run_ref}).ref('data')


def test_a_named_stage_is_held_from_its_first_call(
    client: Client, data: OutputRef
) -> None:
    """``reused`` is what publication reads: the result came out of a held stage."""
    tune = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='hist'
    )
    first = client.run(tune, {'bins': 2})
    second = client.run(tune, {'bins': 8})
    assert not first.reused
    assert second.reused
    assert client.output(second).sizes == {'x': 8}
    assert client.latest('hist').id == second.id

    # The stage is named by what it is, so another template for it finds it too.
    again = Template(spec=HISTOGRAM, params={'data': data}, blanks=('bins',))
    assert client.run(again, {'bins': 4}).reused


def test_plain_runs_that_differ_in_one_value_share_no_stage(
    client: Client, data: OutputRef
) -> None:
    """Each has a workflow ID of its own; no stage is inferred from the pair."""
    first = client.run(HISTOGRAM, {'data': data, 'bins': 2}, label='hist')
    second = client.run(HISTOGRAM, {'data': data, 'bins': 8}, label='hist')
    assert not first.reused
    assert not second.reused
    assert second.supersedes == first.id


def test_a_varied_value_replaces_the_templates_and_stays_out_of_the_workflow_id(
    client: Client, data: OutputRef
) -> None:
    first = client.run(
        Template(spec=HISTOGRAM, params={'data': data, 'bins': 2}, blanks=('bins',)),
        {'bins': 4},
    )
    assert first.request.params['bins'] == 4
    second = client.run(
        Template(spec=HISTOGRAM, params={'data': data}, blanks=('bins',)), {'bins': 8}
    )
    assert second.request.workflow_id == first.request.workflow_id
    assert second.reused


def test_another_workflow_or_another_cut_builds_a_new_stage(
    client: Client, data: OutputRef
) -> None:
    hist = Template(spec=HISTOGRAM, params={'data': data}, blanks=('bins',))
    assert not client.run(hist, {'bins': 2}).reused

    refiltered = Template(
        spec=HISTOGRAM, params={'data': data, 'threshold': 2.0}, blanks=('bins',)
    )
    assert not client.run(refiltered, {'bins': 4}).reused
    assert client.run(refiltered, {'bins': 8}).reused

    both = hist.cut(blanks=('threshold', 'bins'))
    assert not client.run(both, {'threshold': 2.0, 'bins': 8}).reused


def test_a_file_that_changed_on_disk_does_not_find_the_stage_built_from_its_bytes(
    client: Client, run_file: Path
) -> None:
    """
    A dataset reference is the same when the bytes behind it are not, so the
    checksums of the datasets a request's params name are part of a stage's name.
    """
    run = dataset_ref(instrument='dream', run=1)
    tune = Template(spec=HISTOGRAM, params={'data': run}, blanks=('bins',))
    assert not client.run(tune, {'bins': 2}).reused
    assert client.run(tune, {'bins': 4}).reused

    write_run(run_file, [9.0] * 6)
    again = client.run(tune, {'bins': 4})
    assert not again.reused
    assert client.output(again).sum().value == 54.0


def test_an_intermediate_is_a_stage_output_and_a_stage_input(
    client: Client, run_ref: DatasetRef
) -> None:
    """Cutting the pipeline at the intermediates gives what a plain run gives."""
    normalize = Template(spec=NORMALIZE, params={'floor': 1.5, 'scale': 2.0})
    member = client.run(
        normalize.cut(blanks=('run',), outputs=('numerator', 'denominator')),
        {'run': run_ref},
    )
    assert member.output_names() == {'numerator', 'denominator'}
    finalize = client.run(
        normalize.cut(blanks=('numerator', 'denominator'), outputs=('normalized',)),
        {
            'numerator': member.ref('numerator'),
            'denominator': member.ref('denominator'),
        },
    )
    plain = client.run(normalize, {'run': run_ref})
    assert [r.status for r in (member, finalize, plain)] == [Status.COMPLETED] * 3
    assert plain.output_names() == {'normalized'}
    assert equal(client.output(finalize), client.output(plain))

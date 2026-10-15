# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import functools
from collections.abc import Mapping, Sequence
from typing import Any, NewType

import numpy as np
import pytest
import sciline
from pydantic import BaseModel, Field

from ess.spec import DriverBinding, WorkflowSpec
from ess.spec.pipeline import PipelineBinding
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row


def _spec(name: str, params: type[BaseModel], outputs: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
    )


Loaded = NewType('Loaded', float)
Offset = NewType('Offset', float)
Shifted = NewType('Shifted', float)


def shift(loaded: Loaded, offset: Offset) -> Shifted:
    return Shifted(loaded + offset)


class Loading(BaseModel):
    loaded: float


class Shift(BaseModel):
    offset: float = 0.25


class ShiftParams(BaseModel):
    run: Loading
    shift: Shift | None = Field(default_factory=Shift)


class Value(BaseModel):
    value: float


SHIFT = _spec('shift', ShiftParams, Value)


@pytest.fixture
def binding() -> PipelineBinding:
    return PipelineBinding(
        SHIFT,
        sciline.Pipeline([shift]),
        params={'run.loaded': Loaded, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )


def test_a_plain_request_returns_the_outputs_by_field_name(
    binding: PipelineBinding,
) -> None:
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.compute(values) == {'value': 10.5}


def test_a_parameter_left_out_takes_the_specs_default(
    binding: PipelineBinding,
) -> None:
    assert binding.compute({'run': {'loaded': 10.0}}) == {'value': 10.25}
    staged = binding.make_stage({}, ('run',))
    assert staged({'run': {'loaded': 10.0}}) == {'value': 10.25}


def test_a_stage_with_a_blank_group_gives_the_plain_requests_outputs(
    binding: PipelineBinding,
) -> None:
    plain = binding.compute({'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}})
    staged = binding.make_stage({'run': {'loaded': 10.0}}, ('shift',))
    assert staged({'shift': {'offset': 0.5}}) == plain


def test_a_group_mapped_as_a_whole_sets_its_values() -> None:
    Settings = NewType('Settings', dict)

    def from_settings(settings: Settings) -> Offset:
        return Offset(settings['offset'])

    binding = PipelineBinding(
        SHIFT,
        sciline.Pipeline([shift, from_settings]),
        params={'run.loaded': Loaded, 'shift': Settings},
        outputs={'value': Shifted},
    )
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.compute(values) == {'value': 10.5}


def test_an_optional_group_mapped_by_paths_sets_its_keys_to_none() -> None:
    def shift_or_not(loaded: Loaded, offset: Offset) -> Shifted:
        return Shifted(loaded + (offset or 0.0))

    binding = PipelineBinding(
        SHIFT,
        sciline.Pipeline([shift_or_not]),
        params={'run.loaded': Loaded, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )
    staged = binding.make_stage({'run': {'loaded': 10.0}}, ('shift',))

    assert binding.compute({'run': {'loaded': 10.0}, 'shift': None}) == {'value': 10.0}
    assert staged({'shift': None}) == {'value': 10.0}
    assert staged({'shift': {'offset': 0.5}}) == {'value': 10.5}


class RunsParams(BaseModel):
    runs: list[Loading]


def test_a_table_mapped_as_a_whole_sets_its_key_to_the_rows() -> None:
    Runs = NewType('Runs', list)

    def total(runs: Runs) -> Shifted:
        return Shifted(sum(run['loaded'] for run in runs))

    binding = PipelineBinding(
        _spec('runs', RunsParams, Value),
        sciline.Pipeline([total]),
        params={'runs': Runs},
        outputs={'value': Shifted},
    )
    rows = [{'loaded': 1.0}, {'loaded': 2.0}]
    assert binding.compute({'runs': rows}) == {'value': 3.0}


@pytest.mark.parametrize(
    ('params', 'outputs', 'reason'),
    [
        ({'shift': Offset, 'shift.offset': Offset}, {'value': Shifted}, 'as a whole'),
        ({'speed': Offset}, {'value': Shifted}, r"\['speed'\]: not parameters"),
        ({'run.loaded': Loaded}, {'other': Shifted}, r"\['other'\]: not outputs"),
        ({'run.loaded': Loaded}, {}, r"\['value'\]: outputs of shift not mapped"),
    ],
)
def test_a_mapping_that_does_not_fit_the_spec_is_refused(
    params: dict[str, Any], outputs: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        PipelineBinding(
            SHIFT, sciline.Pipeline([shift]), params=params, outputs=outputs
        )


@pytest.mark.parametrize(
    ('fixed', 'blanks', 'reason'),
    [
        ({'speed': 2.0}, ('run',), r"\['speed'\]: not parameters of shift"),
        ({}, ('shift',), r"\['run'\]: neither fixed nor blank"),
        ({'run': {'loaded': 1.0}}, ('run',), r"\['run'\]: both fixed and blank"),
        ({'run': {'loaded': 10.0, 'speed': 2.0}}, (), r"no sciline key.*run.speed"),
    ],
)
def test_a_stage_refuses_values_that_do_not_fit(
    binding: PipelineBinding,
    fixed: dict[str, Any],
    blanks: tuple[str, ...],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        binding.make_stage(fixed, blanks)


# A toy SANS reduction: a pipeline of one sample run and one can run, and a
# driver over the same pipeline that sums the runs of each table.

SampleFile = NewType('SampleFile', list)
CanFile = NewType('CanFile', list)
Bins = NewType('Bins', int)
SampleCounts = NewType('SampleCounts', np.ndarray)
CanCounts = NewType('CanCounts', np.ndarray)
SampleNumerator = NewType('SampleNumerator', np.ndarray)
SampleDenominator = NewType('SampleDenominator', np.ndarray)
CanNumerator = NewType('CanNumerator', np.ndarray)
CanDenominator = NewType('CanDenominator', np.ndarray)
SampleIofQ = NewType('SampleIofQ', np.ndarray)
IofQ = NewType('IofQ', np.ndarray)


class Run(BaseModel):
    file: list[float]


class Reduction(BaseModel):
    bins: int = 2


class IofQParams(BaseModel):
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)


class MultiIofQParams(BaseModel):
    sample: list[Run]
    can: list[Run]
    reduction: Reduction = Field(default_factory=Reduction)


class IofQOutputs(BaseModel):
    sample_iofq: Any
    iofq: Any


IOFQ = _spec('iofq', IofQParams, IofQOutputs)
IOFQ_MULTI = _spec('iofq-multi', MultiIofQParams, IofQOutputs)
OUTPUTS = {'sample_iofq': SampleIofQ, 'iofq': IofQ}


@pytest.fixture
def loaded() -> list[list[float]]:
    return []


@pytest.fixture
def sans(loaded: list[list[float]]) -> sciline.Pipeline:
    """
    The denominators are arrays of one element, so that adding into a value
    that a stage holds would change the next call.
    """

    def load_sample(run: SampleFile) -> SampleCounts:
        loaded.append(run)
        return SampleCounts(np.asarray(run, dtype=float))

    def load_can(run: CanFile) -> CanCounts:
        loaded.append(run)
        return CanCounts(np.asarray(run, dtype=float))

    def sample_numerator(counts: SampleCounts, bins: Bins) -> SampleNumerator:
        return SampleNumerator(counts.reshape(bins, -1).sum(axis=1))

    def sample_denominator(counts: SampleCounts) -> SampleDenominator:
        return SampleDenominator(counts.sum(keepdims=True))

    def can_numerator(counts: CanCounts, bins: Bins) -> CanNumerator:
        return CanNumerator(counts.reshape(bins, -1).sum(axis=1))

    def can_denominator(counts: CanCounts) -> CanDenominator:
        return CanDenominator(counts.sum(keepdims=True))

    def sample_iofq(num: SampleNumerator, den: SampleDenominator) -> SampleIofQ:
        return SampleIofQ(num / den)

    def iofq(sample: SampleIofQ, num: CanNumerator, den: CanDenominator) -> IofQ:
        return IofQ(sample - num / den)

    providers = [load_sample, load_can, sample_numerator, sample_denominator]
    providers += [can_numerator, can_denominator, sample_iofq, iofq]
    return sciline.Pipeline(providers)


class IofQDriver:
    """
    Sums the numerator and the denominator of each table's runs, and computes
    the outputs from the sums; refuses a state without a sample and a can run.
    """

    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[str, Any]) -> None:
        wf = pipeline.copy()
        wf[Bins] = fixed['reduction']['bins']
        parts = {
            'sample': (SampleFile, (SampleNumerator, SampleDenominator)),
            'can': (CanFile, (CanNumerator, CanDenominator)),
        }
        self._contribute = {
            table: (key, sciline.Stage(wf, inputs=(key,), outputs=summed))
            for table, (key, summed) in parts.items()
        }
        summed = [key for _, keys in parts.values() for key in keys]
        self._finalize = sciline.Stage(
            wf, inputs=summed, outputs=tuple(OUTPUTS.values())
        )
        self._sums: dict[Any, np.ndarray] = {}

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        key, stage = self._contribute[table]
        for row in rows:
            for k, part in stage.compute({key: row['file']}).items():
                self._sums[k] = self._sums[k] + part if k in self._sums else part

    def outputs(self) -> Mapping[str, Any]:
        results = self._finalize.compute(self._sums)
        return {name: results[key] for name, key in OUTPUTS.items()}


@pytest.fixture
def make_driver(sans: sciline.Pipeline) -> functools.partial[IofQDriver]:
    return functools.partial(IofQDriver, sans)


@pytest.fixture
def multi(make_driver: functools.partial[IofQDriver]) -> DriverBinding:
    return DriverBinding(IOFQ_MULTI, make_driver)


@pytest.fixture
def single(sans: sciline.Pipeline) -> PipelineBinding:
    params = {'sample.file': SampleFile, 'can.file': CanFile, 'reduction.bins': Bins}
    return PipelineBinding(IOFQ, sans, params=params, outputs=OUTPUTS)


SAMPLES = [[1.0, 3.0, 2.0, 6.0], [2.0, 2.0, 0.0, 4.0]]
CANS = [[1.0, 1.0, 1.0, 1.0], [0.0, 2.0, 2.0, 0.0]]
ROWS = {
    'sample': [{'file': run} for run in SAMPLES],
    'can': [{'file': run} for run in CANS],
}


def _reduction(bins: int) -> dict[str, dict[str, int]]:
    return {'reduction': {'bins': bins}}


def test_the_bindings_keep_the_symmetries(
    single: PipelineBinding,
    multi: DriverBinding,
    make_driver: functools.partial[IofQDriver],
) -> None:
    runs = {'sample': {'file': SAMPLES[0]}, 'can': {'file': CANS[0]}}
    check_one_row(
        single,
        multi,
        {**runs, **_reduction(2)},
        {'sample': ROWS['sample'][:1], 'can': ROWS['can'][:1], **_reduction(2)},
    )
    check_caching(single, runs, [_reduction(2), _reduction(1), _reduction(2)])
    check_arrival_and_order(make_driver, _reduction(2), ROWS)


def test_summing_the_rows_gives_the_reduction_of_the_summed_runs(
    single: PipelineBinding, multi: DriverBinding
) -> None:
    summed = {
        'sample': {'file': list(np.sum(SAMPLES, axis=0))},
        'can': {'file': list(np.sum(CANS, axis=0))},
        **_reduction(2),
    }
    np.testing.assert_allclose(
        multi.compute({**ROWS, **_reduction(2)})['iofq'],
        single.compute(summed)['iofq'],
    )


def test_a_stage_loads_each_run_once(
    single: PipelineBinding, loaded: list[list[float]]
) -> None:
    tune = single.make_stage(
        {'sample': {'file': SAMPLES[0]}, 'can': {'file': CANS[0]}}, ('reduction',)
    )
    for bins in (1, 2, 4):
        tune(_reduction(bins))

    assert sorted(loaded) == sorted([SAMPLES[0], CANS[0]])


def test_a_driver_with_fixed_rows_loads_each_run_once_across_reads(
    multi: DriverBinding, loaded: list[list[float]]
) -> None:
    driver = multi.make_accumulator({'can': ROWS['can'], **_reduction(2)}, ('sample',))
    for row in ROWS['sample']:
        driver.push('sample', [row])
        driver.outputs()

    assert loaded == CANS + SAMPLES

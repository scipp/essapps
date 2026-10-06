# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from typing import NewType

import numpy as np
import pytest
import sciline

from ess.spec.pipeline import AccumulatingPipelineBinding, PipelineBinding
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row

Loaded = NewType('Loaded', float)
Offset = NewType('Offset', float)
Shifted = NewType('Shifted', float)


def shift(loaded: Loaded, offset: Offset) -> Shifted:
    return Shifted(loaded + offset)


@pytest.fixture
def binding() -> PipelineBinding:
    return PipelineBinding(
        sciline.Pipeline([shift]),
        params={'loaded': Loaded, 'offset': Offset},
        outputs={'value': Shifted},
    )


def test_a_plain_request_returns_the_outputs_by_field_name(
    binding: PipelineBinding,
) -> None:
    assert binding.stage({'loaded': 10.0, 'offset': 0.5}, ())() == {'value': 10.5}


def test_a_stage_with_a_blank_gives_the_plain_requests_outputs(
    binding: PipelineBinding,
) -> None:
    plain = binding.stage({'loaded': 10.0, 'offset': 0.5}, ())()
    staged = binding.stage({'loaded': 10.0}, ('offset',))(offset=0.5)
    assert staged == plain


def test_an_unknown_parameter_is_refused(binding: PipelineBinding) -> None:
    with pytest.raises(ValueError, match='no sciline key'):
        binding.stage({'speed': 2.0}, ())


SampleFile = NewType('SampleFile', list)
CanFile = NewType('CanFile', list)
Bins = NewType('Bins', int)
SampleCounts = NewType('SampleCounts', np.ndarray)
CanCounts = NewType('CanCounts', np.ndarray)
SampleNumerator = NewType('SampleNumerator', np.ndarray)
SampleDenominator = NewType('SampleDenominator', float)
CanNumerator = NewType('CanNumerator', np.ndarray)
CanDenominator = NewType('CanDenominator', float)
SampleIofQ = NewType('SampleIofQ', np.ndarray)
IofQ = NewType('IofQ', np.ndarray)


@pytest.fixture
def loaded() -> list[list[float]]:
    return []


@pytest.fixture
def sans(loaded: list[list[float]]) -> sciline.Pipeline:
    """A toy SANS reduction of one sample run and one can run."""

    def load_sample(run: SampleFile) -> SampleCounts:
        loaded.append(run)
        return SampleCounts(np.asarray(run, dtype=float))

    def load_can(run: CanFile) -> CanCounts:
        loaded.append(run)
        return CanCounts(np.asarray(run, dtype=float))

    def sample_numerator(counts: SampleCounts, bins: Bins) -> SampleNumerator:
        return SampleNumerator(counts.reshape(bins, -1).sum(axis=1))

    def sample_denominator(counts: SampleCounts) -> SampleDenominator:
        return SampleDenominator(counts.sum())

    def can_numerator(counts: CanCounts, bins: Bins) -> CanNumerator:
        return CanNumerator(counts.reshape(bins, -1).sum(axis=1))

    def can_denominator(counts: CanCounts) -> CanDenominator:
        return CanDenominator(counts.sum())

    def sample_iofq(num: SampleNumerator, den: SampleDenominator) -> SampleIofQ:
        return SampleIofQ(num / den)

    def iofq(sample: SampleIofQ, num: CanNumerator, den: CanDenominator) -> IofQ:
        return IofQ(sample - num / den)

    providers = [load_sample, load_can, sample_numerator, sample_denominator]
    providers += [can_numerator, can_denominator, sample_iofq, iofq]
    return sciline.Pipeline(providers)


OUTPUTS = {'sample': SampleIofQ, 'iofq': IofQ}
SUMMED = (SampleNumerator, SampleDenominator, CanNumerator, CanDenominator)
TABLES = {'sample_runs': {'run': SampleFile}, 'can_runs': {'run': CanFile}}


@pytest.fixture
def multi(sans: sciline.Pipeline) -> AccumulatingPipelineBinding:
    return AccumulatingPipelineBinding(
        sans, params={'bins': Bins}, tables=TABLES, outputs=OUTPUTS, accumulate=SUMMED
    )


@pytest.fixture
def single(sans: sciline.Pipeline) -> PipelineBinding:
    params = {'run': SampleFile, 'can': CanFile, 'bins': Bins}
    return PipelineBinding(sans, params=params, outputs=OUTPUTS)


SAMPLES = [[1.0, 3.0, 2.0, 6.0], [2.0, 2.0, 0.0, 4.0]]
CANS = [[1.0, 1.0, 1.0, 1.0], [0.0, 2.0, 2.0, 0.0]]
ROWS = {
    'sample_runs': [{'run': run} for run in SAMPLES],
    'can_runs': [{'run': run} for run in CANS],
}


def test_the_binding_keeps_the_symmetries(
    single: PipelineBinding, multi: AccumulatingPipelineBinding
) -> None:
    one_row = {'sample_runs': ROWS['sample_runs'][:1], 'can_runs': ROWS['can_runs'][:1]}
    check_one_row(
        single,
        multi,
        {'run': SAMPLES[0], 'can': CANS[0], 'bins': 2},
        {**one_row, 'bins': 2},
    )
    check_caching(multi, ROWS, [{'bins': 2}, {'bins': 1}, {'bins': 2}])
    check_caching(multi, {'bins': 2}, [ROWS, one_row, ROWS])
    check_arrival_and_order(multi, {'bins': 2}, ROWS)


def test_summing_the_rows_gives_the_reduction_of_the_summed_runs(
    single: PipelineBinding, multi: AccumulatingPipelineBinding
) -> None:
    summed = {
        'run': list(np.sum(SAMPLES, axis=0)),
        'can': list(np.sum(CANS, axis=0)),
        'bins': 2,
    }
    np.testing.assert_allclose(
        multi.stage({**ROWS, 'bins': 2}, ())()['iofq'],
        single.stage(summed, ())()['iofq'],
    )


def test_a_stage_over_fixed_rows_loads_each_run_once(
    multi: AccumulatingPipelineBinding, loaded: list[list[float]]
) -> None:
    tune = multi.stage(ROWS, ('bins',))
    for bins in (1, 2, 4):
        tune(bins=bins)

    assert sorted(loaded) == sorted(SAMPLES + CANS)


def test_a_held_state_loads_each_run_once_across_reads(
    multi: AccumulatingPipelineBinding, loaded: list[list[float]]
) -> None:
    held = multi.held_state({'bins': 2})
    for sample in SAMPLES:
        held.push({'sample_runs': {'run': sample}})
        held.outputs()

    assert loaded == SAMPLES


def test_an_accumulator_may_fix_the_rows_of_one_table(
    multi: AccumulatingPipelineBinding,
) -> None:
    held = multi.held_state({'bins': 2, 'can_runs': ROWS['can_runs']})
    for row in ROWS['sample_runs']:
        held.push({'sample_runs': row})

    np.testing.assert_allclose(
        held.outputs()['iofq'], multi.stage({**ROWS, 'bins': 2}, ())()['iofq']
    )


def test_an_output_that_needs_a_table_with_no_rows_is_left_out(
    multi: AccumulatingPipelineBinding,
) -> None:
    plain = multi.stage(
        {'sample_runs': ROWS['sample_runs'], 'can_runs': [], 'bins': 2}, ()
    )

    assert plain().keys() == {'sample'}


def test_an_accumulated_key_that_depends_on_two_tables_is_refused(
    sans: sciline.Pipeline,
) -> None:
    def masked_can(counts: CanCounts, mask: SampleCounts, bins: Bins) -> CanNumerator:
        return CanNumerator((counts * (mask > 0)).reshape(bins, -1).sum(axis=1))

    sans.insert(masked_can)
    with pytest.raises(
        ValueError,
        match=r"CanNumerator.* one table, not of \['can_runs', 'sample_runs'\]",
    ):
        AccumulatingPipelineBinding(
            sans,
            params={'bins': Bins},
            tables=TABLES,
            outputs=OUTPUTS,
            accumulate=SUMMED,
        )


def test_an_output_that_reads_rows_other_than_through_sums_is_refused(
    sans: sciline.Pipeline,
) -> None:
    with pytest.raises(
        ValueError, match=r"output iofq depends on the rows of \['can_runs'\]"
    ):
        AccumulatingPipelineBinding(
            sans,
            params={'bins': Bins},
            tables=TABLES,
            outputs=OUTPUTS,
            accumulate=(SampleNumerator, SampleDenominator, CanDenominator),
        )


def test_a_table_that_no_accumulated_key_depends_on_is_refused(
    sans: sciline.Pipeline,
) -> None:
    with pytest.raises(
        ValueError, match=r"no accumulated key depends on the rows of \['can_runs'\]"
    ):
        AccumulatingPipelineBinding(
            sans,
            params={'bins': Bins},
            tables=TABLES,
            outputs={'sample': SampleIofQ},
            accumulate=(SampleNumerator, SampleDenominator),
        )


def test_a_row_field_without_a_sciline_key_is_refused(
    multi: AccumulatingPipelineBinding,
) -> None:
    held = multi.held_state({'bins': 2})
    with pytest.raises(
        ValueError, match=r"no sciline key for \['note'\] of sample_runs"
    ):
        held.push({'sample_runs': {'run': SAMPLES[0], 'note': 'x'}})

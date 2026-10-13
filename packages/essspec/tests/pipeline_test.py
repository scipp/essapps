# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import gc
import weakref
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


Settings = NewType('Settings', dict)
Run = NewType('Run', dict)
Numerator = NewType('Numerator', np.ndarray)


@pytest.fixture
def binding() -> PipelineBinding:
    return PipelineBinding(
        sciline.Pipeline([shift]),
        params={'run.loaded': Loaded, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )


def test_a_plain_request_returns_the_outputs_by_field_name(
    binding: PipelineBinding,
) -> None:
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.stage(values, ())() == {'value': 10.5}


def test_a_stage_with_a_blank_group_gives_the_plain_requests_outputs(
    binding: PipelineBinding,
) -> None:
    plain = binding.stage({'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}, ())()
    staged = binding.stage({'run': {'loaded': 10.0}}, ('shift',))
    assert staged(shift={'offset': 0.5}) == plain


def test_a_group_mapped_as_a_whole_sets_its_values() -> None:
    def from_settings(settings: Settings) -> Offset:
        return Offset(settings['offset'])

    binding = PipelineBinding(
        sciline.Pipeline([shift, from_settings]),
        params={'run.loaded': Loaded, 'shift': Settings},
        outputs={'value': Shifted},
    )
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.stage(values, ())() == {'value': 10.5}


def test_an_optional_group_mapped_by_paths_sets_its_keys_to_none() -> None:
    def shift_or_not(loaded: Loaded, offset: Offset) -> Shifted:
        return Shifted(loaded + (offset or 0.0))

    binding = PipelineBinding(
        sciline.Pipeline([shift_or_not]),
        params={'run.loaded': Loaded, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )
    staged = binding.stage({'run': {'loaded': 10.0}}, ('shift',))

    assert binding.stage({'run': {'loaded': 10.0}, 'shift': None}, ())() == {
        'value': 10.0
    }
    assert staged(shift=None) == {'value': 10.0}
    assert staged(shift={'offset': 0.5}) == {'value': 10.5}


def test_a_group_mapped_as_a_whole_and_by_paths_is_refused() -> None:
    with pytest.raises(ValueError, match='as a whole and by paths'):
        PipelineBinding(
            sciline.Pipeline([shift]),
            params={'shift': Offset, 'shift.offset': Offset},
            outputs={'value': Shifted},
        )


@pytest.mark.parametrize(
    ('fixed', 'blanks'),
    [
        ({'speed': 2.0}, ()),
        ({'run': {'loaded': 10.0, 'speed': 2.0}}, ()),
        ({'run': {'loaded': 10.0}}, ('speed',)),
    ],
)
def test_an_unknown_parameter_is_refused(
    binding: PipelineBinding, fixed: dict, blanks: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match='no sciline key'):
        binding.stage(fixed, blanks)


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


@pytest.fixture
def loaded() -> list[list[float]]:
    return []


@pytest.fixture
def numerators() -> list[weakref.ref]:
    return []


@pytest.fixture
def sans(loaded: list[list[float]], numerators: list[weakref.ref]) -> sciline.Pipeline:
    """
    A toy SANS reduction of one sample run and one can run, if any.

    The denominators are arrays of one element, so that adding into a value
    that a stage holds would change the next call.
    """

    def load_sample(run: SampleFile) -> SampleCounts:
        loaded.append(run)
        return SampleCounts(np.asarray(run, dtype=float))

    def load_can(run: CanFile) -> CanCounts:
        if run is None:  # no can run counts nothing
            return CanCounts(np.zeros(0))
        loaded.append(run)
        return CanCounts(np.asarray(run, dtype=float))

    def sample_numerator(counts: SampleCounts, bins: Bins) -> SampleNumerator:
        return SampleNumerator(counts.reshape(bins, -1).sum(axis=1))

    def sample_denominator(counts: SampleCounts) -> SampleDenominator:
        return SampleDenominator(counts.sum(keepdims=True))

    def can_numerator(counts: CanCounts, bins: Bins) -> CanNumerator:
        numerator = CanNumerator(counts.reshape(bins, -1).sum(axis=1))
        numerators.append(weakref.ref(numerator))
        return numerator

    def can_denominator(counts: CanCounts) -> CanDenominator:
        return CanDenominator(counts.sum(keepdims=True))

    def sample_iofq(num: SampleNumerator, den: SampleDenominator) -> SampleIofQ:
        return SampleIofQ(num / den)

    def iofq(sample: SampleIofQ, num: CanNumerator, den: CanDenominator) -> IofQ:
        # a can that counts nothing subtracts nothing
        return IofQ(sample - np.divide(num, den, out=np.zeros_like(num), where=den > 0))

    providers = [load_sample, load_can, sample_numerator, sample_denominator]
    providers += [can_numerator, can_denominator, sample_iofq, iofq]
    return sciline.Pipeline(providers)


OUTPUTS = {'sample': SampleIofQ, 'iofq': IofQ}
PARAMS = {
    'sample_runs.file': SampleFile,
    'can_runs.file': CanFile,
    'reduction.bins': Bins,
}
ACCUMULATE = {
    'sample_runs': (SampleNumerator, SampleDenominator),
    'can_runs': (CanNumerator, CanDenominator),
}


@pytest.fixture
def multi(sans: sciline.Pipeline) -> AccumulatingPipelineBinding:
    return AccumulatingPipelineBinding(
        sans, params=PARAMS, outputs=OUTPUTS, accumulate=ACCUMULATE
    )


@pytest.fixture
def single(sans: sciline.Pipeline) -> PipelineBinding:
    params = {'sample.file': SampleFile, 'can.file': CanFile, 'reduction.bins': Bins}
    return PipelineBinding(sans, params=params, outputs=OUTPUTS)


SAMPLES = [[1.0, 3.0, 2.0, 6.0], [2.0, 2.0, 0.0, 4.0]]
CANS = [[1.0, 1.0, 1.0, 1.0], [0.0, 2.0, 2.0, 0.0]]
ROWS = {
    'sample_runs': [{'file': run} for run in SAMPLES],
    'can_runs': [{'file': run} for run in CANS],
}


def _reduction(bins: int) -> dict[str, dict[str, int]]:
    return {'reduction': {'bins': bins}}


def test_the_binding_keeps_the_symmetries(
    single: PipelineBinding, multi: AccumulatingPipelineBinding
) -> None:
    one_row = {'sample_runs': ROWS['sample_runs'][:1], 'can_runs': ROWS['can_runs'][:1]}
    check_one_row(
        single,
        multi,
        {'sample': {'file': SAMPLES[0]}, 'can': {'file': CANS[0]}, **_reduction(2)},
        {**one_row, **_reduction(2)},
    )
    check_caching(multi, ROWS, [_reduction(2), _reduction(1), _reduction(2)])
    check_caching(multi, _reduction(2), [ROWS, one_row, ROWS])
    check_arrival_and_order(multi, _reduction(2), ROWS)


def test_an_empty_table_gives_what_a_group_set_to_none_gives(
    single: PipelineBinding, multi: AccumulatingPipelineBinding
) -> None:
    check_one_row(
        single,
        multi,
        {'sample': {'file': SAMPLES[0]}, 'can': None, **_reduction(2)},
        {'sample_runs': ROWS['sample_runs'][:1], 'can_runs': [], **_reduction(2)},
    )


def test_the_rows_of_one_table_may_be_fixed(
    multi: AccumulatingPipelineBinding,
) -> None:
    cans = {'can_runs': ROWS['can_runs']}
    samples = {'sample_runs': ROWS['sample_runs']}
    calls = [{**samples, **_reduction(n)} for n in (2, 1, 2)]
    check_caching(multi, cans, calls)
    check_arrival_and_order(multi, {**cans, **_reduction(2)}, samples)


def test_summing_the_rows_gives_the_reduction_of_the_summed_runs(
    single: PipelineBinding, multi: AccumulatingPipelineBinding
) -> None:
    summed = {
        'sample': {'file': list(np.sum(SAMPLES, axis=0))},
        'can': {'file': list(np.sum(CANS, axis=0))},
        **_reduction(2),
    }
    np.testing.assert_allclose(
        multi.stage({**ROWS, **_reduction(2)}, ())()['iofq'],
        single.stage(summed, ())()['iofq'],
    )


def test_a_stage_over_fixed_rows_loads_each_run_once(
    multi: AccumulatingPipelineBinding, loaded: list[list[float]]
) -> None:
    tune = multi.stage(ROWS, ('reduction',))
    for bins in (1, 2, 4):
        tune(**_reduction(bins))

    assert sorted(loaded) == sorted(SAMPLES + CANS)


def test_a_held_state_loads_each_run_once_across_reads(
    multi: AccumulatingPipelineBinding, loaded: list[list[float]]
) -> None:
    held = multi.held_state(_reduction(2))
    for row in ROWS['sample_runs']:
        held.push({'sample_runs': row})
        held.outputs()

    assert loaded == SAMPLES


def test_a_held_state_keeps_the_sums_of_fixed_rows_not_each_row(
    multi: AccumulatingPipelineBinding, numerators: list[weakref.ref]
) -> None:
    held = multi.held_state({'can_runs': ROWS['can_runs'], **_reduction(2)})
    held.push({'sample_runs': ROWS['sample_runs'][0]})
    held.outputs()
    gc.collect()

    assert len(numerators) == len(CANS)
    assert [n for n in numerators if n() is not None] == []


def test_a_table_mapped_as_a_whole_sets_its_key_to_each_row() -> None:
    def numerator(run: Run) -> Numerator:
        return Numerator(np.asarray(run['file'], dtype=float))

    binding = AccumulatingPipelineBinding(
        sciline.Pipeline([numerator]),
        params={'runs': Run},
        outputs={'total': Numerator},
        accumulate={'runs': (Numerator,)},
    )
    rows = [{'file': [1.0, 2.0]}, {'file': [3.0, 4.0]}]
    np.testing.assert_allclose(binding.stage({'runs': rows}, ())()['total'], [4, 6])


def test_a_stage_refuses_a_table_neither_fixed_nor_blank(
    multi: AccumulatingPipelineBinding,
) -> None:
    with pytest.raises(
        ValueError, match=r"no rows given for the tables \['can_runs'\]"
    ):
        multi.stage({'sample_runs': ROWS['sample_runs'], **_reduction(2)}, ())


def test_an_accumulated_key_that_depends_on_two_tables_is_refused(
    sans: sciline.Pipeline,
) -> None:
    def masked_can(counts: CanCounts, mask: SampleCounts, bins: Bins) -> CanNumerator:
        return CanNumerator((counts * (mask > 0)).reshape(bins, -1).sum(axis=1))

    sans.insert(masked_can)
    with pytest.raises(
        ValueError,
        match=r"CanNumerator.* of can_runs depends on the rows of "
        r"\['can_runs', 'sample_runs'\], not of can_runs alone",
    ):
        AccumulatingPipelineBinding(
            sans, params=PARAMS, outputs=OUTPUTS, accumulate=ACCUMULATE
        )


def test_an_accumulated_key_of_another_table_is_refused(
    sans: sciline.Pipeline,
) -> None:
    accumulate = {**ACCUMULATE, 'can_runs': (CanNumerator, SampleDenominator)}
    with pytest.raises(
        ValueError,
        match=r"SampleDenominator.* of can_runs depends on the rows of "
        r"\['sample_runs'\]",
    ):
        AccumulatingPipelineBinding(
            sans, params=PARAMS, outputs=OUTPUTS, accumulate=accumulate
        )


def test_an_output_that_reads_rows_other_than_through_sums_is_refused(
    sans: sciline.Pipeline,
) -> None:
    accumulate = {**ACCUMULATE, 'can_runs': (CanDenominator,)}
    with pytest.raises(
        ValueError, match=r"output iofq depends on the rows of \['can_runs'\]"
    ):
        AccumulatingPipelineBinding(
            sans, params=PARAMS, outputs=OUTPUTS, accumulate=accumulate
        )


def test_accumulated_keys_not_in_a_sequence_are_refused(
    sans: sciline.Pipeline,
) -> None:
    accumulate = {**ACCUMULATE, 'can_runs': CanNumerator}
    with pytest.raises(TypeError, match=r"accumulate\['can_runs'\]"):
        AccumulatingPipelineBinding(
            sans, params=PARAMS, outputs=OUTPUTS, accumulate=accumulate
        )


def test_a_row_field_without_a_sciline_key_is_refused(
    multi: AccumulatingPipelineBinding,
) -> None:
    held = multi.held_state(_reduction(2))
    with pytest.raises(
        ValueError, match=r"no sciline key for the parameters \['sample_runs.note'\]"
    ):
        held.push({'sample_runs': {'file': SAMPLES[0], 'note': 'x'}})

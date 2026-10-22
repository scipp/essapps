# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal, NewType

import numpy as np
import pytest
import sciline
from pydantic import BaseModel, Field

from ess.spec import DriverBinding, WorkflowSpec
from ess.spec.pipeline import PipelineBinding
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row

# A toy SANS reduction of a sample run and a can run, each a file of counts. The
# pipeline sets each field under its type as written, such as ``CanFile``.
# ``IofQDriver`` sums the numerator and the denominator of each table's runs,
# and computes the outputs from the sums.


class Reduction(BaseModel):
    bins: int = 2


SampleFile = NewType('SampleFile', Path)
CanFile = NewType('CanFile', Path)
SampleCounts = NewType('SampleCounts', np.ndarray)
CanCounts = NewType('CanCounts', np.ndarray)
SampleNumerator = NewType('SampleNumerator', np.ndarray)
SampleDenominator = NewType('SampleDenominator', np.ndarray)
CanNumerator = NewType('CanNumerator', np.ndarray)
CanDenominator = NewType('CanDenominator', np.ndarray)
SampleIofQ = NewType('SampleIofQ', np.ndarray)
CanIofQ = NewType('CanIofQ', np.ndarray)
IofQ = NewType('IofQ', np.ndarray)


class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: SampleFile
    can: CanFile


class IofQMultiParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: list[SampleFile] = Field(min_length=1)
    can: list[CanFile] = Field(min_length=1)


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ
    can_iofq: CanIofQ


def _spec(name: str, **fields: Any) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(name=name, version=1, title=name, description=name, **fields)


IOFQ = _spec('iofq', params=IofQParams, outputs=IofQOutputs)
IOFQ_MULTI = _spec(
    'iofq-multi', params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can')
)


def sample_numerator(counts: SampleCounts, reduction: Reduction) -> SampleNumerator:
    return SampleNumerator(counts.reshape(reduction.bins, -1).sum(axis=1))


def sample_denominator(counts: SampleCounts) -> SampleDenominator:
    """An array of one element, so that adding into a held value shows."""
    return SampleDenominator(counts.sum(keepdims=True))


def can_numerator(counts: CanCounts, reduction: Reduction) -> CanNumerator:
    return CanNumerator(counts.reshape(reduction.bins, -1).sum(axis=1))


def can_denominator(counts: CanCounts) -> CanDenominator:
    return CanDenominator(counts.sum(keepdims=True))


def sample_iofq(num: SampleNumerator, den: SampleDenominator) -> SampleIofQ:
    return SampleIofQ(num / den)


def can_iofq(num: CanNumerator, den: CanDenominator) -> CanIofQ:
    return CanIofQ(num / den)


def subtract_can(sample: SampleIofQ, can: CanIofQ) -> IofQ:
    return IofQ(sample - can)


MakePipeline = Callable[[Mapping[str, Any]], sciline.Pipeline]


@pytest.fixture
def loaded() -> list[Path]:
    return []


@pytest.fixture
def make_pipeline(loaded: list[Path]) -> MakePipeline:
    def load_sample(path: SampleFile) -> SampleCounts:
        loaded.append(path)
        return SampleCounts(np.loadtxt(path))

    def load_can(path: CanFile) -> CanCounts:
        loaded.append(path)
        return CanCounts(np.loadtxt(path))

    def make(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        sample = [load_sample, sample_numerator, sample_denominator, sample_iofq]
        can = [load_can, can_numerator, can_denominator, can_iofq]
        return sciline.Pipeline([*sample, *can, subtract_can])

    return make


SAMPLE_PARTS = (SampleNumerator, SampleDenominator)
CAN_PARTS = (CanNumerator, CanDenominator)


class IofQDriver:
    """
    A driver of ``IOFQ_MULTI``, made from the pipeline and keys of the
    single-run binding; refuses a state without a sample run or a can run.
    """

    def __init__(self, single: PipelineBinding, fixed: Mapping[str, Any]) -> None:
        keys = single.keys
        pipeline = single.pipeline({'reduction': fixed['reduction']})
        self._contribute = {
            table: (
                keys[table][0],
                sciline.Stage(pipeline, inputs=keys[table], outputs=parts),
            )
            for table, parts in (('sample', SAMPLE_PARTS), ('can', CAN_PARTS))
        }
        self._outputs = keys['iofq'] + keys['can_iofq']
        self._finalize = sciline.Stage(
            pipeline, inputs=SAMPLE_PARTS + CAN_PARTS, outputs=self._outputs
        )
        self._sums: dict[Any, np.ndarray] = {}

    def push(self, table: str, rows: Sequence[Any]) -> None:
        key, stage = self._contribute[table]
        for path in rows:
            for k, part in stage.compute({key: path}).items():
                self._sums[k] = self._sums[k] + part if k in self._sums else part

    def outputs(self) -> IofQOutputs:
        results = self._finalize.compute(self._sums)
        iofq, can_iofq = self._outputs
        return IofQOutputs(iofq=results[iofq], can_iofq=results[can_iofq])


@pytest.fixture
def single(make_pipeline: MakePipeline) -> PipelineBinding:
    return PipelineBinding(IOFQ, make_pipeline)


@pytest.fixture
def multi(single: PipelineBinding) -> DriverBinding:
    return DriverBinding(IOFQ_MULTI, lambda fixed: IofQDriver(single, fixed))


SAMPLES = [[1.0, 3.0, 2.0, 6.0], [2.0, 2.0, 0.0, 4.0]]
CANS = [[1.0, 1.0, 1.0, 1.0], [0.0, 2.0, 2.0, 0.0]]


def write(tmp_path: Path, name: str, counts: list[float]) -> Path:
    path = tmp_path / f'{name}.txt'
    np.savetxt(path, counts)
    return path


@pytest.fixture
def samples(tmp_path: Path) -> list[Path]:
    return [write(tmp_path, f'sample{i}', c) for i, c in enumerate(SAMPLES)]


@pytest.fixture
def cans(tmp_path: Path) -> list[Path]:
    return [write(tmp_path, f'can{i}', c) for i, c in enumerate(CANS)]


def reduction(bins: int) -> dict[str, Reduction]:
    return {'reduction': Reduction(bins=bins)}


def iofq(counts: list[float], bins: int = 2) -> np.ndarray:
    return np.reshape(counts, (bins, -1)).sum(axis=1) / np.sum(counts)


def test_a_plain_request_computes_the_outputs_model(
    single: PipelineBinding, samples: list[Path], cans: list[Path]
) -> None:
    outputs = single.compute(IofQParams(sample=samples[0], can=cans[0]))
    assert isinstance(outputs, IofQOutputs)
    np.testing.assert_allclose(outputs.iofq, iofq(SAMPLES[0]) - iofq(CANS[0]))
    np.testing.assert_allclose(outputs.can_iofq, iofq(CANS[0]))


def test_make_pipeline_gets_the_fixed_values_with_defaults_and_no_blank(
    make_pipeline: MakePipeline, samples: list[Path]
) -> None:
    made: list[Mapping[str, Any]] = []

    def recording(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        made.append(fixed)
        return make_pipeline(fixed)

    PipelineBinding(IOFQ, recording).make_stage({'sample': samples[0]}, ('can',))
    assert made == [{'sample': samples[0], 'reduction': Reduction()}]


def test_a_stage_loads_each_run_once(
    single: PipelineBinding, samples: list[Path], cans: list[Path], loaded: list[Path]
) -> None:
    tune = single.make_stage({'sample': samples[0], 'can': cans[0]}, ('reduction',))
    for bins in (1, 2, 4):
        np.testing.assert_allclose(
            tune(reduction(bins)).iofq, iofq(SAMPLES[0], bins) - iofq(CANS[0], bins)
        )
    assert sorted(loaded) == sorted([samples[0], cans[0]])


def test_a_stage_call_takes_the_blanks_exactly(
    single: PipelineBinding, samples: list[Path], cans: list[Path]
) -> None:
    tune = single.make_stage({'sample': samples[0], 'can': cans[0]}, ('reduction',))
    with pytest.raises(ValueError, match=r"give the blanks \['reduction'\]"):
        tune({})


def test_a_stage_refuses_values_that_do_not_fit(single: PipelineBinding) -> None:
    with pytest.raises(
        ValueError,
        match=r'^sample: a required parameter, not given; can: a required parameter',
    ):
        single.make_stage({}, ('reduction',))


def test_the_bindings_keep_the_symmetries(
    single: PipelineBinding,
    multi: DriverBinding,
    samples: list[Path],
    cans: list[Path],
) -> None:
    check_one_row(single, multi, IofQParams(sample=samples[0], can=cans[0]))
    runs = {'sample': samples[0], 'can': cans[0]}
    check_caching(single, runs, [reduction(2), reduction(1), reduction(2)])
    tables = {'sample': samples, 'can': cans}
    check_arrival_and_order(multi.make_driver, reduction(2), tables)


def test_summing_the_rows_gives_the_reduction_of_the_summed_runs(
    single: PipelineBinding,
    multi: DriverBinding,
    samples: list[Path],
    cans: list[Path],
    tmp_path: Path,
) -> None:
    summed = IofQParams(
        sample=write(tmp_path, 'samples', np.sum(SAMPLES, axis=0)),
        can=write(tmp_path, 'cans', np.sum(CANS, axis=0)),
    )
    rows = IOFQ_MULTI.params(sample=samples, can=cans)
    np.testing.assert_allclose(multi.compute(rows).iofq, single.compute(summed).iofq)


def test_a_driver_with_fixed_rows_loads_each_run_once_across_reads(
    multi: DriverBinding, samples: list[Path], cans: list[Path], loaded: list[Path]
) -> None:
    driver = multi.make_accumulator({'can': cans, **reduction(2)}, ('sample',))
    for sample in samples:
        driver.push('sample', [sample])
        driver.outputs()
    assert loaded == cans + samples


def test_keys_give_the_key_of_each_field(single: PipelineBinding) -> None:
    assert dict(single.keys) == {
        'reduction': (Reduction,),
        'sample': (SampleFile,),
        'can': (CanFile,),
        'iofq': (IofQ,),
        'can_iofq': (CanIofQ,),
    }
    with pytest.raises(TypeError):
        single.keys['iofq'] = (SampleIofQ,)  # type: ignore[index]


def test_pipeline_sets_each_fixed_value_at_its_key(
    single: PipelineBinding, samples: list[Path], cans: list[Path]
) -> None:
    pipeline = single.pipeline({**reduction(4), 'sample': samples[0], 'can': cans[0]})
    np.testing.assert_allclose(
        pipeline.compute(IofQ), iofq(SAMPLES[0], 4) - iofq(CANS[0], 4)
    )
    with pytest.raises(ValueError, match=r"\['iofq'\]: not parameters of"):
        single.pipeline({'iofq': None})


# Keys of a workflow's own: the pipeline reads a ``Filename`` and computes a
# ``Total``, where the spec has a ``SampleFile`` and a ``SampleTotal``.

Filename = NewType('Filename', Path)
Counts = NewType('Counts', np.ndarray)
Total = NewType('Total', float)
SampleTotal = NewType('SampleTotal', float)
SampleMean = NewType('SampleMean', float)


class TotalMultiParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: list[SampleFile] = Field(min_length=1)


class TotalOutputs(BaseModel):
    total: SampleTotal


TOTAL = _spec('total', params=IofQParams, outputs=TotalOutputs)
TOTAL_MULTI = _spec(
    'total-multi', params=TotalMultiParams, outputs=TotalOutputs, tables=('sample',)
)


def load(filename: Filename) -> Counts:
    return Counts(np.loadtxt(filename))


def total(counts: Counts, reduction: Reduction) -> Total:
    return Total(reduction.bins * counts.sum())


def test_keys_name_keys_of_the_workflows_own(
    samples: list[Path], cans: list[Path]
) -> None:
    binding = PipelineBinding(
        TOTAL,
        lambda _: sciline.Pipeline([load, total]),
        keys={'sample': (Filename,), 'total': (Total,)},
    )
    outputs = binding.compute(IofQParams(sample=samples[0], can=cans[0]))
    assert outputs == TotalOutputs(total=2 * sum(SAMPLES[0]))


class TotalAndMeanOutputs(BaseModel):
    total: SampleTotal
    mean: SampleMean | None = None


def mean(counts: Counts) -> SampleMean:
    return SampleMean(counts.mean())


@pytest.mark.parametrize(
    ('providers', 'expected_mean'), [([load, total], None), ([load, total, mean], 3.0)]
)
def test_an_optional_output_is_none_where_the_pipeline_has_no_provider(
    samples: list[Path],
    cans: list[Path],
    providers: list[Callable[..., Any]],
    expected_mean: float | None,
) -> None:
    spec = _spec('total-and-mean', params=IofQParams, outputs=TotalAndMeanOutputs)
    binding = PipelineBinding(
        spec,
        lambda _: sciline.Pipeline(providers),
        keys={'sample': (Filename,), 'total': (Total,)},
    )
    outputs = binding.compute(IofQParams(sample=samples[0], can=cans[0]))
    assert outputs == TotalAndMeanOutputs(total=2 * 12.0, mean=expected_mean)


def test_a_table_sets_the_key_of_its_type_to_its_list(samples: list[Path]) -> None:
    def load_all(paths: list[SampleFile]) -> Counts:
        return Counts(sum(np.loadtxt(path) for path in paths))

    binding = PipelineBinding(
        TOTAL_MULTI,
        lambda _: sciline.Pipeline([load_all, total]),
        keys={'total': (Total,)},
    )
    expected = TotalOutputs(total=2 * np.sum(SAMPLES))
    params = TOTAL_MULTI.params(sample=samples)
    assert binding.compute(params) == expected
    stage = binding.make_stage({}, ('sample',))
    assert stage({'sample': samples}) == expected
    check_caching(binding, {}, [{'sample': samples}, {'sample': samples[:1]}])


def test_a_field_without_a_key_is_for_make_pipeline_alone(samples: list[Path]) -> None:
    def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        counts = sum(load(path) for path in fixed['sample'])
        return sciline.Pipeline([total], params={Counts: counts})

    binding = PipelineBinding(
        TOTAL_MULTI, make_pipeline, keys={'sample': (), 'total': (Total,)}
    )
    params = TOTAL_MULTI.params(sample=samples)
    assert binding.compute(params) == TotalOutputs(total=2 * np.sum(SAMPLES))
    with pytest.raises(ValueError, match=r"\['sample'\]: without a key, cannot be"):
        binding.make_stage({}, ('sample',))


@pytest.mark.parametrize(
    ('keys', 'reason'),
    [
        ({'speed': (Total,)}, r"\['speed'\]: not fields of total"),
        ({'total': ()}, r"\['total'\]: an output is computed as one key"),
        ({'total': (Total, Counts)}, r"\['total'\]: an output is computed as one key"),
        ({'sample': (Filename, Counts)}, r"\['sample'\]: a parameter sets one key, or"),
        ({'sample': (CanFile,)}, 'total: sample and can have the key CanFile; no two'),
        (
            {'sample': (Filename,), 'total': (Filename,)},
            'total: sample and total have the key Filename; no two fields share',
        ),
    ],
)
def test_keys_that_do_not_fit_the_spec_are_refused(
    keys: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        PipelineBinding(TOTAL, lambda _: sciline.Pipeline([load, total]), keys=keys)


class TwoFiles(BaseModel):
    first: SampleFile
    second: SampleFile


def test_two_parameters_of_one_type_need_a_key_each() -> None:
    spec = _spec('twins', params=TwoFiles, outputs=TotalOutputs)
    with pytest.raises(
        ValueError,
        match='twins: first and second have the key SampleFile; no two fields share',
    ):
        PipelineBinding(spec, lambda _: sciline.Pipeline([load, total]))


def test_keys_are_given_as_tuples() -> None:
    with pytest.raises(TypeError, match=r"\['total'\]: keys= gives a tuple of keys"):
        PipelineBinding(
            TOTAL, lambda _: sciline.Pipeline([load, total]), keys={'total': Total}
        )


Offset = NewType('Offset', float)
Shifted = NewType('Shifted', float)


class OffsetParams(BaseModel):
    offset: Offset | None = None


class ShiftedOutputs(BaseModel):
    shifted: Shifted


SHIFTED = _spec('shifted', params=OffsetParams, outputs=ShiftedOutputs)


def shift(offset: Offset) -> Shifted:
    return Shifted(offset + 1.0)


@pytest.mark.parametrize('keys', [None, {'offset': (Offset,)}])
def test_an_optional_parameter_with_a_key_is_refused(
    keys: dict[str, Any] | None,
) -> None:
    with pytest.raises(
        ValueError, match=r"shifted: \['offset'\]: a parameter typed X \| None"
    ):
        PipelineBinding(SHIFTED, lambda _: sciline.Pipeline([shift]), keys=keys)


def test_an_optional_parameter_without_a_key_reaches_make_pipeline() -> None:
    def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        pipeline = sciline.Pipeline([shift])
        if fixed['offset'] is None:
            pipeline[Shifted] = Shifted(-1.0)
        else:
            pipeline[Offset] = fixed['offset']
        return pipeline

    binding = PipelineBinding(SHIFTED, make_pipeline, keys={'offset': ()})
    assert binding.keys['offset'] == ()
    assert binding.compute(OffsetParams(offset=2.0)).shifted == 3.0
    assert binding.compute(OffsetParams()).shifted == -1.0


class RequiredOffsetParams(BaseModel):
    offset: Offset


def test_make_pipeline_may_return_a_pipeline_it_holds() -> None:
    spec = _spec('shifted', params=RequiredOffsetParams, outputs=ShiftedOutputs)
    base = sciline.Pipeline([shift])
    binding = PipelineBinding(spec, lambda _: base)
    assert binding.compute(RequiredOffsetParams(offset=2.0)).shifted == 3.0
    assert binding.compute(RequiredOffsetParams(offset=5.0)).shifted == 6.0
    with pytest.raises(sciline.UnsatisfiedRequirement):
        base.compute(Shifted)


# A beam centre, given or found from the sample run. ``find_centre`` leads from
# the key of one alternative, ``FindCentre``, to that of the other,
# ``BeamCentre``, so one graph serves both. The output ``centre`` reports the
# centre given or found, at a key of its own.


class BeamCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    tolerance: float = 0.5


class RefinedCentre(BeamCentre):
    """A subclass of an alternative, whose instance passes the union unchanged."""

    sigma: float = 1.0


Centre = Annotated[BeamCentre | FindCentre, Field(discriminator='kind')]
Centred = NewType('Centred', float)
UsedCentre = NewType('UsedCentre', BeamCentre)


class CentredParams(BaseModel):
    sample: SampleFile
    beam_centre: Centre = BeamCentre()


class CentredOutputs(BaseModel):
    centred: Centred
    centre: UsedCentre


CENTRED = _spec('centred', params=CentredParams, outputs=CentredOutputs)


def load_counts(path: SampleFile) -> SampleCounts:
    return SampleCounts(np.loadtxt(path))


def centred(counts: SampleCounts, centre: BeamCentre) -> Centred:
    return Centred(counts.sum() - centre.x)


def used_centre(centre: BeamCentre) -> UsedCentre:
    return UsedCentre(centre.model_copy())


@pytest.fixture
def found() -> list[FindCentre]:
    return []


@pytest.fixture
def find_centre(found: list[FindCentre]) -> Callable[..., BeamCentre]:
    def find_centre(settings: FindCentre, counts: SampleCounts) -> BeamCentre:
        found.append(settings)
        return BeamCentre(x=counts.mean() * settings.tolerance)

    return find_centre


@pytest.fixture
def centring(find_centre: Callable[..., BeamCentre]) -> PipelineBinding:
    return PipelineBinding(
        CENTRED,
        lambda _: sciline.Pipeline([load_counts, find_centre, centred, used_centre]),
    )


def test_one_graph_serves_both_alternatives_of_a_union(
    centring: PipelineBinding, samples: list[Path], found: list[FindCentre]
) -> None:
    assert centring.keys['beam_centre'] == (BeamCentre, FindCentre)
    total, mean = np.sum(SAMPLES[0]), np.mean(SAMPLES[0])

    given = centring.compute(
        CentredParams(sample=samples[0], beam_centre=BeamCentre(x=1.0))
    )
    assert given == CentredOutputs(centred=total - 1.0, centre=BeamCentre(x=1.0))
    assert found == []

    settings = FindCentre(tolerance=0.5)
    find = centring.compute(CentredParams(sample=samples[0], beam_centre=settings))
    centre = BeamCentre(x=0.5 * mean)
    assert find == CentredOutputs(centred=total - 0.5 * mean, centre=centre)
    assert found == [settings]


def test_a_union_left_out_takes_the_key_of_its_default(
    centring: PipelineBinding, samples: list[Path], found: list[FindCentre]
) -> None:
    outputs = centring.compute(CentredParams(sample=samples[0]))
    assert outputs.centre == BeamCentre()
    assert found == []


def test_the_key_of_a_union_is_chosen_by_tag_not_by_type(
    centring: PipelineBinding, samples: list[Path]
) -> None:
    refined = RefinedCentre(x=2.0)
    params = CentredParams(sample=samples[0], beam_centre=refined)
    assert params.beam_centre is refined
    assert centring.compute(params).centred == np.sum(SAMPLES[0]) - 2.0


def test_a_union_cannot_be_a_blank(
    centring: PipelineBinding, samples: list[Path]
) -> None:
    with pytest.raises(
        ValueError, match=r"\['beam_centre'\]: a union cannot be a blank"
    ):
        centring.make_stage({'sample': samples[0]}, ('beam_centre',))


@pytest.mark.parametrize('centre', [BeamCentre(x=1.0), FindCentre(tolerance=0.5)])
def test_a_stage_with_a_union_fixed_keeps_the_symmetries(
    centring: PipelineBinding,
    samples: list[Path],
    centre: BeamCentre | FindCentre,
) -> None:
    # The blank sample is upstream of find_centre.
    calls = [{'sample': samples[0]}, {'sample': samples[1]}, {'sample': samples[0]}]
    check_caching(centring, {'beam_centre': centre}, calls)


@pytest.mark.parametrize('centre', [BeamCentre(x=1.0), FindCentre(tolerance=0.5)])
@pytest.mark.parametrize('held', [BeamCentre, FindCentre])
def test_a_value_held_at_a_key_of_a_union_is_refused(
    find_centre: Callable[..., BeamCentre],
    samples: list[Path],
    centre: BeamCentre | FindCentre,
    held: type[BaseModel],
) -> None:
    # A held BeamCentre would replace find_centre, so the tag 'find' would set
    # FindCentre without effect. A held FindCentre is harmless, but the
    # binding cannot tell the two apart, so it refuses either, for any tag.
    binding = PipelineBinding(
        CENTRED,
        lambda _: sciline.Pipeline(
            [load_counts, find_centre, centred, used_centre],
            params={held: held()},
        ),
    )
    params = CentredParams(sample=samples[0], beam_centre=centre)
    with pytest.raises(
        ValueError,
        match=rf'centred: make_pipeline holds a value at {held.__name__}, a key '
        'of the union beam_centre',
    ):
        binding.compute(params)


@pytest.mark.parametrize(
    ('keys', 'reason'),
    [
        (
            {'beam_centre': (BeamCentre,)},
            r"\['beam_centre'\]: a parameter sets one key, or none; a union one "
            'for each alternative',
        ),
        (
            {'centred': (BeamCentre,)},
            'centred: beam_centre and centred have the key BeamCentre; no two',
        ),
    ],
)
def test_keys_of_a_union_that_do_not_fit_are_refused(
    keys: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        PipelineBinding(CENTRED, lambda _: sciline.Pipeline([centred]), keys=keys)


CentreSetting = NewType('CentreSetting', BaseModel)


def centred_by_setting(counts: SampleCounts, setting: CentreSetting) -> Centred:
    if isinstance(setting, BeamCentre):
        return Centred(counts.sum() - setting.x)
    return Centred(counts.sum() - counts.mean() * setting.tolerance)


def test_a_union_may_set_one_key_for_both_alternatives(samples: list[Path]) -> None:
    class CentredOnly(BaseModel):
        centred: Centred

    binding = PipelineBinding(
        _spec('centred', params=CentredParams, outputs=CentredOnly),
        lambda _: sciline.Pipeline([load_counts, centred_by_setting]),
        keys={'beam_centre': (CentreSetting, CentreSetting)},
    )
    total, mean = np.sum(SAMPLES[0]), np.mean(SAMPLES[0])

    given = CentredParams(sample=samples[0], beam_centre=BeamCentre(x=1.0))
    find = CentredParams(sample=samples[0], beam_centre=FindCentre(tolerance=0.5))
    assert binding.compute(given).centred == total - 1.0
    assert binding.compute(find).centred == total - 0.5 * mean


def test_a_value_held_at_the_one_key_of_a_union_is_set_over(
    samples: list[Path],
) -> None:
    class CentredOnly(BaseModel):
        centred: Centred

    binding = PipelineBinding(
        _spec('centred', params=CentredParams, outputs=CentredOnly),
        lambda _: sciline.Pipeline(
            [load_counts, centred_by_setting], params={CentreSetting: BeamCentre()}
        ),
        keys={'beam_centre': (CentreSetting, CentreSetting)},
    )
    total, mean = np.sum(SAMPLES[0]), np.mean(SAMPLES[0])
    given = CentredParams(sample=samples[0], beam_centre=BeamCentre(x=1.0))
    find = CentredParams(sample=samples[0], beam_centre=FindCentre(tolerance=0.5))
    assert binding.compute(given).centred == total - 1.0
    assert binding.compute(find).centred == total - 0.5 * mean


def test_an_optional_union_with_a_key_is_refused() -> None:
    class OptionalCentreParams(BaseModel):
        beam_centre: Centre | None = None

    spec = _spec('centred', params=OptionalCentreParams, outputs=CentredOutputs)
    with pytest.raises(
        ValueError, match=r"\['beam_centre'\]: a parameter typed X \| None sets no"
    ):
        PipelineBinding(spec, lambda _: sciline.Pipeline([centred]))
    keyless = PipelineBinding(
        spec, lambda _: sciline.Pipeline([centred]), keys={'beam_centre': ()}
    )
    assert keyless.keys['beam_centre'] == ()


def test_a_list_of_unions_sets_the_key_of_its_type() -> None:
    class CentresParams(BaseModel):
        centres: list[Centre] = []

    spec = _spec('centres', params=CentresParams, outputs=CentredOutputs)
    binding = PipelineBinding(spec, lambda _: sciline.Pipeline([centred]))
    assert binding.keys['centres'] == (list[BeamCentre | FindCentre],)


# Two union parameters of one sample run: the centre of the sample and that of
# the can, each told apart by ``NewType`` alternatives, as two parameters of
# one plain type are.

SampleCentre = NewType('SampleCentre', BeamCentre)
FindSampleCentre = NewType('FindSampleCentre', FindCentre)
CanCentre = NewType('CanCentre', BeamCentre)
FindCanCentre = NewType('FindCanCentre', FindCentre)
Shift = NewType('Shift', float)


class TwoCentresParams(BaseModel):
    sample: SampleFile
    sample_centre: Annotated[
        SampleCentre | FindSampleCentre, Field(discriminator='kind')
    ] = BeamCentre()
    can_centre: Annotated[CanCentre | FindCanCentre, Field(discriminator='kind')] = (
        BeamCentre()
    )


class SameCentresParams(BaseModel):
    sample: SampleFile
    sample_centre: Centre = BeamCentre()
    can_centre: Centre = BeamCentre()


class ShiftOutputs(BaseModel):
    shift: Shift


def find_sample_centre(
    settings: FindSampleCentre, counts: SampleCounts
) -> SampleCentre:
    return SampleCentre(BeamCentre(x=counts.mean() * settings.tolerance))


def find_can_centre(settings: FindCanCentre, counts: SampleCounts) -> CanCentre:
    return CanCentre(BeamCentre(x=-counts.mean() * settings.tolerance))


def shift_between(sample: SampleCentre, can: CanCentre) -> Shift:
    return Shift(sample.x - can.x)


NEWTYPE_KEYS = {
    'sample_centre': (SampleCentre, FindSampleCentre),
    'can_centre': (CanCentre, FindCanCentre),
}


@pytest.mark.parametrize(
    ('params', 'keys'), [(TwoCentresParams, None), (SameCentresParams, NEWTYPE_KEYS)]
)
def test_union_parameters_are_told_apart_by_newtype_alternatives(
    samples: list[Path], params: type[BaseModel], keys: dict[str, Any] | None
) -> None:
    spec = _spec('shift', params=params, outputs=ShiftOutputs)
    providers = [load_counts, find_sample_centre, find_can_centre, shift_between]
    binding = PipelineBinding(spec, lambda _: sciline.Pipeline(providers), keys=keys)
    assert {n: binding.keys[n] for n in NEWTYPE_KEYS} == NEWTYPE_KEYS

    request = params(
        sample=samples[0],
        sample_centre=FindCentre(tolerance=0.5),
        can_centre=BeamCentre(x=1.0),
    )
    assert binding.compute(request).shift == 0.5 * np.mean(SAMPLES[0]) - 1.0


def test_two_union_parameters_of_one_type_need_keys() -> None:
    spec = _spec('shift', params=SameCentresParams, outputs=ShiftOutputs)
    with pytest.raises(
        ValueError,
        match='shift: sample_centre and can_centre have the key BeamCentre; ',
    ):
        PipelineBinding(spec, lambda _: sciline.Pipeline([shift_between]))

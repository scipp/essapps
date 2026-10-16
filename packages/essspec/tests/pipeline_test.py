# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NewType

import numpy as np
import pytest
import sciline
from pydantic import BaseModel, Field

from ess.spec import DriverBinding, WorkflowSpec
from ess.spec.pipeline import PipelineBinding
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row

# A toy SANS reduction of a sample run and an optional can run, each a file of
# counts. The pipeline sets each field under its type as written, such as
# ``CanFile | None``. ``IofQDriver`` sums the numerator and the denominator of
# each table's runs and computes the outputs from the sums.


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
    can: CanFile | None = None


class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ
    can_iofq: CanIofQ | None = None


def _spec(name: str, **models: type[BaseModel]) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(name=name, version=1, title=name, description=name, **models)


IOFQ = _spec('iofq', params=IofQParams, outputs=IofQOutputs)
IOFQ_MULTI = IOFQ.tabulate(name='iofq-multi', version=1, tables=('sample', 'can'))


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


def without_can(sample: SampleIofQ) -> IofQ:
    return IofQ(sample.copy())


MakePipeline = Callable[[Mapping[str, Any]], sciline.Pipeline]


@pytest.fixture
def loaded() -> list[Path]:
    return []


@pytest.fixture
def make_pipeline(loaded: list[Path]) -> MakePipeline:
    """Makes the graph with a can unless the can is fixed to ``None``."""

    def load_sample(path: SampleFile) -> SampleCounts:
        loaded.append(path)
        return SampleCounts(np.loadtxt(path))

    def load_can(path: CanFile | None) -> CanCounts:
        loaded.append(path)
        return CanCounts(np.loadtxt(path))

    def make(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        providers = [load_sample, sample_numerator, sample_denominator, sample_iofq]
        if 'can' in fixed and fixed['can'] is None:
            providers.append(without_can)
        else:
            providers += [load_can, can_numerator, can_denominator, can_iofq]
            providers.append(subtract_can)
        return sciline.Pipeline(providers)

    return make


SAMPLE_PARTS = (SampleNumerator, SampleDenominator)
CAN_PARTS = (CanNumerator, CanDenominator)


class IofQDriver:
    """
    A driver of ``IOFQ_MULTI``, made from the pipelines and keys of the
    single-run binding; refuses a state without a sample run.
    """

    def __init__(self, single: PipelineBinding, fixed: Mapping[str, Any]) -> None:
        keys = single.keys
        with_can = single.pipeline({'reduction': fixed['reduction']})
        without = single.pipeline({'reduction': fixed['reduction'], 'can': None})
        self._contribute = {
            table: (
                keys[table],
                sciline.Stage(with_can, inputs=(keys[table],), outputs=parts),
            )
            for table, parts in (('sample', SAMPLE_PARTS), ('can', CAN_PARTS))
        }
        self._outputs = (keys['iofq'], keys['can_iofq'])
        self._finalize = {
            True: sciline.Stage(
                with_can, inputs=SAMPLE_PARTS + CAN_PARTS, outputs=self._outputs
            ),
            False: sciline.Stage(
                without, inputs=SAMPLE_PARTS, outputs=self._outputs[:1]
            ),
        }
        self._sums: dict[Any, np.ndarray] = {}

    def push(self, table: str, columns: Mapping[str, Any]) -> None:
        key, stage = self._contribute[table]
        for path in columns[table]:
            for k, part in stage.compute({key: path}).items():
                self._sums[k] = self._sums[k] + part if k in self._sums else part

    def outputs(self) -> IofQOutputs:
        results = self._finalize[CanNumerator in self._sums].compute(self._sums)
        iofq, can_iofq = self._outputs
        return IofQOutputs(iofq=results[iofq], can_iofq=results.get(can_iofq))


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


def test_an_optional_output_is_none_where_the_pipeline_has_no_provider(
    single: PipelineBinding, samples: list[Path]
) -> None:
    outputs = single.compute(IofQParams(sample=samples[0]))
    np.testing.assert_allclose(outputs.iofq, iofq(SAMPLES[0]))
    assert outputs.can_iofq is None


def test_make_pipeline_gets_the_fixed_values_with_defaults_and_no_blank(
    make_pipeline: MakePipeline, samples: list[Path]
) -> None:
    made: list[Mapping[str, Any]] = []

    def recording(fixed: Mapping[str, Any]) -> sciline.Pipeline:
        made.append(fixed)
        return make_pipeline(fixed)

    PipelineBinding(IOFQ, recording).make_stage({'sample': samples[0]}, ('reduction',))
    assert made == [{'sample': samples[0], 'can': None}]


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
    single: PipelineBinding, samples: list[Path]
) -> None:
    tune = single.make_stage({'sample': samples[0]}, ('reduction',))
    with pytest.raises(ValueError, match=r"give the blanks \['reduction'\]"):
        tune({})


def test_a_stage_refuses_values_that_do_not_fit(single: PipelineBinding) -> None:
    with pytest.raises(
        ValueError, match=r"\['sample'\]: required parameters, not given"
    ):
        single.make_stage({}, ('reduction',))


def test_the_bindings_keep_the_symmetries(
    single: PipelineBinding,
    multi: DriverBinding,
    samples: list[Path],
    cans: list[Path],
) -> None:
    for can in (cans[0], None):
        check_one_row(single, multi, IofQParams(sample=samples[0], can=can))
    runs = {'sample': samples[0], 'can': cans[0]}
    check_caching(single, runs, [reduction(2), reduction(1), reduction(2)])
    tables = {'sample': {'sample': samples}, 'can': {'can': cans}}
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
        driver.push('sample', {'sample': [sample]})
        driver.outputs()
    assert loaded == cans + samples


def test_keys_give_the_key_of_each_field(single: PipelineBinding) -> None:
    assert dict(single.keys) == {
        'reduction': Reduction,
        'sample': SampleFile,
        'can': CanFile | None,
        'iofq': IofQ,
        'can_iofq': CanIofQ,
    }
    with pytest.raises(TypeError):
        single.keys['iofq'] = SampleIofQ  # type: ignore[index]


def test_pipeline_sets_each_fixed_value_at_its_key(
    single: PipelineBinding, samples: list[Path]
) -> None:
    pipeline = single.pipeline({**reduction(4), 'sample': samples[0], 'can': None})
    np.testing.assert_allclose(pipeline.compute(IofQ), iofq(SAMPLES[0], 4))
    with pytest.raises(ValueError, match=r"\['iofq'\]: not parameters of"):
        single.pipeline({'iofq': None})


# Keys of a workflow's own: the pipeline reads a ``Filename`` and computes a
# ``Total``, where the spec has a ``SampleFile`` and a ``SampleTotal``.

Filename = NewType('Filename', Path)
Counts = NewType('Counts', np.ndarray)
Total = NewType('Total', float)
SampleTotal = NewType('SampleTotal', float)


class TotalOutputs(BaseModel):
    total: SampleTotal


TOTAL = _spec('total', params=IofQParams, outputs=TotalOutputs)
TOTAL_MULTI = TOTAL.tabulate(name='total-multi', version=1, tables=('sample',))


def load(filename: Filename) -> Counts:
    return Counts(np.loadtxt(filename))


def total(counts: Counts, reduction: Reduction) -> Total:
    return Total(reduction.bins * counts.sum())


def test_keys_name_keys_of_the_workflows_own(samples: list[Path]) -> None:
    binding = PipelineBinding(
        TOTAL,
        lambda _: sciline.Pipeline([load, total]),
        keys={'sample': Filename, 'total': Total},
    )
    outputs = binding.compute(IofQParams(sample=samples[0]))
    assert outputs == TotalOutputs(total=2 * sum(SAMPLES[0]))


def test_a_table_sets_the_key_of_each_column_to_its_list(samples: list[Path]) -> None:
    def load_all(paths: list[SampleFile]) -> Counts:
        return Counts(sum(np.loadtxt(path) for path in paths))

    binding = PipelineBinding(
        TOTAL_MULTI,
        lambda _: sciline.Pipeline([load_all, total]),
        keys={'total': Total},
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
        TOTAL_MULTI, make_pipeline, keys={'sample': None, 'total': Total}
    )
    params = TOTAL_MULTI.params(sample=samples)
    assert binding.compute(params) == TotalOutputs(total=2 * np.sum(SAMPLES))
    with pytest.raises(ValueError, match=r"\['sample'\]: without a key, cannot be"):
        binding.make_stage({}, ('sample',))


@pytest.mark.parametrize(
    ('keys', 'reason'),
    [
        ({'speed': Total}, r"\['speed'\]: not fields of total"),
        ({'total': None}, r"\['total'\]: an output is computed as a key"),
        ({'sample': CanFile | None}, 'total: sample and can: two fields have one key'),
        (
            {'sample': Filename, 'total': Filename},
            'total: sample and total: two fields have one key',
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
        ValueError, match='twins: first and second: two fields have one key'
    ):
        PipelineBinding(spec, lambda _: sciline.Pipeline([load, total]))

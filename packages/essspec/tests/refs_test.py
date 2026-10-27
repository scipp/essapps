# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pydantic
import pytest

from ess.spec import AccumulatorRef, DatasetModel, DatasetRef, OutputRef
from ess.spec.refs import ParamsError, as_ref, iter_refs, loc_text, map_refs

OUTPUT_REF = {'@ref': 'output', 'record': 'r1', 'output': 'data'}
DATASET_REF = {'@ref': 'dataset', 'dataset': 'pid-1'}
ACCUMULATOR_REF = {'@ref': 'accumulator', 'accumulator': 'a1', 'output': 'data'}


def test_references_print_what_they_name() -> None:
    assert str(OutputRef(record='r1', output='data')) == 'r1.data'
    assert str(OutputRef(record='r1', output='banks', key='a')) == 'r1.banks[a]'
    assert str(DatasetRef(dataset='run:dream/1')) == 'run:dream/1'
    assert str(AccumulatorRef(accumulator='a1', output='data')) == 'a1.data'


def test_dataset_identity_is_an_opaque_string() -> None:
    # The spec does not normalize: the framework that minted the identity is
    # the one that knows a run number and the PID minted from it are one
    # dataset.
    assert DatasetRef(dataset='pid:20.500.12269/abc') == DatasetRef(
        dataset='pid:20.500.12269/abc'
    )
    assert DatasetRef(dataset='run:dream/1') != DatasetRef(
        dataset='pid:20.500.12269/abc'
    )


PARAMS = {
    'sample': OUTPUT_REF,
    'runs': [DATASET_REF, None, {'@ref': 'output', 'record': 'f2', 'output': 'file'}],
    'can': None,
    'sum': AccumulatorRef(accumulator='a1', output='data'),
    'centre': 'not a reference',
    'rows': [{'file': DATASET_REF, 'shift': {'offset': OUTPUT_REF}}],
    'group': {'centre': OUTPUT_REF, 'x': 1.0},
    'deep': {'mask': {'by': [{'kind': 'given', 'data': ACCUMULATOR_REF}]}},
}


def test_iter_refs_gives_each_reference_of_the_params_at_any_depth() -> None:
    assert [(p, str(r)) for p, r in iter_refs(PARAMS)] == [
        (('sample',), 'r1.data'),
        (('runs', 0), 'pid-1'),
        (('runs', 2), 'f2.file'),
        (('sum',), 'a1.data'),
        (('rows', 0, 'file'), 'pid-1'),
        (('rows', 0, 'shift', 'offset'), 'r1.data'),
        (('group', 'centre'), 'r1.data'),
        (('deep', 'mask', 'by', 0, 'data'), 'a1.data'),
    ]


def test_loc_text_names_a_place_as_a_message_does() -> None:
    assert loc_text(('sample',)) == 'sample'
    assert loc_text(('runs', 2, 'transmission')) == 'runs[2].transmission'


def test_map_refs_replaces_each_reference_and_keeps_other_values() -> None:
    assert map_refs(PARAMS, str) == {
        'sample': 'r1.data',
        'runs': ['pid-1', None, 'f2.file'],
        'can': None,
        'sum': 'a1.data',
        'centre': 'not a reference',
        'rows': [{'file': 'pid-1', 'shift': {'offset': 'r1.data'}}],
        'group': {'centre': 'r1.data', 'x': 1.0},
        'deep': {'mask': {'by': [{'kind': 'given', 'data': 'a1.data'}]}},
    }


def test_the_json_form_of_a_reference_names_its_kind() -> None:
    assert OutputRef(record='r1', output='data').model_dump() == {
        **OUTPUT_REF,
        'key': None,
    }
    assert DatasetRef(dataset='pid-1').model_dump() == DATASET_REF
    assert AccumulatorRef(accumulator='a1', output='data').model_dump() == (
        ACCUMULATOR_REF
    )
    element = OutputRef(record='r1', output='banks', key='a')
    assert element.model_dump_json() == (
        '{"@ref":"output","record":"r1","output":"banks","key":"a"}'
    )
    assert OutputRef.model_validate_json(element.model_dump_json()) == element


def test_as_ref_decides_what_a_reference_is_by_its_kind() -> None:
    ref = OutputRef(record='r', output='o')
    assert as_ref(ref) is ref
    assert as_ref(OUTPUT_REF) == OutputRef(record='r1', output='data')
    assert as_ref(DATASET_REF) == DatasetRef(dataset='pid-1')
    assert as_ref(ACCUMULATOR_REF) == AccumulatorRef(accumulator='a1', output='data')
    assert as_ref({'record': 'r1', 'output': 'data'}) is None
    assert as_ref({'dataset': 'pid-1'}) is None
    assert as_ref({'value': 1.0}) is None
    assert as_ref('r1.data') is None


@pytest.mark.parametrize(
    ('value', 'error'),
    [
        ({'@ref': 'record', 'record': 'r1'}, "'record' is not a kind of reference"),
        ({'@ref': None, 'dataset': 'pid-1'}, 'None is not a kind of reference'),
        ({'@ref': 'output', 'record': 'r1'}, 'output: Field required'),
        ({**DATASET_REF, 'run': 1}, 'run: Extra inputs are not permitted'),
        ({**ACCUMULATOR_REF, 'record': 'r1'}, 'record: Extra inputs'),
        ({**DATASET_REF, 'dataset': ''}, 'dataset: String should have at least'),
    ],
)
def test_as_ref_refuses_an_unknown_kind_or_malformed_fields(
    value: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        as_ref(value)


def test_iter_refs_and_map_refs_name_the_place_of_a_malformed_reference() -> None:
    inputs = {'runs': [DATASET_REF, {'@ref': 'run'}]}
    with pytest.raises(ValueError, match=r"^runs\[1\]: .*'run' is not a kind"):
        list(iter_refs(inputs))
    with pytest.raises(
        ParamsError, match=r"^runs\[1\]: .*'run' is not a kind"
    ) as raised:
        map_refs(inputs, str)
    ((loc, msg),) = raised.value.errors
    assert loc == ('runs', 1)
    assert "'run' is not a kind" in msg


class MaskFile(DatasetModel, kind='mask', instrument='loki'):
    pass


def test_a_dataset_model_holds_its_class_keywords_as_metadata() -> None:
    assert DatasetModel.metadata == {}
    assert MaskFile.metadata == {'kind': 'mask', 'instrument': 'loki'}
    assert MaskFile(path='mask.xml').model_dump() == {'path': Path('mask.xml')}


def test_the_keywords_of_a_subclass_are_merged_over_its_parents() -> None:
    class BeamStopMask(MaskFile, kind='beam-stop', bank=1):
        pass

    assert BeamStopMask.metadata == {
        'kind': 'beam-stop',
        'instrument': 'loki',
        'bank': 1,
    }
    assert MaskFile.metadata == {'kind': 'mask', 'instrument': 'loki'}


def test_a_subclass_without_keywords_has_the_metadata_of_its_parent() -> None:
    class OtherMask(MaskFile):
        pass

    assert OtherMask.metadata == MaskFile.metadata


def test_a_pydantic_config_keyword_configures_the_model_and_is_no_metadata() -> None:
    class FrozenMask(DatasetModel, frozen=True, kind='mask'):
        pass

    assert FrozenMask.metadata == {'kind': 'mask'}
    mask = FrozenMask(path='mask.xml')
    with pytest.raises(pydantic.ValidationError, match='frozen'):
        mask.path = Path('other.xml')

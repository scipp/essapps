# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from ess.spec import AccumulatorRef, DatasetRef, OutputRef, as_ref, walk_refs

OUTPUT_REF = {'record': 'r1', 'output': 'data'}
DATASET_REF = {'dataset': 'pid-1'}
ACCUMULATOR_REF = {'accumulator': 'a1', 'output': 'data'}


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


def test_walk_refs_finds_references_at_any_depth() -> None:
    inputs = {
        'sample': OUTPUT_REF,
        'runs': [DATASET_REF, {'record': 'f2', 'output': 'file'}],
        'banks': {'a': {'record': 'r2', 'output': 'banks', 'key': 'a'}},
        'centre': {'value': 1.0, 'unit': 'm'},
        'sum': ACCUMULATOR_REF,
    }
    assert [(p, str(r)) for p, r in walk_refs(inputs)] == [
        ('sample', 'r1.data'),
        ('runs[0]', 'pid-1'),
        ('runs[1]', 'f2.file'),
        ('banks.a', 'r2.banks[a]'),
        ('sum', 'a1.data'),
    ]


def test_as_ref_decides_what_a_reference_is() -> None:
    ref = OutputRef(record='r', output='o')
    assert as_ref(ref) is ref
    assert as_ref(OUTPUT_REF) == OutputRef(record='r1', output='data')
    assert as_ref(DATASET_REF) == DatasetRef(dataset='pid-1')
    assert as_ref(ACCUMULATOR_REF) == AccumulatorRef(accumulator='a1', output='data')
    assert as_ref({'record': 'r1', 'output': 'o', 'extra': 1}) is None
    assert as_ref({'dataset': 'pid', 'extra': 1}) is None
    assert as_ref({**ACCUMULATOR_REF, 'upto': 2}) is None
    assert as_ref({**ACCUMULATOR_REF, 'record': 'r1'}) is None
    assert as_ref({'value': 1.0}) is None
    assert as_ref('r1.data') is None

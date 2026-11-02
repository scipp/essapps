# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type validates a request and is the params schema, so the two
agree: a request that :func:`ess.spec.places.held` accepts is one that the
params schema takes, and the other way round.
"""

from typing import Annotated, Any, Literal

import pytest
from annotated_types import Gt
from pydantic import AfterValidator, BaseModel, Field, TypeAdapter

from ess.spec import FileModel, FileRef, OutputRef, WorkflowSpec
from ess.spec.places import held
from ess.spec.refs import ParamsError
from ess.spec.request_types import FILE, INLINE, OUTPUT, row_forms


class RunFile(FileModel, kind='raw'):
    pass


class Reduction(BaseModel):
    bins: Annotated[int, Gt(0)] = 10


class Specimen(BaseModel):
    run: RunFile
    thickness: float = 1.0


class Given(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class Find(BaseModel):
    kind: Literal['find'] = 'find'
    tol: Annotated[float, Gt(0)] = 1e-3


class WithFile(BaseModel):
    kind: Literal['wf'] = 'wf'
    run: RunFile


Centre = Annotated[Given | Find, Field(discriminator='kind')]


class Params(BaseModel):
    red: Reduction = Reduction()
    opt: Reduction | None = None
    spec: Specimen | None = None
    centre: Centre = Given()
    ocentre: Centre | None = None
    wf: Annotated[Given | WithFile, Field(discriminator='kind')] | None = None
    run: RunFile | None = None
    runs: list[RunFile] = []
    reds: list[Reduction] = []
    cents: list[Centre] = []
    given: Given = Given()  # also an alternative of centre, with its tag optional


class Outputs(BaseModel):
    result: Reduction


SPEC = WorkflowSpec(
    name='forms', version=1, title='F', description='.', params=Params, outputs=Outputs
)

OUT = {'@ref': 'output', 'record': 'r1', 'output': 'o'}
ACC = {'@ref': 'accumulator', 'accumulator': 'a1', 'output': 'o'}
RUN = {'@ref': 'file', 'file': 'run:1'}

REQUESTS = [
    ('red', OUT),
    ('red', ACC),
    ('red', {**OUT, 'extra': 1}),
    ('red', {'@ref': 'output'}),
    ('red', {'@ref': 'bogus'}),
    ('red', {'@ref': 5}),
    ('red', RUN),
    ('red', 5),
    ('red', None),
    ('red', []),
    ('red', {'bins': 0}),
    ('red', {'bins': OUT}),
    ('red', {'bins': RUN}),
    ('red', {'bins': 2, 'nope': 1}),
    ('opt', None),
    ('opt', OUT),
    ('opt', 5),
    ('spec', OUT),
    ('spec', {'run': RUN}),
    ('spec', {'run': OUT}),
    ('spec', {'run': 'run:1'}),
    ('spec', {'run': {'file': 'run:1'}}),
    ('spec', {}),
    ('centre', {'kind': 'find', 'tol': -1}),
    ('centre', {'tol': 1}),
    ('centre', {'kind': 'nope'}),
    ('centre', {'kind': OUT}),
    ('centre', OUT),
    ('centre', RUN),
    ('centre', 5),
    ('centre', None),
    ('centre', {'kind': 'find', 'x': 1}),
    ('ocentre', None),
    ('ocentre', {'kind': 'find'}),
    ('ocentre', OUT),
    ('wf', {'kind': 'wf', 'run': RUN}),
    ('wf', {'kind': 'wf', 'run': OUT}),
    ('wf', {'kind': 'wf'}),
    ('run', RUN),
    ('run', OUT),
    ('run', ACC),
    ('run', {'file': 'run:1'}),
    ('run', 'run:1'),
    ('run', {'@ref': 'file'}),
    ('run', {**RUN, 'x': 1}),
    ('run', None),
    ('run', 5),
    ('runs', [RUN]),
    ('runs', [RUN, OUT]),
    ('runs', OUT),
    ('runs', RUN),
    ('runs', [None]),
    ('runs', 'x'),
    ('reds', [{'bins': 1}, OUT]),
    ('reds', [{'bins': 1}, RUN]),
    ('reds', [{'bins': 0}]),
    ('reds', [None]),
    ('cents', [{'kind': 'find'}, {'kind': 'given', 'x': 1.0}]),
    ('cents', [{'kind': 'find'}, OUT]),
    ('cents', [{'kind': 'x'}]),
    ('cents', [5]),
    ('given', {}),
    ('given', {'kind': 'given'}),
    ('given', OUT),
]


def _resolved(name: str, model: type[FileModel]) -> str:
    """Each name is the identity of a known file: the schema checks no file."""
    return name


@pytest.mark.parametrize(('name', 'value'), REQUESTS)
def test_the_params_schema_takes_what_a_request_may_write(
    name: str, value: Any
) -> None:
    jsonschema = pytest.importorskip('jsonschema')
    validator = jsonschema.Draft202012Validator(SPEC.serialize().params_schema)
    try:
        held(Params, name, value, _resolved)
    except ParamsError:
        accepted = False
    else:
        accepted = True
    request = TypeAdapter(Any).dump_python({name: value}, mode='json')
    assert validator.is_valid(request) == accepted


def test_each_place_takes_the_forms_its_type_allows() -> None:
    assert list(row_forms(Params, 'red')) == [INLINE, OUTPUT]
    assert list(row_forms(Params, 'spec')) == [INLINE]
    assert list(row_forms(Params, 'centre')) == [INLINE]
    assert list(row_forms(Params, 'run')) == [FILE]
    assert list(row_forms(Params, 'runs')) == [FILE]


def _only_run_1(run: RunFile) -> RunFile:
    if run.file != 'run:1':
        raise ValueError('only run:1 is allowed here')
    return run


def test_the_validators_of_a_file_model_at_a_place_run_when_it_is_written() -> None:
    class Checked(BaseModel):
        run: Annotated[RunFile, AfterValidator(_only_run_1)]
        runs: list[Annotated[RunFile, AfterValidator(_only_run_1)]] = []

    assert held(Checked, 'run', FileRef(file='run:1'), _resolved) == RUN
    for name, value, loc in (
        ('run', FileRef(file='run:2'), ('run',)),
        ('runs', [FileRef(file='run:2')], ('runs', 0)),
    ):
        with pytest.raises(ParamsError) as raised:
            held(Checked, name, value, _resolved)
        ((at, msg),) = raised.value.errors
        assert at == loc
        assert 'only run:1 is allowed here' in msg


def test_a_reference_to_an_output_is_held_as_given() -> None:
    ref = OutputRef(record='r1', output='o')
    assert held(Params, 'red', ref, _resolved) == ref.model_dump()

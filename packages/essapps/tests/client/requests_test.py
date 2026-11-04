# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""
Submitting requests: what is refused, what a record holds of its request,
model validators, bindings, and specs.
"""

from typing import Self

import pytest
from pydantic import BaseModel, model_validator

from ess.apps import (
    Backend,
    Client,
    FieldError,
    Request,
    SpecId,
    SubmitError,
    Template,
    file,
    local,
)
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows
from ess.spec import Binding, RunFile, function_binding

from . import toys


class Unchecked(toys.StepsParams):
    """Its model validator always raises, which the framework never calls."""

    @model_validator(mode='after')
    def _raises(self) -> Self:
        raise ValueError('a model validator was called')


UNCHECKED_PLAIN = toys.make_spec('unchecked-plain', params=Unchecked, tables=('steps',))


UNCHECKED_STAGED = toys.make_spec(
    'unchecked-staged', params=Unchecked, tables=('steps',)
)


UNCHECKED_ROWS = toys.make_spec('unchecked-rows', params=Unchecked, tables=('steps',))


def test_the_framework_never_calls_a_model_validator(files: FakeFiles) -> None:
    def sizes(params: toys.StepsParams) -> toys.Value:
        return toys.as_value(params.scale.factor * sum(s.size for s in params.steps))

    bind = [
        function_binding(UNCHECKED_PLAIN, sizes),
        StagedFunction(UNCHECKED_STAGED, sizes),
        kept_rows(UNCHECKED_ROWS, sizes),
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        plain = client.compute(UNCHECKED_PLAIN, params={'steps': [toys.Step(size=1.0)]})
        stage = client.stage(
            Template(
                UNCHECKED_STAGED,
                params={'steps': [toys.Step(size=2.0)]},
                blanks=('scale',),
            )
        )
        staged = client.compute(stage, params={'scale': {'factor': 2.0}})
        rows = client.accumulator(Template(UNCHECKED_ROWS, blanks=('steps',)))
        rows.push(steps=[toys.Step(size=3.0)])

        assert client.output(plain, 'value') == toys.Amount(value=1.0)
        assert client.output(staged, 'value') == toys.Amount(value=4.0)
        assert client.output(rows, 'value') == toys.Amount(value=3.0)


def test_bind_is_a_list_of_bindings(files: FakeFiles) -> None:
    for bind in ({toys.LOAD: toys.load_run}, [toys.load_run]):
        with pytest.raises(TypeError, match='bind is a list of bindings'):
            local(proposal='p1', files=files, bind=bind)  # type: ignore[arg-type]


def test_values_go_with_a_spec_or_a_stage(client: Client) -> None:
    with pytest.raises(TypeError, match='params go with a spec or a stage'):
        client.submit(toys.load_request(1), params={'sample': file(run=2)})


def test_a_client_gives_the_specs_the_backend_runs(
    client: Client, bindings: list[Binding]
) -> None:
    load = client.compute(toys.load_request(1))

    assert client.spec(load.spec) is toys.LOAD
    assert client.specs() == [b.spec for b in bindings]
    with pytest.raises(KeyError, match='unknown spec load/v2'):
        client.spec(SpecId(name='load', version=2))


def test_a_spec_whose_output_holds_a_file_field_is_refused() -> None:
    class RunOutputs(BaseModel):
        run: RunFile

    with pytest.raises(ValueError, match='no output holds a file or a Path'):
        toys.make_spec('echo', params=toys.Sample, outputs=RunOutputs)


def test_a_reference_to_an_unknown_record_is_refused(client: Client) -> None:
    missing = {'@ref': 'output', 'record': 'missing', 'output': 'value'}

    with pytest.raises(SubmitError, match='unknown record or accumulator missing'):
        client.submit(toys.SHIFT, params={'source': missing})


def test_a_reference_to_a_missing_output_is_refused(client: Client) -> None:
    loaded = client.compute(toys.load_request(1))
    missing = {'@ref': 'output', 'record': loaded.id, 'output': 'total'}

    with pytest.raises(SubmitError, match="source: load/v1 has no output 'total'"):
        client.submit(toys.SHIFT, params={'source': missing})


def test_a_reference_to_another_proposal_is_refused(backend: Backend) -> None:
    theirs = Client(backend, proposal='p2', submitter='eve')
    mine = Client(backend, proposal='p1', submitter='anna')
    loaded = mine.compute(toys.load_request(1))

    with pytest.raises(SubmitError, match='belongs to proposal p1'):
        theirs.submit(toys.read_request(loaded))


def test_a_reference_to_a_failed_record_is_refused(
    client: Client, unreadable: Request
) -> None:
    failed = client.compute(unreadable)

    with pytest.raises(SubmitError, match=f'source: record {failed.id} failed'):
        client.submit(toys.read_request(failed))


def test_a_reference_with_a_key_reads_an_element_of_an_output(client: Client) -> None:
    split = client.submit(toys.SPLIT, params={'sample': file(run=2)})
    whole, half = (
        split.ref('parts').model_copy(update={'key': k}) for k in ('whole', 'half')
    )
    total = client.compute(toys.TOTAL, params={'parts': [whole, half]})

    assert client.output(total, 'value') == toys.Amount(value=3.0)


def test_a_reference_with_a_key_into_a_list_is_by_integer(client: Client) -> None:
    split = client.submit(toys.SPLIT, params={'sample': file(run=2)})
    whole, half = (split.ref('pieces', key=k) for k in ('0', '1'))

    assert client.output(
        client.compute(toys.TOTAL, params={'parts': [whole, half]})
    ) == {'value': toys.Amount(value=3.0)}
    for key in ('half', '01', '+1', '-1', ' 1'):
        named = split.ref('pieces', key=key)
        with pytest.raises(SubmitError, match=r'^source: .*: the key of an element of'):
            client.submit(toys.SHIFT, params={'source': named})


def test_a_reference_to_an_element_of_an_output_without_elements_is_refused(
    client: Client,
) -> None:
    a = client.submit(toys.load_request(1))
    with pytest.raises(SubmitError, match='element of an output without elements'):
        client.submit(toys.SHIFT, params={'source': a.ref('value', key='0')})


def test_an_unknown_file_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='unknown file run:9'):
        client.submit(toys.load_request(9))


def test_a_file_of_another_proposal_is_refused(
    client: Client, files: FakeFiles
) -> None:
    files.measure(4, 4.0, proposal='p2')

    with pytest.raises(
        SubmitError, match=r'^sample: file run:4 belongs to proposal p2$'
    ):
        client.submit(toys.load_request(4))


def test_a_file_fits_a_file_field_by_each_key_of_its_metadata(
    client: Client, files: FakeFiles
) -> None:
    files.measure(5, 5.0, kind='mask', instrument='dream')
    files.measure(6, 6.0, kind='mask', instrument='loki')

    with pytest.raises(
        SubmitError,
        match=r"^mask: file run:5 does not fit the field, which takes files "
        r"with \{'instrument': 'loki', 'kind': 'mask'\}$",
    ):
        client.submit(toys.LOKI_MASKED, params={'mask': file(run=5)})
    with pytest.raises(SubmitError, match=r'^mask: file run:1 does not fit the field'):
        client.submit(toys.LOKI_MASKED, params={'mask': file(run=1)})  # a raw file
    masked = client.compute(toys.LOKI_MASKED, params={'mask': file(run=6)})
    assert client.output(masked, 'value') == toys.Amount(value=6.0)


def test_a_path_names_the_file_at_that_path(client: Client, files: FakeFiles) -> None:
    path = files.path(files.resolve(file(run=1)))
    by_path = client.compute(toys.LOAD, params={'sample': file(path=path)})

    assert by_path.request == client.compute(toys.load_request(1)).request
    assert client.output(by_path, 'value') == toys.Amount(value=1.0)


def test_a_record_holds_its_request_with_defaults_filled_in(client: Client) -> None:
    a = client.compute(toys.load_request(1))
    shifted = client.compute(toys.read_request(a))

    assert shifted.request.params == {
        'shift': {'offset': 0.0},
        'source': a.ref('value'),
    }
    assert shifted.request.upstream() == [a.ref('value')]


def test_one_refused_request_refuses_the_submission(client: Client) -> None:
    a = client.submit(toys.load_request(1))
    typo = Request(
        toys.SHIFT, params={'shift': {'offset': 'x'}, 'source': a.ref('value')}
    )

    with pytest.raises(SubmitError, match='offset'):
        client.submit([toys.load_request(2), typo])
    assert client.records() == [a]


def test_a_required_parameter_left_out_is_refused(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r'^sample: a required parameter, not given$'
    ) as raised:
        client.submit(toys.LOAD)
    assert raised.value.errors == (
        FieldError(None, ('sample',), 'a required parameter, not given'),
    )


def test_a_refusal_lists_each_problem_of_a_value(client: Client) -> None:
    typo = Request(
        toys.SCALE,
        params={
            'scaling': {'valeu': 1, 'other': 2},
            'sample': file(run=1),
        },
    )

    with pytest.raises(SubmitError) as raised:
        client.submit({'typo': typo})
    assert [(e.entry, e.loc) for e in raised.value.errors] == [
        ('typo', ('scaling', 'valeu')),
        ('typo', ('scaling', 'other')),
    ]
    assert str(raised.value).count('typo: scaling.') == 2


def test_a_refusal_names_the_request_and_the_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^second: sample: unknown file run:9$'):
        client.submit({'first': toys.load_request(1), 'second': toys.load_request(9)})
    with pytest.raises(SubmitError) as raised:
        client.submit([toys.load_request(1), toys.load_request(9)])
    assert raised.value.errors == (FieldError(1, ('sample',), 'unknown file run:9'),)
    assert str(raised.value) == '[1]: sample: unknown file run:9'


def test_a_table_that_needs_a_row_is_refused_without_one(client: Client) -> None:
    with pytest.raises(SubmitError, match='parts'):
        client.submit(toys.TOTAL, params={'parts': []})


def test_rows_are_distinct_as_a_whole(client: Client) -> None:
    weighted = {'value': 1.0, 'weight': 2.0}
    summed = client.compute(
        toys.WEIGHTED, params={'parts': [{'value': 1.0, 'weight': 1.0}, weighted]}
    )

    assert client.output(summed, 'value') == toys.Amount(value=3.0)
    with pytest.raises(SubmitError, match=r'parts\[1\]: repeats parts\[0\]'):
        client.submit(toys.WEIGHTED, params={'parts': [weighted, dict(weighted)]})


def test_rows_of_file_fields_are_distinct_by_identity(
    client: Client, files: FakeFiles
) -> None:
    # Two files of one run, by PID.
    a = files.measure(7, 1.0, pid='x')
    b = files.measure(7, 2.0, pid='y')

    summed = client.compute(toys.SUM, params={'runs': [a, b]})
    assert client.output(summed, 'value') == toys.Amount(value=3.0)
    with pytest.raises(SubmitError, match=r'^runs\[1\]: repeats runs\[0\]'):
        client.submit(toys.SUM, params={'runs': [a, file(pid='x')]})

    acc = client.accumulator(Template(toys.SUM, blanks=('runs',)))
    acc.push(runs=[a])
    acc.push(runs=[b])
    assert client.output(acc, 'value') == toys.Amount(value=3.0)
    with pytest.raises(SubmitError, match=r'^runs\[2\]: repeats runs\[0\]'):
        acc.push(runs=[file(pid='x')])


def test_a_stage_reads_a_reference_inside_a_group_of_its_template(
    client: Client,
) -> None:
    a, b = client.submit([toys.load_request(1), toys.load_request(2)])
    offset = {'offset': {'amount': a.ref('value')}}
    stage = client.stage(Template(toys.TERMS, params=offset, blanks=('terms',)))
    client.release(a)  # the stage keeps what its template references

    first = client.compute(stage, params={'terms': [{'amount': b.ref('value')}]})
    rows = [{'amount': b.ref('value')}, {'scale': 2.0, 'amount': b.ref('value')}]
    second = client.compute(stage, params={'terms': rows})

    assert client.output(first, 'value') == toys.Amount(value=3.0)
    assert client.output(second, 'value') == toys.Amount(value=7.0)


def test_an_accumulator_takes_rows_that_hold_references(client: Client) -> None:
    a, b = client.submit([toys.load_request(1), toys.load_request(2)])
    acc = client.accumulator(Template(toys.TERMS_ROWS, blanks=('terms',)))
    acc.push(terms=[{'amount': a.ref('value')}])
    acc.push(terms=[{'scale': 2.0, 'amount': b.ref('value')}])

    assert client.output(acc, 'value') == toys.Amount(value=5.0)
    # A row that leaves its default out repeats one that gives it.
    with pytest.raises(SubmitError, match=r'^terms\[2\]: repeats terms\[0\]'):
        acc.push(terms=[{'scale': 1.0, 'amount': a.ref('value')}])


def test_a_reference_to_an_accumulator_inside_a_row_reads_its_state(
    client: Client,
) -> None:
    a = client.submit(toys.load_request(2))
    acc = client.accumulator(Template(toys.TERMS_ROWS, blanks=('terms',)))
    acc.push(terms=[{'amount': a.ref('value')}])

    rows = [{'scale': 3.0, 'amount': acc.ref('value')}]
    summed = client.compute(toys.TERMS, params={'terms': rows})

    assert client.output(summed, 'value') == toys.Amount(value=6.0)

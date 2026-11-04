# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Stages."""

import threading
import weakref
from collections.abc import Callable, Collection, Mapping
from typing import Any

import pytest

from ess.apps import (
    Backend,
    Client,
    Request,
    Status,
    SubmitError,
    Template,
    file,
    local,
)
from ess.apps.backend import Entry
from ess.apps.testing import FakeFiles, StagedFunction
from ess.spec import function_binding

from .conftest import (
    ARRAY,
    COPY,
    SCALE,
    SHIFT,
    TOTAL,
    Amount,
    CopyOutputs,
    CopyParams,
    Counts,
    Factor,
    Shift,
    _load,
    _read,
    _shift,
    _stage_shift,
    array,
)


def test_calls_through_a_stage_are_staged_once(
    client: Client, shifting: StagedFunction
) -> None:
    load = client.submit(_load(1))
    stage = _stage_shift(client, load)
    shifted = [_shift(client, stage, x) for x in (0.5, 1.5)]
    plain = client.compute(
        SHIFT,
        params={'shift': Shift(offset=1.5), 'source': load.ref('value')},
    )

    assert [client.output(r, 'value') for r in shifted] == [
        Amount(value=v) for v in (1.5, 2.5)
    ]
    assert shifted[1].request == plain.request
    assert shifting.staged == [('shift',)]  # the plain request makes no stage


def test_concurrent_calls_through_a_stage_stage_it_once(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(1)))
    pending = [_shift(client, stage, x) for x in (1.0, 2.0, 3.0, 4.0)]
    loading.set()
    client.wait(pending)

    assert [client.output(r, 'value') for r in pending] == [
        Amount(value=v) for v in (2.0, 3.0, 4.0, 5.0)
    ]
    assert shifting.staged == [('shift',)]


def test_a_released_stage_takes_no_calls_and_runs_those_made_before(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(2)))
    pending = _shift(client, stage, 1.0)
    client.release(stage)
    loading.set()

    with pytest.raises(SubmitError, match='the stage was released'):
        _shift(client, stage, 2.0)
    assert client.wait(pending) is Status.COMPLETED
    assert client.output(pending, 'value') == Amount(value=3.0)
    assert shifting.staged == [('shift',)]


def test_a_stage_keeps_the_file_its_name_resolved_to_when_it_was_made(
    client: Client, scaling: StagedFunction, files: FakeFiles
) -> None:
    run = files.resolve(file(run=1))
    stage = client.stage(
        Template(SCALE, params={'sample': file(run=1)}, blanks=('scaling',))
    )
    twice = {'scaling': Factor(factor=2.0)}
    before = client.compute(stage, params=twice)
    files.correct(run, run=99)
    files.correct(files.measure(7, 5.0), run=1)
    after = client.compute(stage, params=twice)

    assert files.resolve(file(run=1)) != run
    assert stage.template.params == {'sample': run}
    assert after.request == before.request
    assert [client.output(r, 'value') for r in (before, after)] == [
        Amount(value=v) for v in (2.0, 2.0)
    ]
    assert scaling.staged == [('scaling',)]


def test_a_stage_lets_go_of_what_its_template_references_once_staged(
    files: FakeFiles,
) -> None:
    class KeepsTheSum:
        """Stages COPY keeping only the sum of the source, not the source."""

        spec = COPY

        def compute(self, params: CopyParams) -> CopyOutputs:
            return self.make_stage({'source': params.source}, ())({})

        def make_stage(
            self, fixed: Mapping[str, Any], blanks: Collection[str]
        ) -> Callable[[Mapping[str, Any]], CopyOutputs]:
            total = CopyOutputs(value=Counts(counts=fixed['source'].counts.sum()))
            return lambda given: total

    bind = [function_binding(ARRAY, array), KeepsTheSum()]
    with local(proposal='p1', files=files, bind=bind) as client:
        loaded = client.compute(ARRAY, params={'sample': file(run=2)})
        value = weakref.ref(client.output(loaded, 'value'))
        stage = client.stage(Template(COPY, params={'source': loaded.ref('value')}))
        client.release(loaded)
        assert value() is not None

        first = client.compute(stage)
        assert value() is None
        again = client.compute(stage)
        sums = [client.output(r, 'value').counts.value for r in (first, again)]
        assert sums == [2.0] * 2


def test_a_stage_keeps_what_its_template_references_until_it_has_staged(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(2))
    stage = _stage_shift(client, load)
    client.release(load)
    loading.set()

    shifted = [_shift(client, stage, x) for x in (1.0, 2.0)]
    client.wait(shifted)
    assert [client.output(r, 'value') for r in shifted] == [
        Amount(value=v) for v in (3.0, 4.0)
    ]
    with pytest.raises(SubmitError, match='not kept by this client'):
        client.submit(_read(load))


def test_releasing_a_stage_lets_go_of_what_its_template_references(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(2))
    stage = _stage_shift(client, load)
    client.release([load, stage])
    loading.set()

    assert client.status(load) is Status.CANCELLED


def test_a_stage_whose_staging_fails_stops(
    client: Client, scaling: StagedFunction
) -> None:
    scaling.fail_next = True
    stage = client.stage(
        Template(SCALE, params={'sample': file(run=2)}, blanks=('scaling',))
    )
    twice = {'scaling': Factor(factor=2.0)}
    failed = client.compute(stage, params=twice)

    assert client.status(failed) is Status.FAILED
    assert client.failure(failed) == 'OSError: staging failed'
    with pytest.raises(SubmitError, match='the stage stopped: staging failed'):
        client.submit(stage, params=twice)
    assert scaling.staged == []


def test_the_calls_waiting_for_a_stage_whose_staging_fails_fail_with_it(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(1)))
    shifting.fail_next = True
    pending = [_shift(client, stage, x) for x in (1.0, 2.0, 3.0)]
    loading.set()
    client.wait(pending)

    assert sorted(client.failure(r) for r in pending) == [
        'OSError: staging failed',
        *['the stage stopped: staging failed: OSError: staging failed'] * 2,
    ]
    assert shifting.staged == []


def test_a_request_through_a_stage_must_have_the_stage_s_values(
    backend: Backend,
) -> None:
    client = backend.open_client('p1', 'anna')
    (load,) = backend.submit([Entry(_load(1))], client=client)
    stage, template = backend.open_stage(
        Template(SCALE, params={'sample': file(run=1)}, blanks=('scaling',)),
        client=client,
    )
    twice = {'scaling': {'factor': 2.0}}
    refused = {
        'the stage holds scale': _read(load),
        r"\['sample'\]: differ from the stage's values": Request(
            SCALE, params={**twice, 'sample': file(run=2)}
        ),
    }
    for reason, request in refused.items():
        with pytest.raises(SubmitError, match=reason):
            backend.submit([Entry(request, stage=stage)], client=client)

    (record,) = backend.submit(
        [Entry(template.fill(params=twice), stage=stage)], client=client
    )
    assert record.request.params['sample'] == template.params['sample']


def test_a_stage_refuses_a_template_that_a_request_would_refuse(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    cancelled = client.submit(_load(1))
    client.cancel(cancelled)
    loading.set()
    released = client.compute(_load(1))
    client.release(released)
    refused = {
        r'sample: unknown file run:9': Template(
            SCALE, params={'sample': file(run=9)}, blanks=('scaling',)
        ),
        r'source: file .*: the field is not a file field': Template(
            SHIFT, params={'source': file(run=1)}, blanks=('shift',)
        ),
        f'source: record {cancelled.id} cancelled': Template(
            SHIFT, params={'source': cancelled.ref('value')}, blanks=('shift',)
        ),
        f'source: record {released.id} is not kept by this client': Template(
            SHIFT, params={'source': released.ref('value')}, blanks=('shift',)
        ),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.stage(template)


def test_a_template_and_its_calls_take_models(client: Client) -> None:
    load = client.submit(_load(1))
    half = Shift(offset=0.5)
    stage = client.stage(
        Template(SHIFT, params={'shift': half, 'source': load.ref('value')})
    )
    tuned = client.compute(_stage_shift(client, load), params={'shift': half})

    assert client.output(client.compute(stage), 'value') == Amount(value=1.5)
    assert client.output(tuned, 'value') == Amount(value=1.5)


def test_a_stage_needs_a_binding_that_makes_stages(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^total/v1: its binding makes no stages$'):
        client.stage(Template(TOTAL, blanks=('parts',)))
    with pytest.raises(SubmitError, match=r'^total/v1: its binding makes no stages$'):
        client.stage(Template(TOTAL, params={'speed': 1.0}, blanks=('parts',)))

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""
Resolving requests and templates: the form a record holds a request in, without
submitting it.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from ess.apps import Client, Request, SubmitError, Template, Workspace, file, local
from ess.apps.log import Log
from ess.apps.testing import FakeFiles, FakeStore
from ess.spec import RunFile, function_binding

from . import toys

type Make = Callable[[Client, FakeFiles], Request]


def _defaults(client: Client, files: FakeFiles) -> Request:
    return Request(toys.SUM, params={'runs': [file(run=1), file(run=2)]})


def _file_by_run_number(client: Client, files: FakeFiles) -> Request:
    return toys.load_request(7)  # identified by its PID


def _file_by_path(client: Client, files: FakeFiles) -> Request:
    path = files.path(files.resolve(file(run=1)))
    return Request(toys.LOAD, params={'sample': file(path=path)})


def _reference(client: Client, files: FakeFiles) -> Request:
    return toys.read_request(client.submit(toys.load_request(1)))


def _table_rows(client: Client, files: FakeFiles) -> Request:
    a = client.submit(toys.load_request(1))
    terms = [{'amount': a.ref('value')}, {'scale': 2, 'amount': a.ref('extra')}]
    return Request(toys.TERMS, params={'terms': terms})


def _validator_that_changes_a_value(client: Client, files: FakeFiles) -> Request:
    steps = [{'size': 1.0}, {'size': 2.0}]
    return Request(toys.WALK, params={'scale': {'factor': -2}, 'steps': steps})


REQUESTS = [
    _defaults,
    _file_by_run_number,
    _file_by_path,
    _reference,
    _table_rows,
    _validator_that_changes_a_value,
]


def _name(make: Make) -> str:
    return make.__name__.strip('_')


@pytest.fixture
def files(files: FakeFiles) -> FakeFiles:
    """Runs 1 and 2, and run 7, whose identity is its PID, not its run number."""
    files.measure(7, 7.0, pid='x')
    return files


@pytest.mark.parametrize('make', REQUESTS, ids=_name)
def test_a_resolved_request_is_the_request_its_record_holds(
    client: Client, files: FakeFiles, make: Make
) -> None:
    request = make(client, files)

    resolved = client.resolve(request)

    assert resolved == client.submit(request).request


@pytest.mark.parametrize('make', REQUESTS, ids=_name)
def test_resolving_a_resolved_request_gives_the_same_request(
    client: Client, files: FakeFiles, make: Make
) -> None:
    resolved = client.resolve(make(client, files))
    assert isinstance(resolved, Request)

    assert client.resolve(resolved) == resolved


def test_a_record_s_request_resolves_to_itself_after_a_restart(
    files: FakeFiles, tmp_path: Path
) -> None:
    bind = [
        function_binding(toys.LOAD, toys.load_run),
        function_binding(toys.SHIFT, toys.shift),
    ]
    store = FakeStore()

    def client() -> Client:
        log = Log(tmp_path / 'log')
        workspace = Workspace(
            proposal='p1', files=files, bind=bind, log=log, store=store
        )
        return Client(workspace, submitter='anna', on_close=workspace.close)

    with client() as before:
        kept = before.compute(toys.load_request(1))
        persisted = before.compute(toys.load_request(1), persist=True)
        reads = before.compute([toys.read_request(kept), toys.read_request(persisted)])
    with client() as after:  # the output of kept is gone
        assert after.resolve([r.request for r in reads]) == [r.request for r in reads]


def test_a_refused_value_gives_the_reasons_of_submit_and_hides_no_other(
    client: Client,
) -> None:
    requests = {
        'good': toys.load_request(1),
        'unknown': toys.load_request(9),
        'typo': Request(toys.SCALE, params={'scaling': {'factor': 'x'}}),
    }

    resolved = client.resolve(requests)

    assert resolved['good'] == client.resolve(toys.load_request(1))
    with pytest.raises(SubmitError) as raised:
        client.submit(requests)
    assert raised.value.errors == (
        *resolved['unknown'].errors,
        *resolved['typo'].errors,
    )


def test_resolve_does_not_look_at_whether_a_reference_may_be_read(
    client: Client, unreadable: Request
) -> None:
    failed = client.compute(unreadable)
    released = client.compute(toys.load_request(1))
    client.release(released)
    requests = [toys.read_request(failed), toys.read_request(released)]

    resolved = client.resolve(requests)

    assert [r.params['source'] for r in resolved] == [
        failed.ref('value'),
        released.ref('value'),
    ]
    with pytest.raises(
        SubmitError, match=r'failed; .* is neither in memory nor persisted'
    ):
        client.submit(requests)


def test_resolving_logs_nothing(client: Client) -> None:
    load = client.compute(toys.load_request(1))
    cursor = client.changes().cursor

    client.resolve([toys.load_request(2), toys.load_request(9)])
    client.resolve(Template(toys.SHIFT, params={'source': load.ref('value')}))

    assert client.changes(after=cursor).records == ()
    assert client.records() == [load]


class SidesParams(BaseModel):
    """Files and tables on each side of a template, and a validator that changes."""

    sample: RunFile
    can: RunFile
    runs: list[RunFile]
    shifts: list[toys.Shift] = Field(min_length=1)
    scale: toys.PositiveScale = Field(default_factory=toys.PositiveScale)


SIDES = toys.make_spec('sides', params=SidesParams, tables=('runs', 'shifts'))


@pytest.fixture
def sides(files: FakeFiles) -> Iterator[Client]:
    """A client whose workspace runs SIDES."""
    bind = [function_binding(SIDES, lambda params: toys.as_value(0.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


@pytest.fixture
def settings() -> Template:
    """A template of SIDES whose blanks are a file and a table."""
    params = {
        'can': file(run=7),
        'shifts': [{}, {'offset': 1}],
        'scale': {'factor': -2},
    }
    return Template(SIDES, params=params, blanks=('sample', 'runs'))


def test_a_resolved_template_holds_every_value_but_its_blanks(
    sides: Client, settings: Template
) -> None:
    resolved = sides.resolve(settings)

    assert resolved == Template(
        SIDES,
        params={
            'can': file(pid='x'),
            'shifts': [{'offset': 0.0}, {'offset': 1.0}],
            'scale': {'factor': 2.0},
        },
        blanks=('sample', 'runs'),
    )
    assert sides.resolve(resolved) == resolved


def test_a_parameter_resolves_the_same_whatever_the_others_are(
    sides: Client, settings: Template
) -> None:
    cells = {'sample': file(run=7), 'runs': [file(run=1), file(run=7)]}
    others = [n for n in SidesParams.model_fields if n not in cells]

    whole = sides.resolve(settings.fill(params=cells)).params
    fixed = sides.resolve(settings).params
    row = sides.resolve(Template(SIDES, params=cells, blanks=others)).params

    assert whole == fixed | row


def test_a_refused_template_hides_no_other(sides: Client, settings: Template) -> None:
    resolved = sides.resolve(
        {
            'settings': settings,
            'no-can': Template(
                SIDES, params={'shifts': [{}]}, blanks=('sample', 'runs')
            ),
            'unknown': Template(
                SIDES, params={'sample': file(run=9)}, blanks=('can', 'runs', 'shifts')
            ),
            'request': settings.fill(params={'sample': file(run=1), 'runs': []}),
        }
    )

    assert resolved['settings'] == sides.resolve(settings)
    assert isinstance(resolved['request'], Request)
    assert str(resolved['no-can']) == 'no-can: can: a required parameter, not given'
    assert str(resolved['unknown']) == 'unknown: sample: unknown file run:9'

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A record of another spec than the one bound: a folder written with one set of
bindings and opened with another. Its request and its persisted outputs stay
readable as stored, and a request may not reference its outputs.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pydantic
import pytest
import scipp as sc

from ess.apps import (
    ArrayInfo,
    Client,
    Listing,
    Record,
    SpecId,
    Status,
    SubmitError,
)
from ess.apps.batch import Batch, Row
from ess.apps.folder import open_folder
from ess.apps.log import Finished, Log, Started, Submitted
from ess.spec import Binding, Referenced, WorkflowSpec, function_binding

COUNTS = sc.DataArray(sc.arange('q', 4.0), coords={'q': sc.arange('q', 5.0)})


class Centre(pydantic.BaseModel, arbitrary_types_allowed=True):
    x: float
    y: float
    counts: sc.DataArray


class Found(pydantic.BaseModel):
    centre: Centre


class Renamed(pydantic.BaseModel, arbitrary_types_allowed=True):
    """``Centre`` after ``x`` was renamed, with a default, under the same version."""

    x_mm: float = 0.0
    y: float
    counts: sc.DataArray


class FoundRenamed(pydantic.BaseModel):
    centre: Renamed


class Bins(pydantic.BaseModel):
    bins: int = 4


class UseParams(pydantic.BaseModel):
    centre: Referenced[Centre]


class Total(pydantic.BaseModel):
    total: float


def _centre(
    outputs: type[pydantic.BaseModel] = Found, version: int = 1, title: str = 'Centre'
) -> WorkflowSpec:
    return WorkflowSpec(
        name='centre',
        version=version,
        title=title,
        description='A toy.',
        params=Bins,
        outputs=outputs,
    )


CENTRE = _centre()
USE = WorkflowSpec(
    name='use',
    version=1,
    title='Use',
    description='A toy.',
    params=UseParams,
    outputs=Total,
)


def _find(params: Bins) -> Found:
    return Found(centre=Centre(x=0.5, y=0.25, counts=COUNTS))


def _find_renamed(params: Bins) -> FoundRenamed:
    return FoundRenamed(centre=Renamed(x_mm=500.0, y=0.25, counts=COUNTS))


def _use(params: UseParams) -> Total:
    return Total(total=params.centre.x)


WRITTEN = [function_binding(CENTRE, _find), function_binding(USE, _use)]
OTHERS: dict[str, list[Binding]] = {
    'renamed': [function_binding(_centre(FoundRenamed), _find_renamed)],
    'bumped': [function_binding(_centre(version=2), _find)],
    'unbound': [],
}
"""The bindings of another centre spec than the one ``WRITTEN`` binds."""


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    (tmp_path / 'files.csv').write_text('file,run\n')
    return tmp_path


@pytest.fixture
def written(folder: Path) -> str:
    """The ID of a persisted record of ``CENTRE``, with ``bins`` 5."""
    with open_folder(folder, WRITTEN) as client:
        record = client.submit(CENTRE, params={'bins': 5}, label='c', persist=True)
        client.wait(record)
    return record.id


@pytest.fixture(params=sorted(OTHERS))
def another(request: pytest.FixtureRequest) -> list[Binding]:
    """The bindings of the folder reopened: another centre spec, and ``USE``."""
    return [*OTHERS[request.param], WRITTEN[1]]


@pytest.fixture
def other(folder: Path, written: str, another: list[Binding]) -> Iterator[Client]:
    """A client of the folder reopened with ``another``."""
    with open_folder(folder, another) as client:
        yield client


def _record(client: Client, record_id: str) -> Record:
    (record,) = client.records(ids=[record_id])
    return record


def test_the_request_of_a_record_of_another_spec_reads_back_as_stored(
    other: Client, written: str
) -> None:
    record = _record(other, written)

    assert record.spec == SpecId(name='centre', version=1)
    assert record.request.params == {'bins': 5}


def test_an_output_of_a_record_of_another_spec_is_the_stored_scipp_data(
    other: Client, written: str
) -> None:
    centre = other.output(_record(other, written), 'centre')

    assert isinstance(centre, sc.DataGroup)
    assert set(centre) == {'x', 'y', 'counts'}  # x as stored, no x_mm
    assert centre['x'] == 0.5
    assert sc.identical(centre['counts'], COUNTS)


def test_describe_and_select_work_on_the_stored_data(
    other: Client, written: str
) -> None:
    record = _record(other, written)

    assert other.describe(record, 'centre') == {
        'x': 0.5,
        'y': 0.25,
        'counts': ArrayInfo(sizes={'q': 4}, unit='dimensionless', coords=('q',)),
    }
    part = other.output(record, 'centre', select={'q': slice(1, 3)})
    assert sc.identical(part['counts'], COUNTS['q', 1:3])
    assert part['x'] == 0.5
    with pytest.raises(ValueError, match="no array field with dimension 'z'"):
        other.output(record, 'centre', select={'z': 0})


def test_a_request_referencing_an_output_of_a_record_of_another_spec_is_refused(
    other: Client, written: str
) -> None:
    record = _record(other, written)
    assert other.readable(record) == ('centre',)

    with pytest.raises(SubmitError) as refused:
        other.submit(USE, params={'centre': record.ref('centre')})

    (error,) = refused.value.errors
    assert error.loc == ('centre',)
    assert "the output's type cannot be checked" in error.msg
    assert 'another spec than the one bound' in error.msg


def test_a_record_of_another_spec_is_listed_and_its_output_is_readable(
    other: Client, written: str
) -> None:
    record = _record(other, written)

    assert other.latest('c') == record
    assert other.status(record) is Status.COMPLETED
    assert other.readable(record) == ('centre',)
    assert other.provenance(record).records() == []


def test_a_spec_whose_title_and_description_changed_stays_the_bound_spec(
    folder: Path, written: str
) -> None:
    retitled = WorkflowSpec(
        **{**dict(CENTRE), 'title': 'Find the centre', 'description': 'Another.'}
    )
    with open_folder(folder, [function_binding(retitled, _find), WRITTEN[1]]) as client:
        record = _record(client, written)

        assert isinstance(client.output(record, 'centre'), Centre)
        assert client.readable(record) == ('centre',)
        use = client.compute(USE, params={'centre': record.ref('centre')})
        assert client.output(use, 'total') == 0.5


def _unfinished(folder: Path, *record_ids: str) -> None:
    """Leave the records pending in the log, as if killed before they started."""
    log = folder / 'log.jsonl'
    header, *lines = log.read_bytes().splitlines(keepends=True)
    kept = [
        line
        for line, event in zip(lines, Log.read(log), strict=True)
        if not (isinstance(event, Started | Finished) and event.record in record_ids)
    ]
    log.write_bytes(b''.join([header, *kept]))


def test_at_a_restart_a_pending_record_of_another_spec_fails(
    folder: Path, written: str, another: list[Binding]
) -> None:
    _unfinished(folder, written)

    with open_folder(folder, another) as client:
        record = _record(client, written)
        assert client.status(record) is Status.FAILED
        assert 'the workspace restarted: its spec is not bound' in client.failure(
            record
        )


def test_at_a_restart_a_pending_record_reading_an_output_of_another_spec_fails(
    folder: Path, written: str, another: list[Binding]
) -> None:
    with open_folder(folder, WRITTEN) as client:
        use = client.submit(
            USE,
            params={'centre': _record(client, written).ref('centre')},
            persist=True,
        )
        client.wait(use)
    _unfinished(folder, use.id)

    with open_folder(folder, another) as client:
        use = _record(client, use.id)
        assert client.status(use) is Status.FAILED
        assert f'record {written} output centre: the spec of its record is not ' in (
            client.failure(use)
        )


def test_a_digest_is_logged_with_the_first_record_of_its_spec_and_when_it_changes(
    folder: Path, written: str
) -> None:
    renamed = _centre(FoundRenamed)
    with open_folder(folder, WRITTEN) as client:
        client.compute(CENTRE, params={}, persist=True)
    with open_folder(folder, OTHERS['renamed']) as client:
        later = client.compute(renamed, params={}, persist=True)
    with open_folder(folder, WRITTEN) as client:
        client.compute(CENTRE, params={}, persist=True)
        # each record is of the spec whose digest was logged last before it
        assert isinstance(client.output(_record(client, written), 'centre'), Centre)
        assert isinstance(client.output(later, 'centre'), sc.DataGroup)

    logged = [
        e.specs for e in Log.read(folder / 'log.jsonl') if isinstance(e, Submitted)
    ]
    assert logged == [
        {'centre/v1': CENTRE.digest},
        {},
        {'centre/v1': renamed.digest},
        {'centre/v1': CENTRE.digest},
    ]


def test_the_changes_after_a_restart_with_another_spec_are_those_before(
    folder: Path, written: str, another: list[Binding]
) -> None:
    with open_folder(folder, WRITTEN) as client:
        centre = _record(client, written)
        client.compute(USE, params={'centre': centre.ref('centre')})
        before = client.changes()

    with open_folder(folder, another) as client:
        assert client.changes() == before


@pytest.fixture
def batch_of(folder: Path) -> Callable[[list[Binding]], Batch]:
    """A batch of row ``5`` of the folder reopened with the bindings given."""

    def make(bindings: list[Binding]) -> Batch:
        client = open_folder(folder, bindings)
        batch = Batch(client, bindings[0].spec, columns=('bins',), label='c')
        batch.rows = batch.rows.rescan([Row('5', {'bins': 5})])
        return batch

    with open_folder(folder, WRITTEN) as client:
        client.submit(CENTRE, params={'bins': 5}, label='c', member='5', persist=True)
    return make


@pytest.mark.parametrize(
    ('another', 'shows'), [('renamed', 'completed'), ('bumped', 'not reduced')]
)
def test_a_batch_shows_a_row_whose_result_is_of_another_spec(
    batch_of: Callable[[list[Binding]], Batch], another: str, shows: str
) -> None:
    batch = batch_of(OTHERS[another])
    try:
        Listing(batch.client)  # what the batch's views list
        assert batch.state()['5'].text == shows
    finally:
        batch.client.close()

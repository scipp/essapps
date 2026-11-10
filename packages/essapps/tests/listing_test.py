# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A listing made from an earlier one equals a listing read in full."""

import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ess.apps import Client, Listing, Workspace, local
from ess.apps.testing import CountingClient, FakeFiles, read_file
from ess.spec import File, FileRef, RunFile, WorkflowSpec, function_binding

type Spec = WorkflowSpec[Any, Any]


class BeamCentre(BaseModel):
    x: float = 0.0
    y: float = 0.0


class FindParams(BaseModel):
    run: RunFile


class FindOutputs(BaseModel):
    centre: BeamCentre


class Sample(BaseModel):
    thickness: float


class ReduceParams(BaseModel):
    run: RunFile
    sample: Sample


class Total(BaseModel):
    value: float


class ReduceOutputs(BaseModel):
    total: Total


FIND: Spec = WorkflowSpec(
    name='find',
    version=1,
    title='Find centre',
    description='The beam centre in a run.',
    params=FindParams,
    outputs=FindOutputs,
)
REDUCE: Spec = WorkflowSpec(
    name='reduce',
    version=1,
    title='Reduce',
    description='The counts of a sample run per thickness.',
    params=ReduceParams,
    outputs=ReduceOutputs,
)


def find(params: FindParams) -> FindOutputs:
    return FindOutputs(centre=BeamCentre(x=read_file(params.run)['x']))


def reduce(params: ReduceParams) -> ReduceOutputs:
    counts = read_file(params.run)['counts']
    return ReduceOutputs(total=Total(value=counts / params.sample.thickness))


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles('p1', tmp_path)


@pytest.fixture
def run(files: FakeFiles) -> FileRef:
    return files.measure(101, {'counts': 6.0, 'x': 1.5}, role='sample')


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    bind = [function_binding(FIND, find), function_binding(REDUCE, reduce)]
    workspace = Workspace(proposal='p1', files=files, bind=bind)
    yield Client(workspace, submitter='anna')
    workspace.close()


@pytest.fixture
def find_spec() -> Spec:
    return FIND


@pytest.fixture
def reduce_spec() -> Spec:
    return REDUCE


def same(left: Listing, right: Listing) -> bool:
    return (
        left.records == right.records
        and left.status == right.status
        and left.readable == right.readable
        and left.labels == right.labels
        and left.cursor == right.cursor
    )


def test_a_listing_with_previous_equals_a_full_listing(
    client: Client, find_spec: Spec, reduce_spec: Spec, run: FileRef
) -> None:
    client.compute(find_spec, params={'run': run})
    previous = Listing(client)
    client.compute(reduce_spec, params={'run': run, 'sample': {'thickness': 2.0}})
    client.compute(find_spec, params={'run': run})

    assert same(Listing(client, previous=previous), Listing(client))


def test_a_new_record_that_makes_two_labels_collide_changes_the_known_label(
    client: Client, find_spec: Spec, run: FileRef
) -> None:
    first = client.compute(find_spec, params={'run': run})
    previous = Listing(client)
    second = client.compute(find_spec, params={'run': run})
    if second.created.minute != first.created.minute:
        pytest.skip('a label holds the minute, and it changed between the records')

    listing = Listing(client, previous=previous)

    assert same(listing, Listing(client))
    assert listing.labels[first.id] != previous.labels[first.id]
    assert listing.labels[first.id] != listing.labels[second.id]


def test_the_previous_listing_is_not_modified(
    client: Client, find_spec: Spec, run: FileRef
) -> None:
    first = client.compute(find_spec, params={'run': run})
    previous = Listing(client)
    kept = (list(previous.records), dict(previous.status), dict(previous.labels))
    client.compute(find_spec, params={'run': run})

    Listing(client, previous=previous)

    assert (previous.records, previous.status, previous.labels) == kept
    assert previous.records == [first]


def test_a_known_record_that_finished_is_read_again(
    files: FakeFiles, run: FileRef
) -> None:
    gate = threading.Event()

    def find_at_the_gate(params: Any) -> Any:
        gate.wait()
        return find(params)

    bind = [function_binding(FIND, find_at_the_gate)]
    with local(proposal='p1', files=files, bind=bind) as client:
        try:
            record = client.submit(FIND, params={'run': run})
            previous = Listing(client)
            assert previous.pending == [record]
        finally:
            gate.set()
        client.wait(record)
        counting = CountingClient(client)

        listing = Listing(counting, previous=previous)  # type: ignore[arg-type]

        assert same(listing, Listing(client))
        assert dict(counting.reads) == {'records': 1, 'status': 1, 'readable': 1}


class CountingMetadata:
    """Delegates to a workspace and logs the files of each ``file_metadata`` call."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self.calls: list[list[FileRef]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._workspace, name)

    def file_metadata(self, refs: Sequence[FileRef]) -> list[dict[str, Any]]:
        self.calls.append(list(refs))
        return self._workspace.file_metadata(refs)


def test_a_listing_reads_the_metadata_of_its_files_in_one_call(
    files: FakeFiles,
) -> None:
    runs = [files.measure(n, {'counts': 1.0}) for n in (101, 102, 103)]
    workspace = Workspace(
        proposal='p1', files=files, bind=[function_binding(FIND, find)]
    )
    counting = CountingMetadata(workspace)
    client = Client(counting, submitter='anna')

    listed = Listing(client).files(File())

    assert list(listed.values()) == runs
    assert counting.calls == [runs]
    workspace.close()


def test_a_listing_lists_the_runs_of_several_roles_once(files: FakeFiles) -> None:
    runs = [files.measure(n, {'counts': 1.0}) for n in (101, 102)]
    workspace = Workspace(
        proposal='p1', files=files, bind=[function_binding(FIND, find)]
    )
    counting = CountingMetadata(workspace)
    client = Client(counting, submitter='anna')
    listing = Listing(client)

    for role in ('sample', 'background'):
        listing.files(File(role=role))

    assert counting.calls == [runs]
    workspace.close()


def test_a_role_of_another_kind_selects_what_the_file_holds(files: FakeFiles) -> None:
    beam = files.measure(1, {'counts': 1.0}, kind='calibration', role='direct_beam')
    files.measure(2, {'counts': 1.0}, kind='calibration', role='geometry')
    workspace = Workspace(
        proposal='p1', files=files, bind=[function_binding(FIND, find)]
    )
    client = Client(workspace, submitter='anna')

    listed = Listing(client).files(File(kind='calibration', role='direct_beam'))

    assert list(listed.values()) == [beam]
    workspace.close()


def test_files_of_a_marker_with_a_role_are_those_of_the_role_and_those_without_one(
    client: Client, files: FakeFiles
) -> None:
    files.measure(301, {'counts': 1.0}, role='sample')
    files.measure(302, {'counts': 1.0}, role='transmission')
    files.measure(303, {'counts': 1.0})
    listing = Listing(client)

    assert list(listing.files(File(role='sample'))) == ['run 301 (sample)', 'run 303']
    assert list(listing.files(File())) == [
        'run 301 (sample)',
        'run 302 (transmission)',
        'run 303',
    ]

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A listing made from an earlier one equals a listing read in full."""

import threading
from typing import Any

import pytest

from ess.apps import Client, local
from ess.apps.panel import Listing
from ess.apps.testing import CountingClient, FakeFiles
from ess.spec import FileRef, function_binding

from .conftest import FIND, Spec, find


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

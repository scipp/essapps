# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Labels, members, latest, and the listing of records."""

import pytest

from ess.apps import Client, RecordFailed, Status

from .conftest import Gate, load


def test_the_same_request_twice_makes_two_records(client: Client, gate: Gate) -> None:
    gate.open.set()
    request = load(1)
    first, second = client.compute([request, request])

    assert first.id != second.id
    assert first.request == second.request
    assert client.records() == [first, second]


def test_the_keys_of_a_dict_become_members(client: Client, gate: Gate) -> None:
    gate.open.set()
    records = client.compute({'250K': load(1), '260K': load(2)}, label='scan')

    assert {r.member: r for r in client.records(label='scan')} == records
    assert client.latest('scan', member='260K') == records['260K']


def test_latest_without_a_member_is_the_newest_record_of_any_member(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    cold = client.submit(load(1), label='scan', member='250K')
    warm = client.submit(load(2), label='scan', member='260K')

    assert client.latest('scan') == warm
    assert client.latest('scan', member='250K') == cold
    with pytest.raises(KeyError, match="'270K'"):
        client.latest('scan', member='270K')


def test_reading_an_output_of_a_failed_record_raises(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(RecordFailed, match='negative run') as raised:
        client.output(failed, 'value')
    assert raised.value.record == failed.id
    assert raised.value.status is Status.FAILED
    assert raised.value.reason == client.failure(failed)

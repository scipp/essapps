# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Labels, members, latest, and the listing of records."""

import pytest

from ess.apps import Client, RecordFailed, Request, Status

from .conftest import _load


def test_the_same_request_twice_makes_two_records(client: Client) -> None:
    request = _load(1)
    first, second = client.compute([request, request])

    assert first.id != second.id
    assert first.request == second.request
    assert client.records() == [first, second]


def test_the_keys_of_a_dict_become_members(client: Client) -> None:
    records = client.compute({'250K': _load(1), '260K': _load(2)}, label='scan')

    assert {r.member: r for r in client.records(label='scan')} == records
    assert client.latest('scan', member='260K') == records['260K']


def test_latest_without_a_member_is_the_newest_record_of_any_member(
    client: Client,
) -> None:
    cold = client.submit(_load(1), label='scan', member='250K')
    warm = client.submit(_load(2), label='scan', member='260K')

    assert client.latest('scan') == warm
    assert client.latest('scan', member='250K') == cold
    with pytest.raises(KeyError, match="'270K'"):
        client.latest('scan', member='270K')


def test_reading_an_output_of_a_failed_record_raises(
    client: Client, unreadable: Request
) -> None:
    failed = client.compute(unreadable)

    with pytest.raises(RecordFailed, match='file signature not found') as raised:
        client.output(failed, 'value')
    assert raised.value.record == failed.id
    assert raised.value.status is Status.FAILED
    assert raised.value.reason == client.failure(failed)

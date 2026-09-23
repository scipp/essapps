# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The HTTP transport under the client interface.

``RemoteBackend`` satisfies the ``Backend`` protocol; every method is one HTTP
request to a server holding a ``LocalBackend`` (see ``server.py``). ``Client``
does not know which backend it holds, which is why nothing in ``client.py``
imports HTTP; this module is where that import lives.
"""

from __future__ import annotations

import email.message
import shutil
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import TypeAdapter

from .backend import SubmitError, ValidationReport
from .client import Client
from .datastore import Serializers
from .records import StageRecord, StageRequest, Status
from .sources import Dataset
from .spec import OutputRef, SerializedWorkflowSpec, SpecId
from .views import ViewSpec


def _checked(response: httpx.Response) -> httpx.Response:
    """Map the server's error responses to the exceptions ``LocalBackend`` raises."""
    if response.status_code == 404:
        raise KeyError(response.json()['detail'])
    if response.status_code == 409:
        raise ValueError(response.json()['detail'])
    if response.status_code == 422:
        body = response.json()
        if 'reports' in body:
            raise SubmitError(
                {
                    name: ValidationReport.model_validate(report)
                    for name, report in body['reports'].items()
                }
            )
    response.raise_for_status()
    return response


def _filename(headers: httpx.Headers) -> str:
    """The file name a ``content-disposition`` header carries."""
    message = email.message.Message()
    message['content-disposition'] = headers.get('content-disposition', '')
    if (name := message.get_filename()) is None:
        raise ValueError('no filename in content-disposition')
    return name


class RemoteBackend:
    """
    Forwards every ``Backend`` call to a server over HTTP.

    ``downloads`` is where ``output`` lands the files it reads; a directory
    made here is removed by ``close``, one made elsewhere is left for its
    owner.
    """

    def __init__(
        self, url: str, *, downloads: Path | None = None, timeout: float = 30.0
    ) -> None:
        self._client = httpx.Client(base_url=url, timeout=timeout)
        self._owns_downloads = downloads is None
        self.downloads = (
            Path(downloads)
            if downloads is not None
            else Path(tempfile.mkdtemp(prefix='essapps-'))
        )

    def close(self) -> None:
        self._client.close()
        if self._owns_downloads:
            shutil.rmtree(self.downloads, ignore_errors=True)

    def reserve(self, label: str, rule: str) -> None:
        _checked(self._client.post('/reserve', json={'label': label, 'rule': rule}))

    def spec(self, spec_id: SpecId) -> SerializedWorkflowSpec:
        r = _checked(self._client.get(f'/specs/{spec_id.name}/{spec_id.version}'))
        return SerializedWorkflowSpec.model_validate(r.json())

    def specs(self) -> list[SerializedWorkflowSpec]:
        r = _checked(self._client.get('/specs'))
        return TypeAdapter(list[SerializedWorkflowSpec]).validate_python(r.json())

    def datasets(self, proposal: str) -> list[Dataset]:
        r = _checked(self._client.get('/datasets', params={'proposal': proposal}))
        return TypeAdapter(list[Dataset]).validate_python(r.json())

    def validate(
        self, request: StageRequest, group: Mapping[str, StageRequest] | None = None
    ) -> ValidationReport:
        body = {'request': request.model_dump(mode='json')}
        if group is not None:
            body['group'] = {n: r.model_dump(mode='json') for n, r in group.items()}
        r = _checked(self._client.post('/validate', json=body))
        return ValidationReport.model_validate(r.json())

    def submit(self, group: Mapping[str, StageRequest]) -> dict[str, StageRecord]:
        body = {name: r.model_dump(mode='json') for name, r in group.items()}
        r = _checked(self._client.post('/submit', json=body))
        return {name: StageRecord.model_validate(v) for name, v in r.json().items()}

    def record(self, record_id: str) -> StageRecord:
        r = _checked(self._client.get(f'/records/{record_id}'))
        return StageRecord.model_validate(r.json())

    def records(
        self,
        *,
        proposal: str | None = None,
        spec: SpecId | None = None,
        status: Status | None = None,
        label: str | None = None,
        member_key: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[StageRecord]:
        body = {
            'proposal': proposal,
            'spec': spec.model_dump(mode='json') if spec is not None else None,
            'status': status.value if status is not None else None,
            'label': label,
            'member_key': member_key,
            'since': since.isoformat() if since is not None else None,
            'limit': limit,
        }
        r = _checked(self._client.post('/records/query', json=body))
        return TypeAdapter(list[StageRecord]).validate_python(r.json())

    def latest(
        self, label: str, proposal: str, member_key: str | None = None
    ) -> StageRecord | None:
        body = {'label': label, 'proposal': proposal, 'member_key': member_key}
        r = _checked(self._client.post('/records/latest', json=body))
        return None if r.json() is None else StageRecord.model_validate(r.json())

    def batch(self, label: str, proposal: str) -> list[StageRecord]:
        body = {'label': label, 'proposal': proposal}
        r = _checked(self._client.post('/records/batch', json=body))
        return TypeAdapter(list[StageRecord]).validate_python(r.json())

    def members_to_retry(self, label: str, proposal: str) -> list[StageRecord]:
        body = {'label': label, 'proposal': proposal}
        r = _checked(self._client.post('/records/members-to-retry', json=body))
        return TypeAdapter(list[StageRecord]).validate_python(r.json())

    def wait(
        self, record_ids: list[str], *, timeout: float = 60.0, interval: float = 0.2
    ) -> list[StageRecord]:
        """
        Poll ``record`` for each id until every one is terminal.

        The server never blocks on a client (a route only dispatches and
        returns), so waiting is the client's own loop, not a request.
        """
        deadline = time.monotonic() + timeout
        while True:
            records = [self.record(i) for i in record_ids]
            if all(r.status.terminal for r in records):
                return records
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'{[r.id for r in records if not r.status.terminal]}'
                )
            time.sleep(interval)

    def cancel(self, record_id: str) -> None:
        _checked(self._client.post(f'/records/{record_id}/cancel'))

    def recompute(self, record_id: str) -> StageRecord:
        r = _checked(self._client.post(f'/records/{record_id}/recompute'))
        return StageRecord.model_validate(r.json())

    def retry(self, record_id: str) -> StageRecord:
        r = _checked(self._client.post(f'/records/{record_id}/retry'))
        return StageRecord.model_validate(r.json())

    def output(self, ref: OutputRef) -> Any:
        """
        The value of an output: inline from the record, or downloaded.

        A literal comes back with the record. A data output is downloaded on
        every call and read with the serializer its suffix names. Downloading
        every time keeps the semantics of ``LocalBackend``: after ``drop`` the
        output is gone.
        """
        record = self.record(ref.record)
        if ref.output in record.outputs:
            value = record.outputs[ref.output]
            return value[ref.key] if ref.key is not None else value
        return Serializers().load(self.write_out(ref, self.downloads))

    def view(self, ref: OutputRef, spec: ViewSpec) -> dict[str, Any]:
        """As ``LocalBackend.view``, with lists where it has numpy arrays."""
        body = {
            'ref': ref.model_dump(mode='json'),
            'spec': spec.model_dump(mode='json'),
        }
        r = _checked(self._client.post('/view', json=body))
        return r.json()

    def write_out(self, ref: OutputRef, into: Path | None = None) -> Path:
        """
        Without ``into``, the server's own path, meaningful on the filesystem
        the server and the runs it dispatches share, not necessarily on the
        client's. With ``into``, the file the server's data store holds is
        streamed into that folder; this is the data path a Tiled-style
        transport would replace.
        """
        if into is None:
            body = {'ref': ref.model_dump(mode='json')}
            return Path(
                _checked(self._client.post('/write-out', json=body)).json()['path']
            )
        params = {} if ref.key is None else {'key': ref.key}
        url = f'/outputs/{ref.record}/{ref.output}'
        with self._client.stream('GET', url, params=params) as r:
            if r.is_error:
                r.read()
            _checked(r)
            into.mkdir(parents=True, exist_ok=True)
            path = into / f'{ref}{Path(_filename(r.headers)).suffix}'
            with path.open('wb') as file:
                for chunk in r.iter_bytes():
                    file.write(chunk)
        return path

    def drop(self, ref: OutputRef) -> None:
        _checked(self._client.post('/drop', json={'ref': ref.model_dump(mode='json')}))

    def publish(
        self, ref: OutputRef, publisher: str, *, allow_reused: bool = False
    ) -> str:
        body = {
            'ref': ref.model_dump(mode='json'),
            'publisher': publisher,
            'allow_reused': allow_reused,
        }
        r = _checked(self._client.post('/publish', json=body))
        return r.json()['pid']

    def provenance(self, record_id: str) -> dict[str, Any]:
        r = _checked(self._client.get(f'/provenance/{record_id}'))
        return r.json()


def remote(
    url: str,
    *,
    instrument: str,
    proposal: str,
    submitter: str,
    downloads: Path | None = None,
) -> Client:
    """Remote mode: client here, backend behind ``url``; see :func:`local`."""
    return Client(
        RemoteBackend(url, downloads=downloads),
        instrument=instrument,
        proposal=proposal,
        submitter=submitter,
    )

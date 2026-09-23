# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend as an HTTP service.

One route per ``Backend`` protocol method; the routes are the protocol, not a
resource model, because the client interface is the API and HTTP only carries
it.

The backend is single-threaded by design: a single writer to the record
store. The app holds one ``threading.Lock``, and every route body and the
poller run under it, so requests are serialized. Long work in a route (an
in-process session run) blocks the others; shared mode runs every request in
its own throwaway process, so a route only dispatches and returns quickly.

A poller thread, started and stopped by the app's lifespan, calls
``backend.poll()`` every ``poll_interval`` under the lock: that is where
dispatched runs are reconciled and waiting ones are dispatched. Nothing on
the protocol drives scheduling from a client; ``wait`` polls the server
instead, so a request never blocks it.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .backend import LocalBackend, SubmitError, ValidationReport
from .records import RunRecord, RunRequest, Status
from .sources import Dataset
from .spec import OutputRef, SerializedWorkflowSpec, SpecId
from .views import ViewSpec


class ValidateRequest(BaseModel):
    request: RunRequest
    group: dict[str, RunRequest] | None = None


class RecordsQuery(BaseModel):
    proposal: str | None = None
    spec: SpecId | None = None
    status: Status | None = None
    label: str | None = None
    member_key: str | None = None
    since: datetime | None = None
    limit: int | None = None


class LatestQuery(BaseModel):
    label: str
    proposal: str
    member_key: str | None = None


class LabelQuery(BaseModel):
    """A label and its proposal: what ``batch`` and ``members_to_retry`` need."""

    label: str
    proposal: str


class RefBody(BaseModel):
    ref: OutputRef


class ViewRequest(BaseModel):
    ref: OutputRef
    spec: ViewSpec


class PublishRequest(BaseModel):
    ref: OutputRef
    publisher: str
    allow_reused: bool = False


class ReserveRequest(BaseModel):
    label: str
    rule: str


def _jsonable_view(rendered: dict[str, Any]) -> dict[str, Any]:
    """``view`` returns numpy arrays; JSON only carries lists."""
    return rendered | {
        'values': rendered['values'].tolist(),
        'coords': {name: coord.tolist() for name, coord in rendered['coords'].items()},
    }


def create_app(backend: LocalBackend, *, poll_interval: float = 0.2) -> FastAPI:
    lock = threading.Lock()
    stop = threading.Event()

    def poll_forever() -> None:
        while True:
            with lock:
                backend.poll()
            if stop.wait(poll_interval):
                return

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        thread = threading.Thread(target=poll_forever, daemon=True)
        thread.start()
        yield
        stop.set()
        thread.join()

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(SubmitError)
    def _submit_error(request: Request, exc: SubmitError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                'detail': 'refused',
                'reports': {
                    n: r.model_dump(mode='json') for n, r in exc.reports.items()
                },
            },
        )

    @app.exception_handler(LookupError)
    def _lookup_error(request: Request, exc: LookupError) -> JSONResponse:
        # str(KeyError('x')) is "'x'"; the message itself is the argument.
        detail = exc.args[0] if exc.args else str(exc)
        return JSONResponse(status_code=404, content={'detail': str(detail)})

    @app.exception_handler(ValueError)
    def _value_error(request: Request, exc: ValueError) -> JSONResponse:
        # A pydantic ValidationError raised inside a route is also a ValueError
        # and lands here as 409; FastAPI's own request-body validation runs
        # before the route and stays a plain 422.
        return JSONResponse(status_code=409, content={'detail': str(exc)})

    @app.get('/specs')
    def list_specs() -> list[SerializedWorkflowSpec]:
        with lock:
            return backend.specs()

    @app.get('/specs/{name}/{version}')
    def get_spec(name: str, version: int) -> SerializedWorkflowSpec:
        with lock:
            return backend.spec(SpecId(name=name, version=version))

    @app.get('/datasets')
    def list_datasets(proposal: str) -> list[Dataset]:
        with lock:
            return backend.datasets(proposal)

    @app.post('/validate')
    def validate(body: ValidateRequest) -> ValidationReport:
        with lock:
            return backend.validate(body.request, body.group)

    @app.post('/submit')
    def submit(group: dict[str, RunRequest]) -> dict[str, RunRecord]:
        with lock:
            return backend.submit(group)

    @app.get('/records/{record_id}')
    def get_record(record_id: str) -> RunRecord:
        with lock:
            return backend.record(record_id)

    @app.post('/records/query')
    def query_records(body: RecordsQuery) -> list[RunRecord]:
        with lock:
            return backend.records(
                proposal=body.proposal,
                spec=body.spec,
                status=body.status,
                label=body.label,
                member_key=body.member_key,
                since=body.since,
                limit=body.limit,
            )

    @app.post('/records/latest')
    def latest(body: LatestQuery) -> RunRecord | None:
        with lock:
            return backend.latest(body.label, body.proposal, member_key=body.member_key)

    @app.post('/records/batch')
    def batch(body: LabelQuery) -> list[RunRecord]:
        with lock:
            return backend.batch(body.label, body.proposal)

    @app.post('/records/members-to-retry')
    def members_to_retry(body: LabelQuery) -> list[RunRecord]:
        with lock:
            return backend.members_to_retry(body.label, body.proposal)

    @app.post('/records/{record_id}/cancel', status_code=204)
    def cancel(record_id: str) -> None:
        with lock:
            backend.cancel(record_id)

    @app.post('/records/{record_id}/recompute')
    def recompute(record_id: str) -> RunRecord:
        with lock:
            return backend.recompute(record_id)

    @app.post('/records/{record_id}/retry')
    def retry(record_id: str) -> RunRecord:
        with lock:
            return backend.retry(record_id)

    @app.get('/outputs/{record_id}/{output}')
    def get_output(record_id: str, output: str, key: str | None = None) -> FileResponse:
        ref = OutputRef(record=record_id, output=output, key=key)
        with lock:
            path = backend.data.path(ref)
        return FileResponse(path, filename=path.name)

    @app.post('/view')
    def get_view(body: ViewRequest) -> dict[str, Any]:
        with lock:
            rendered = backend.view(body.ref, body.spec)
        return _jsonable_view(rendered)

    @app.post('/write-out')
    def write_out(body: RefBody) -> dict[str, str]:
        with lock:
            path = backend.write_out(body.ref)
        return {'path': str(path)}

    @app.post('/drop', status_code=204)
    def drop(body: RefBody) -> None:
        with lock:
            backend.drop(body.ref)

    @app.post('/publish')
    def publish(body: PublishRequest) -> dict[str, str]:
        with lock:
            pid = backend.publish(
                body.ref, body.publisher, allow_reused=body.allow_reused
            )
        return {'pid': pid}

    @app.get('/provenance/{record_id}')
    def provenance(record_id: str) -> dict[str, Any]:
        with lock:
            return backend.provenance(record_id)

    @app.post('/reserve', status_code=204)
    def reserve(body: ReserveRequest) -> None:
        with lock:
            backend.reserve(body.label, body.rule)

    return app


def serve(backend: LocalBackend, *, host: str = '127.0.0.1', port: int = 8000) -> None:
    """Run a backend as a service; blocks until interrupted."""
    uvicorn.run(create_app(backend), host=host, port=port)

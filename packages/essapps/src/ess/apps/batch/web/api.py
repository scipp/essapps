# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page over HTTP: :func:`create_app` serves a :class:`~.server.Server`
as a JSON API, and the page that uses it.

Each action of the page is a ``POST`` that answers ``{'notice': ...}`` or a
422 with ``{'message', 'errors'}``, after which the page asks for the state
again. ``GET /api/state?after=<version>`` answers ``{'version': n}`` alone
while nothing changed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ...errors import SubmitError
from .. import Batch, CorrectedFiles
from .server import Draw, Problem, Server

STATIC = Path(__file__).parent / 'static'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _download(content: bytes, name: str, media: str) -> Response:
    return Response(
        content,
        media_type=media,
        headers={'Content-Disposition': f'attachment; filename="{name}"'},
    )


def create_app(
    batch: Batch,
    *,
    info: Sequence[str] = (),
    draw: Draw | None = None,
    files: CorrectedFiles | None = None,
    marks: type[BaseModel] | None = None,
    folder: str = '',
) -> FastAPI:
    """
    The JSON API of ``batch`` and the page that uses it, at ``/``. The
    arguments are those of :class:`Server`. ``/docs`` lists the API. The page
    loads libraries that ``sh static/vendor.sh`` fetches into
    ``static/vendor``.
    """
    server = Server(
        batch, info=info, draw=draw, files=files, marks=marks, folder=folder
    )
    app = FastAPI(title=f'{batch.spec.title}: batch')
    app.state.server = server

    @app.exception_handler(SubmitError)
    def refused(_: Request, error: SubmitError) -> JSONResponse:
        return JSONResponse(server.refusal(error), status_code=422)

    @app.exception_handler(Problem)
    def problem(_: Request, error: Problem) -> JSONResponse:
        return JSONResponse({'message': str(error), 'errors': []}, status_code=422)

    @app.get('/api/state')
    def state(after: int = 0) -> dict[str, Any]:
        """The state, or ``{'version': n}`` alone if it is still that of ``after``."""
        return server.poll(after)

    app.post('/api/scan')(server.scan)
    app.post('/api/cells')(server.edit)
    app.post('/api/cells/revert')(server.revert)
    app.post('/api/rows/remove')(server.remove)
    app.post('/api/rows/restore')(server.restore)
    app.post('/api/rows/add')(server.add)
    app.post('/api/reduce')(server.reduce)
    app.post('/api/cancel')(server.cancel)
    app.post('/api/settings')(server.edit_settings)
    app.get('/api/rows/{key}')(server.row)
    app.post('/api/rows/{key}/use-settings')(server.use_settings)
    app.post('/api/rows/{key}/tweak')(server.edit_tweak)
    app.post('/api/rows/{key}/tweak/submit')(server.submit_tweak)
    app.post('/api/runs/cells')(server.correct)
    app.post('/api/runs/cells/revert')(server.revert_run)

    @app.get('/api/plot')
    def plot(keys: Annotated[list[str] | None, Query()] = None) -> dict[str, Any]:
        return server.plot(keys or [])

    @app.get('/api/table.{suffix}')
    def table_file(suffix: Literal['csv', 'xlsx']) -> Response:
        media = 'text/csv' if suffix == 'csv' else _XLSX
        return _download(server.table_file(f'.{suffix}'), f'samples.{suffix}', media)

    @app.post('/api/table')
    def load_table(name: str, content: Annotated[bytes, Body()]) -> dict[str, Any]:
        return server.load_table(name, content)

    @app.get('/api/settings.yaml')
    def settings_file() -> Response:
        return _download(server.settings_file(), 'settings.yaml', 'application/yaml')

    @app.post('/api/settings.yaml')
    def load_settings(name: str, content: Annotated[bytes, Body()]) -> dict[str, Any]:
        return server.load_settings(name, content)

    @app.get('/api/results.zip')
    def results_file(keys: Annotated[list[str] | None, Query()] = None) -> Response:
        return _download(
            server.results_file(keys or []), 'results.zip', 'application/zip'
        )

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/', include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    return app

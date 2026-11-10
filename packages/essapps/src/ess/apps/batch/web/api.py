# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page over HTTP: :func:`create_app` serves a :class:`~.server.Server`
as a JSON API, and the page that uses it.

Each action of the page is a ``POST`` that answers with the
:class:`~.models.State` after it, or a 422 with ``{'message': ...}``; the
reasons of a refused reduction are in the state. ``GET /api/state`` with the
``after`` version and the ``start`` the page has answers 204 while nothing
changed. A row is named by its key in the body or the query, never in the
path, since a key may hold any character.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import Batch, CorrectedFiles
from .models import (
    Key,
    Keys,
    Plot,
    RowDetail,
    RowEdit,
    Runs,
    State,
    Words,
)
from .server import Draw, Problem, Server

STATIC = Path(__file__).parent / 'static'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

type Keyed = Annotated[list[str] | None, Query()]


def _download(content: bytes, name: str, media: str) -> Response:
    return Response(
        content,
        media_type=media,
        headers={'Content-Disposition': f'attachment; filename="{name}"'},
    )


def create_app(
    batch: Batch,
    *,
    title: str | None = None,
    words: Words | None = None,
    name: str | None = None,
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
        batch,
        title=title,
        words=words,
        name=name,
        info=info,
        draw=draw,
        files=files,
        marks=marks,
        folder=folder,
    )
    app = FastAPI(title=f'{server.title}: batch')
    app.state.server = server

    @app.exception_handler(Problem)
    def problem(_: Request, error: Problem) -> JSONResponse:
        return JSONResponse({'message': str(error)}, status_code=422)

    @app.get('/api/state', responses={204: {'description': 'Nothing changed.'}})
    def state(after: int = 0, start: str = '') -> Any:
        """The state, or 204 if it is that of version ``after`` of ``start``."""
        current = server.poll(after, start)
        return Response(status_code=204) if current is None else current

    # Each action answers with the state after it.
    def action(path: str, method: Any) -> None:
        app.post(path, response_model=State)(method)

    action('/api/scan', server.scan)
    action('/api/cells', server.edit)
    action('/api/cells/revert', server.revert)
    action('/api/rows/restore', server.restore)
    action('/api/reduce', server.reduce)
    action('/api/cancel', server.cancel)
    action('/api/settings', server.edit_settings)
    action('/api/runs/cells', server.correct)
    action('/api/runs/cells/revert', server.revert_run)

    @app.post('/api/rows/remove')
    def remove(body: Keys) -> State:
        return server.remove(body.keys)

    @app.post('/api/rows/add')
    def add(body: Key) -> State:
        return server.add(body.key)

    @app.post('/api/row/use-settings')
    def use_settings(body: Key) -> State:
        """Take the settings of the result of row ``key`` for all rows."""
        return server.use_settings(body.key)

    @app.post('/api/row/tweak/submit')
    def submit_tweak(body: Key) -> State:
        """Reduce row ``key`` alone, with its own form."""
        return server.submit_tweak(body.key)

    @app.get('/api/row')
    def row(key: str) -> RowDetail:
        """What the result of row ``key`` was reduced with, and its own form."""
        return server.row(key)

    @app.post('/api/row/tweak')
    def edit_tweak(edit: RowEdit) -> RowDetail:
        """Edit the form of the reduction of row ``key`` alone."""
        return server.edit_tweak(edit.key, edit)

    @app.get('/api/runs')
    def runs() -> Runs:
        return server.runs()

    @app.get('/api/plot')
    def plot(keys: Keyed = None) -> Plot:
        return server.plot(keys or [])

    @app.get('/api/table.{suffix}', response_class=Response)
    def table_file(suffix: Literal['csv', 'xlsx']) -> Response:
        media = 'text/csv' if suffix == 'csv' else _XLSX
        name = f'{server.words.many}.{suffix}'
        return _download(server.table_file(f'.{suffix}'), name, media)

    @app.post('/api/table')
    def load_table(name: str, content: Annotated[bytes, Body()]) -> State:
        return server.load_table(name, content)

    @app.get('/api/settings.yaml', response_class=Response)
    def settings_file() -> Response:
        return _download(server.settings_file(), 'settings.yaml', 'application/yaml')

    @app.post('/api/settings.yaml')
    def load_settings(name: str, content: Annotated[bytes, Body()]) -> State:
        return server.load_settings(name, content)

    @app.get('/api/results.zip', response_class=Response)
    def results_file(keys: Keyed = None) -> Response:
        content = server.results_file(keys or [])
        return _download(content, 'results.zip', 'application/zip')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    @app.get('/', include_in_schema=False)
    def page() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    return app

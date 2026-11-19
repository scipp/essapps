# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The pages of the batches of a workspace over HTTP: :func:`create_app` serves
the :class:`~.server.Server` of each batch as a JSON API, and the page that
uses it, at ``/batches/<label>/``.

Each action of a page is a ``POST`` that answers with the
:class:`~.models.State` after it, or a 422 with ``{'message': ...}``; the
reasons of a refused reduction are in the state. ``GET api/state`` with the
``after`` version and the ``start`` the page has answers 204 while nothing
changed. The page and the API of a label that is no batch answer 404; the
static files are served under any label. A row is named by its key in
the body or the query, never in the path, since a key may hold any character;
the keys of many rows go in the body, since a URL has a limit. A label is one
segment of a path (:func:`~ess.apps.batch.manager.check_label`). The page asks
for ``api/...`` relative to its own URL, and ``/`` redirects to a page by a
relative URL, so the app can be mounted under a path prefix. Answers are
gzipped for a client that accepts it.
"""

import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from ..auto import every
from .models import (
    CellEdit,
    Cells,
    Edit,
    Key,
    Keys,
    On,
    Plot,
    Reduce,
    RowDetail,
    Runs,
    State,
)
from .pages import Missing, Pages
from .server import Problem, Server

STATIC = Path(__file__).parent / 'static'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _download(content: bytes, name: str, media: str) -> Response:
    return Response(
        content,
        media_type=media,
        headers={'Content-Disposition': f'attachment; filename="{name}"'},
    )


def create_app(pages: Pages) -> FastAPI:
    """
    The JSON API of the batches of ``pages`` and the page that uses it, at
    ``/batches/<label>/``; ``/`` redirects to the page of
    :meth:`Pages.home <.pages.Pages.home>`. ``/docs`` lists the API. The page
    loads libraries that ``sh static/vendor.sh`` fetches into
    ``static/vendor``.

    The app owns the timer of Follow: a thread that the app's lifespan starts,
    and stops at its end once a step that runs has finished. It calls
    :meth:`Pages.tick <.pages.Pages.tick>` every ``pages.follow.interval``
    seconds, which steps the batch that follows, if one does. A parent app
    that mounts this one runs its lifespan, since Starlette does not (see
    ``README.md``).
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with every(pages.follow.interval, itertools.repeat(pages.tick)):
            yield

    app = FastAPI(title=pages.title or 'Batches', lifespan=lifespan)
    app.state.pages = pages
    # The state is about 0.6 kB per row and repeats much.
    app.add_middleware(GZipMiddleware)

    @app.exception_handler(Problem)
    def problem(_: Request, error: Problem) -> JSONResponse:
        return JSONResponse({'message': str(error)}, status_code=422)

    @app.exception_handler(Missing)
    def missing(_: Request, error: Missing) -> JSONResponse:
        return JSONResponse({'message': str(error)}, status_code=404)

    @app.get('/', include_in_schema=False)
    def home() -> Response:
        label = pages.home()
        if label is None:
            return PlainTextResponse('This workspace has no batch.', status_code=404)
        return RedirectResponse(f'batches/{quote(label, safe="")}/')

    # The routes of the page of one batch, by its label. FastAPI reads the
    # annotation Page, a name of this function, so this module does not
    # import annotations from __future__.
    page = APIRouter(prefix='/batches/{label}')
    Page = Annotated[Server, Depends(pages.page)]

    @page.get('/', include_in_schema=False)
    def index(_: Page) -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    @page.get('/api/state', responses={204: {'description': 'Nothing changed.'}})
    def state(server: Page, after: int = 0, start: str = '') -> Any:
        """The state, or 204 if it is that of version ``after`` of ``start``."""
        current = server.poll(after, start)
        return Response(status_code=204) if current is None else current

    # Each action answers with the state after it.

    @page.post('/api/scan')
    def scan(server: Page) -> State:
        return server.scan()

    @page.post('/api/cells')
    def edit(server: Page, body: CellEdit) -> State:
        return server.edit(body)

    @page.post('/api/cells/revert')
    def revert(server: Page, body: Cells) -> State:
        return server.revert(body)

    @page.post('/api/rows/remove')
    def remove(server: Page, body: Keys) -> State:
        return server.remove(body.keys)

    @page.post('/api/rows/restore')
    def restore(server: Page) -> State:
        return server.restore()

    @page.post('/api/rows/add')
    def add(server: Page, body: Key) -> State:
        return server.add(body.key)

    @page.post('/api/reduce')
    def reduce(server: Page, body: Reduce) -> State:
        return server.reduce(body)

    @page.post('/api/cancel')
    def cancel(server: Page) -> State:
        return server.cancel()

    @page.post('/api/follow')
    def follow_files(server: Page, body: On) -> State:
        """Make this batch the one that follows, with a step at once, or stop."""
        return server.follow(body.on)

    @page.post('/api/settings')
    def edit_settings(server: Page, body: Edit) -> State:
        return server.edit_settings(body)

    @page.post('/api/settings/apply')
    def apply_settings(server: Page) -> State:
        return server.apply_settings()

    @page.post('/api/settings/discard')
    def discard_settings(server: Page) -> State:
        return server.discard_settings()

    @page.post('/api/runs/cells')
    def correct(server: Page, body: CellEdit) -> State:
        return server.correct(body)

    @page.post('/api/runs/cells/revert')
    def revert_run(server: Page, body: Cells) -> State:
        return server.revert_run(body)

    @page.post('/api/row/use-settings')
    def use_settings(server: Page, body: Key) -> State:
        """Show the settings of the result of row ``key`` in the form."""
        return server.use_settings(body.key)

    @page.get('/api/row')
    def row(server: Page, key: str) -> RowDetail:
        """What the result of row ``key`` was reduced with."""
        return server.row(key)

    @page.get('/api/runs')
    def runs(server: Page) -> Runs:
        return server.runs()

    @page.post('/api/plot')
    def plot(server: Page, body: Keys) -> Plot:
        """The plot of the results of the rows ``keys``, or of the first ones."""
        return server.plot(body.keys)

    @page.get('/api/table.{suffix}', response_class=Response)
    def table_file(server: Page, suffix: Literal['csv', 'xlsx']) -> Response:
        media = 'text/csv' if suffix == 'csv' else _XLSX
        name = f'{server.words.many}.{suffix}'
        return _download(server.table_file(f'.{suffix}'), name, media)

    @page.post('/api/table')
    def load_table(server: Page, name: str, content: Annotated[bytes, Body()]) -> State:
        return server.load_table(name, content)

    @page.get('/api/settings.yaml', response_class=Response)
    def settings_file(server: Page) -> Response:
        return _download(server.settings_file(), 'settings.yaml', 'application/yaml')

    @page.post('/api/settings.yaml')
    def load_settings(
        server: Page, name: str, content: Annotated[bytes, Body()]
    ) -> State:
        return server.load_settings(name, content)

    @page.post('/api/results.zip', response_class=Response)
    def results_file(server: Page, body: Keys) -> Response:
        """The results of the done rows among ``keys``, or of all, as a zip."""
        content = server.results_file(body.keys)
        return _download(content, 'results.zip', 'application/zip')

    app.include_router(page)
    app.mount('/batches/{label}/static', StaticFiles(directory=STATIC), name='static')
    return app

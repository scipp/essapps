# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The pages of the batches of a workspace over HTTP: :func:`create_app` serves
the :class:`~.page.Page` of each batch as a JSON API, and the page that
uses it, at ``/batches/<label>/``.

Each action of a page is a ``POST`` that answers with the
:class:`~.models.State` after it, or a 422 with ``{'message': ...}``; the
reasons of a refused reduction are in the state. ``GET api/state`` with the
``after`` version and the ``start`` the page has answers 204 while nothing
changed. The API of a label that is no batch answers 404, and of a batch
that cannot be made 422; the page is served for any label, and shows the
batches with that message instead. The
routes of the workspace, such as ``api/batches``, are served at ``/api/...``
and again under any label, which they ignore, so that a page asks for them as
for its own; so are the static files. A row is named by its key in the body or
the query, never in the path, since a key may hold any character; the keys of
many rows go in the body, since a URL has a limit. A label is one
segment of a path (:func:`~ess.apps.batch.manager.check_label`). The page asks
for ``api/...`` relative to its own URL, and ``/`` redirects to a page by a
relative URL, so the app can be mounted under a path prefix. If it has no
batch to go to, ``/`` is the page without a batch, which lists the batches
and asks for the routes of the workspace only. Answers are gzipped for a
client that accepts it.
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
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from ..auto import every
from .models import (
    BatchCounts,
    Batches,
    CellEdit,
    Cells,
    Edit,
    Ends,
    HeaderEdit,
    Key,
    Keys,
    Made,
    Moved,
    NewBatch,
    Next,
    Notice,
    On,
    Plot,
    Reduce,
    RowDetail,
    Runs,
    SourceChoice,
    State,
)
from .page import Missing, Page, Problem
from .pages import Pages

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
    :meth:`Pages.home <.pages.Pages.home>`, or is the page without a batch if
    that gives none. ``/docs`` lists the API. The page
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
        # The answer depends on the batches, so the browser keeps none, not
        # even for Back.
        label, kept = pages.home(), {'Cache-Control': 'no-store'}
        if label is None:
            return FileResponse(STATIC / 'index.html', headers=kept)
        return RedirectResponse(f'batches/{quote(label, safe="")}/', headers=kept)

    # The routes of the page of one batch, by its label. FastAPI reads the
    # annotation PageOfLabel, a name of this function, so this module does not
    # import annotations from __future__.
    batch = APIRouter(prefix='/batches/{label}')
    PageOfLabel = Annotated[Page, Depends(pages.page)]

    # The page of any label: its first request of the state says if the batch
    # cannot be shown, and the page then shows the batches instead.
    @batch.get('/', include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    @batch.get('/api/state', responses={204: {'description': 'Nothing changed.'}})
    def state(page: PageOfLabel, after: int = 0, start: str = '') -> Any:
        """The state, or 204 if it is that of version ``after`` of ``start``."""
        current = page.poll(after, start)
        return Response(status_code=204) if current is None else current

    # Each action answers with the state after it.

    @batch.post('/api/scan')
    def scan(page: PageOfLabel) -> State:
        return page.scan()

    @batch.post('/api/cells')
    def edit(page: PageOfLabel, body: CellEdit) -> State:
        return page.edit(body)

    @batch.post('/api/cells/revert')
    def revert(page: PageOfLabel, body: Cells) -> State:
        return page.revert(body)

    @batch.post('/api/rows/remove')
    def remove(page: PageOfLabel, body: Keys) -> State:
        return page.remove(body.keys)

    @batch.post('/api/rows/restore')
    def restore(page: PageOfLabel) -> State:
        return page.restore()

    @batch.post('/api/rows/add')
    def add(page: PageOfLabel, body: Key) -> State:
        return page.add(body.key)

    @batch.post('/api/reduce')
    def reduce(page: PageOfLabel, body: Reduce) -> State:
        return page.reduce(body)

    @batch.post('/api/cancel')
    def cancel(page: PageOfLabel) -> State:
        return page.cancel()

    @batch.post('/api/follow')
    def follow_files(page: PageOfLabel, body: On) -> State:
        """Make this batch the one that follows, with a step at once, or stop."""
        return page.follow(body.on)

    @batch.post('/api/settings')
    def edit_settings(page: PageOfLabel, body: Edit) -> State:
        return page.edit_settings(body)

    @batch.post('/api/settings/apply')
    def apply_settings(page: PageOfLabel) -> State:
        return page.apply_settings()

    @batch.post('/api/settings/discard')
    def discard_settings(page: PageOfLabel) -> State:
        return page.discard_settings()

    @batch.post('/api/row/use-settings')
    def use_settings(page: PageOfLabel, body: Key) -> State:
        """Show the settings of the result of row ``key`` in the form."""
        return page.use_settings(body.key)

    @batch.get('/api/row')
    def row(page: PageOfLabel, key: str) -> RowDetail:
        """What the result of row ``key`` was reduced with."""
        return page.row(key)

    @batch.post('/api/plot')
    def plot(page: PageOfLabel, body: Keys) -> Plot:
        """The plot of the results of the rows ``keys``, or of the first ones."""
        return page.plot(body.keys)

    @batch.get('/api/table.{suffix}', response_class=Response)
    def table_file(page: PageOfLabel, suffix: Literal['csv', 'xlsx']) -> Response:
        media = 'text/csv' if suffix == 'csv' else _XLSX
        name = f'{page.words.many}.{suffix}'
        return _download(page.table_file(f'.{suffix}'), name, media)

    @batch.post('/api/table')
    def load_table(
        page: PageOfLabel, name: str, content: Annotated[bytes, Body()]
    ) -> State:
        return page.load_table(name, content)

    @batch.get('/api/settings.yaml', response_class=Response)
    def settings_file(page: PageOfLabel) -> Response:
        return _download(page.settings_file(), 'settings.yaml', 'application/yaml')

    @batch.post('/api/settings.yaml')
    def load_settings(
        page: PageOfLabel, name: str, content: Annotated[bytes, Body()]
    ) -> State:
        return page.load_settings(name, content)

    @batch.post('/api/batch')
    def change(page: PageOfLabel, body: HeaderEdit) -> State:
        """Change the title, description and scope of the batch."""
        return page.change(body)

    @batch.post('/api/batch/added')
    def added(page: PageOfLabel, body: Ends) -> int:
        """How many rows the scope of ``body`` adds that Follow would reduce."""
        return page.added(body)

    @batch.post('/api/batch/moved')
    def moved(page: PageOfLabel, body: Key) -> Moved:
        """What starting the next batch at row ``key`` moves."""
        return page.moved(body.key)

    @batch.post('/api/batch/next')
    def start_next(page: PageOfLabel, body: Next) -> Made:
        """End the batch before a row, and start the next batch there."""
        return page.start_next(body)

    @batch.post('/api/batch/remove')
    def remove_batch(label: str) -> dict[str, str]:
        """Remove the batch; its results stay in the workspace."""
        pages.remove(label)
        return {}

    @batch.post('/api/results.zip', response_class=Response)
    def results_file(page: PageOfLabel, body: Keys) -> Response:
        """The results of the done rows among ``keys``, or of all, as a zip."""
        content = page.results_file(body.keys)
        return _download(content, 'results.zip', 'application/zip')

    # The routes of the workspace, the same from every page.
    workspace = APIRouter()

    @workspace.get('/api/batches')
    def batches() -> Batches:
        return pages.batches()

    @workspace.get('/api/batches/counts')
    def counts() -> BatchCounts:
        """The rows of each batch by status; it scans every batch."""
        return pages.counts()

    @workspace.post('/api/batches')
    def create(body: NewBatch) -> Made:
        return pages.create(body)

    @workspace.get('/api/sources')
    def sources() -> list[SourceChoice]:
        """What a new batch can be made with."""
        return pages.sources()

    @workspace.get('/api/runs')
    def runs() -> Runs:
        return pages.runs()

    @workspace.post('/api/runs/cells')
    def correct(body: CellEdit) -> Notice:
        """Correct runs; the page of each batch scans at its next poll."""
        return pages.correct(body)

    @workspace.post('/api/runs/cells/revert')
    def revert_run(body: Cells) -> Notice:
        return pages.revert_run(body)

    app.include_router(batch)
    app.include_router(workspace)
    app.include_router(workspace, prefix='/batches/{label}')
    app.mount('/batches/{label}/static', StaticFiles(directory=STATIC), name='static')
    app.mount('/static', StaticFiles(directory=STATIC), name='static-home')
    return app

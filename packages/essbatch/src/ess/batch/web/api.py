# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The page of each batch over HTTP: :func:`create_app`.

The routes of one batch are under ``/batches/<label>/``. The routes of the
workspace, such as ``api/batches``, are served at ``/api/...`` and again under
every label, which they ignore. So the page requests every route relative to
its own URL, and the app can be mounted under a path prefix. A request
specifies a row by its key in the body or the query, never in the path, since
a key may contain any character.

How the page and the server interact: ``README.md`` in this folder.
"""

import itertools
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
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

from .errors import NotFound, Rejected
from .models import (
    BatchCounts,
    Batches,
    CellEdit,
    Cells,
    Edit,
    HeaderEdit,
    Key,
    Keys,
    Made,
    MakeColumn,
    MakeSetting,
    NewBatch,
    Notice,
    Plot,
    Reduce,
    RemoveBatch,
    RowDetail,
    Runs,
    SourceChoice,
    SourceRows,
    State,
    Value,
)
from .page import Page
from .pages import Pages

STATIC = Path(__file__).parent / 'static'
_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


@contextmanager
def every(seconds: float, actions: Iterable[Callable[[], object]]) -> Iterator[None]:
    """
    Call ``actions`` in turn from a thread, one every ``seconds``, until all are
    called or the block ends. The end of the block waits for a running action.
    """
    stop = threading.Event()

    def call() -> None:
        for action in actions:
            if stop.wait(seconds):
                return
            action()

    thread = threading.Thread(target=call, name='every')
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def _download(content: bytes, name: str, media: str) -> Response:
    return Response(
        content,
        media_type=media,
        headers={'Content-Disposition': f'attachment; filename="{name}"'},
    )


def serve(app: FastAPI, *, port: int) -> None:
    """
    Serve ``app`` on ``http://localhost:port`` until interrupted.

    Exits with a message if ``static/vendor`` is missing.
    """
    import uvicorn

    if not (STATIC / 'vendor').is_dir():
        raise SystemExit(f'The page needs its libraries: sh {STATIC / "vendor.sh"}')
    uvicorn.run(app, host='localhost', port=port)


def answer_errors(app: FastAPI) -> None:
    """Answer a :class:`Rejected` with 422 and a :class:`NotFound` with 404."""

    @app.exception_handler(Rejected)
    def problem(_: Request, error: Rejected) -> JSONResponse:
        return JSONResponse({'message': str(error)}, status_code=422)

    @app.exception_handler(NotFound)
    def missing(_: Request, error: NotFound) -> JSONResponse:
        return JSONResponse({'message': str(error)}, status_code=404)


def create_app(pages: Pages) -> FastAPI:
    """
    The app that serves the page of each batch of ``pages`` and its JSON API.

    The app's lifespan runs a thread that calls
    :meth:`Pages.tick <.pages.Pages.tick>` every ``pages.interval`` seconds.
    A parent app that mounts this app must run its lifespan, since Starlette
    does not (see ``README.md``).
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with every(pages.interval, itertools.repeat(pages.tick)):
            yield

    app = FastAPI(title=pages.title or 'Batches', lifespan=lifespan)
    app.state.pages = pages
    # The state is about 0.6 kB per row and repeats much.
    app.add_middleware(GZipMiddleware)

    answer_errors(app)

    @app.get('/', include_in_schema=False)
    def home() -> Response:
        # The answer depends on the batches, so the browser must not cache it,
        # not even for Back.
        label, kept = pages.home(), {'Cache-Control': 'no-store'}
        if label is None:
            return FileResponse(STATIC / 'index.html', headers=kept)
        return RedirectResponse(f'batches/{quote(label, safe="")}/', headers=kept)

    # The routes of the page of one batch, by its label. FastAPI evaluates the
    # annotation PageOfLabel, a local name of this function, so this module
    # must not import annotations from __future__.
    batch = APIRouter(prefix='/batches/{label}')
    PageOfLabel = Annotated[Page, Depends(pages.page)]

    # Served for any label. If the batch cannot be shown, the first request of
    # the state fails, and the page then shows the list of batches instead.
    @batch.get('/', include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / 'index.html')

    @batch.get('/api/state', responses={204: {'description': 'Nothing changed.'}})
    def state(page: PageOfLabel, after: int = 0, start: str = '') -> Any:
        """The state, or 204 if it is that of revision ``after`` of ``start``."""
        current = page.poll(after, start)
        return Response(status_code=204) if current is None else current

    @batch.post('/api/relist')
    def relist(page: PageOfLabel) -> State:
        """List the runs again; no row changes."""
        return page.relist()

    @batch.post('/api/cells')
    def edit(page: PageOfLabel, body: CellEdit) -> State:
        return page.edit(body)

    @batch.post('/api/rows/remove')
    def remove(page: PageOfLabel, body: Keys) -> State:
        return page.remove(body.keys)

    @batch.post('/api/rows/add-empty')
    def add_empty_row(page: PageOfLabel, body: Key) -> State:
        return page.add_empty_row(body.key)

    @batch.get('/api/rows/available')
    def available_rows(page: PageOfLabel) -> SourceRows:
        """The rows of the row source that the batch table lacks."""
        return page.available_rows()

    @batch.post('/api/rows/add')
    def add_rows(page: PageOfLabel, body: Keys) -> State:
        """Add rows of the row source, as the source makes them now."""
        return page.add_rows(body.keys)

    @batch.post('/api/settings/column')
    def make_column(page: PageOfLabel, body: MakeColumn) -> State:
        """Make a setting a column with empty cells."""
        return page.make_column(body.name)

    @batch.get('/api/columns/values')
    def values(page: PageOfLabel, name: str) -> list[Value]:
        """The different values of a column, each with a row that has it."""
        return page.values(name)

    @batch.post('/api/columns/setting')
    def make_setting(page: PageOfLabel, body: MakeSetting) -> State:
        """Make a column a setting, with the value of one row or of all rows."""
        return page.make_setting(body)

    @batch.post('/api/reduce')
    def reduce(page: PageOfLabel, body: Reduce) -> State:
        return page.reduce(body)

    @batch.post('/api/cancel')
    def cancel(page: PageOfLabel) -> State:
        return page.cancel()

    @batch.post('/api/clear')
    def clear(page: PageOfLabel, body: Keys) -> State:
        """Clear the outputs of the rows ``keys``, or of the batch if none."""
        return page.clear(body.keys)

    @batch.post('/api/follow/stop')
    def stop_following(page: PageOfLabel) -> State:
        """Stop Follow if this batch follows. The page has no action to resume it."""
        return page.stop_following()

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

    # A POST, since the keys of many rows can exceed the length limit of a URL.
    @batch.post('/api/plot')
    def plot(page: PageOfLabel, body: Keys) -> Plot:
        """The plot of the results of the rows ``keys``, or of the first ones."""
        return page.plot(body.keys)

    @batch.get('/api/table.{suffix}', response_class=Response)
    def table_file(page: PageOfLabel, suffix: Literal['csv', 'xlsx']) -> Response:
        media = 'text/csv' if suffix == 'csv' else _XLSX
        return _download(page.table_file(f'.{suffix}'), f'table.{suffix}', media)

    @batch.post('/api/table')
    def load_table(
        page: PageOfLabel, name: str, content: Annotated[bytes, Body()] = b''
    ) -> State:
        """Replace the batch table with the rows of a table file."""
        return page.load_table(name, content)

    @batch.get('/api/settings.yaml', response_class=Response)
    def settings_file(page: PageOfLabel) -> Response:
        return _download(page.settings_file(), 'settings.yaml', 'application/yaml')

    @batch.post('/api/settings.yaml')
    def load_settings(
        page: PageOfLabel, name: str, content: Annotated[bytes, Body()] = b''
    ) -> State:
        return page.load_settings(name, content)

    @batch.post('/api/batch')
    def change(page: PageOfLabel, body: HeaderEdit) -> State:
        """Change the title and description of the batch."""
        return page.change(body)

    @batch.post('/api/output/form')
    def set_output_form(
        page: PageOfLabel, body: Annotated[dict[str, Any], Body()]
    ) -> State:
        """
        Set the output options of the batch.

        The body is a plain dict, so that :class:`~.page.Page` validates it and
        an invalid value returns 422 with a message, as for other actions.
        """
        return page.set_output_form(body)

    @batch.post('/api/publish')
    def publish(page: PageOfLabel, body: Keys) -> State:
        """Publish the results of the rows of ``keys`` to the catalogue."""
        return page.publish(body.keys)

    @batch.post('/api/batch/remove')
    def remove_batch(label: str, body: RemoveBatch | None = None) -> Notice:
        """Remove the batch. Its results stay in the workspace unless cleared."""
        return pages.remove(label, clear=body is not None and body.clear)

    @batch.post('/api/results.zip', response_class=Response)
    def results_file(page: PageOfLabel, body: Keys) -> Response:
        """The results of the done rows among ``keys``, or of all, as a zip."""
        content = page.results_file(body.keys)
        return _download(content, 'results.zip', 'application/zip')

    # The routes of the workspace, the same for every label.
    workspace = APIRouter()

    @workspace.get('/api/batches')
    def batches() -> Batches:
        return pages.batches()

    @workspace.get('/api/batches/counts')
    def counts() -> BatchCounts:
        """The number of rows of each batch by status."""
        return pages.counts()

    @workspace.post('/api/batches')
    def create(body: NewBatch) -> Made:
        return pages.create(body)

    @workspace.post('/api/batches/table')
    def create_from_table(
        spec: str,
        title: str,
        name: str,
        content: Annotated[bytes, Body()] = b'',
        description: str = '',
        settings_from: str | None = None,
    ) -> Made:
        """Make a batch without a source, of the rows of a table file."""
        new = NewBatch(
            spec=spec, title=title, description=description, settings_from=settings_from
        )
        return pages.create(new, (name, content))

    @workspace.post('/api/outputs/clear')
    def clear_all() -> Notice:
        """Clear the outputs of every record of the workspace."""
        return pages.clear()

    @workspace.get('/api/sources')
    def sources() -> list[SourceChoice]:
        """What a new batch can be made with."""
        return pages.sources()

    @workspace.get('/api/sources/rows')
    def rows_of(
        name: str,
        first: str | None = None,
        last: str | None = None,
        later: bool = False,
    ) -> SourceRows:
        """
        The rows of the row source ``name`` now, each marked for a new batch
        of the rows from ``first`` to ``last``, or of no row if ``later``.
        """
        return pages.rows_of(name, first, last, later)

    @workspace.get('/api/runs')
    def runs() -> Runs:
        return pages.runs.shown()

    @workspace.post('/api/runs/cells')
    def correct(body: CellEdit) -> Notice:
        """Correct a field of runs. No batch table changes."""
        return pages.runs.correct(body)

    @workspace.post('/api/runs/cells/revert')
    def revert_run(body: Cells) -> Notice:
        return pages.runs.revert(body)

    app.include_router(batch)
    app.include_router(workspace)
    app.include_router(workspace, prefix='/batches/{label}')
    app.mount('/batches/{label}/static', StaticFiles(directory=STATIC), name='static')
    app.mount('/static', StaticFiles(directory=STATIC), name='static-home')
    return app

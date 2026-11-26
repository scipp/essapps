# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workspaces of a root folder as a web page: :func:`create_launch_app`.

The app holds the workspaces it opened, each with the batch app of its pages
(:func:`.api.create_app`) mounted at ``/workspaces/<name>/``. It runs the
lifespan of each, since Starlette runs no lifespan of a mounted app, and the
lifespan owns the timer of Follow. Every route that opens or closes a
workspace runs in the event loop, under one ``asyncio.Lock``; the mounts are
changed only there.

How the launch page and the batch pages relate: ``README.md`` in this folder,
"The launch page".
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import AbstractContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route, Router

from ..definition import Header, check_label
from ..workspaces import (
    WORKSPACE_FILE,
    WorkspaceHeader,
    create_workspace,
    not_offered,
    read_workspace_header,
    remove_workspace,
    workspaces,
)
from .api import STATIC, answer_errors, create_app
from .errors import NotFound, reject_on_error
from .models import NewWorkspace, Notice, WorkspaceList, WorkspaceRow
from .pages import Pages

type OpenPages = Callable[[Path], AbstractContextManager[Pages]]
"""
Opens the workspace in a folder and returns the pages of its batches. The exit
closes the workspace. The application decides the bindings, the file sources
it allows, and the first batches.
"""

_METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE']


@dataclass(frozen=True)
class _Opened:
    title: str
    pages: Pages
    mount: Mount
    stack: AsyncExitStack


class Launcher:
    """
    The workspaces of ``root``, and the ones open in this server.

    Parameters
    ----------
    root:
        The folder of the workspaces, made with the first workspace.
    open_pages:
        Opens a workspace folder (:data:`OpenPages`).
    sources:
        The file sources that ``open_pages`` allows, as the ``files`` of a
        :class:`~ess.batch.WorkspaceHeader`, in the order the form offers them.
    title:
        The title of the application.
    """

    def __init__(
        self,
        root: Path,
        open_pages: OpenPages,
        *,
        sources: Collection[str],
        title: str = '',
    ) -> None:
        self.root = root
        self.sources = list(sources)
        self.title = title
        self._open_pages = open_pages
        self._lock = asyncio.Lock()
        self._opened: dict[str, _Opened] = {}
        self.mounts = Router(
            routes=[Route('/{name}/{path:path}', self._arrive, methods=_METHODS)]
        )
        """The batch app of each open workspace, under ``/<name>/``."""

    def listed(self) -> WorkspaceList:
        """
        The workspaces of the root, as the launch page lists them. Raises
        :class:`~.errors.Rejected` if a ``workspace.json`` cannot be read.
        """
        with reject_on_error('The workspaces cannot be listed'):
            found = workspaces(self.root)
        rows = [
            WorkspaceRow(
                name=name,
                title=header.header.title,
                description=header.header.description,
                proposal=header.proposal,
                files=header.files,
                open=name in self._opened,
                problem=not_offered(header.files, self.sources),
            )
            for name, header in found.items()
        ]
        return WorkspaceList(application=self.title, sources=self.sources, rows=rows)

    def create(self, new: NewWorkspace) -> Notice:
        """
        Make the workspace of ``new``. Raises :class:`~.errors.Rejected` if
        its file source is not offered, or as
        :func:`~ess.batch.create_workspace` raises.
        """
        with reject_on_error('The workspace was not made'):
            if (problem := not_offered(new.files, self.sources)) is not None:
                raise ValueError(problem)
            header = Header(new.title, new.description)
            create_workspace(
                self.root, new.name, WorkspaceHeader(header, new.proposal, new.files)
            )
        return Notice(notice=f'Made {header.title}.')

    async def open(self, name: str) -> Notice:
        """
        Open the workspace ``name``, unless it is open.

        Raises :class:`~.errors.NotFound` if there is none, and
        :class:`~.errors.Rejected` if it cannot be opened.
        """
        async with self._lock:
            opened = await self._open_locked(name)
        return Notice(notice=f'Opened {opened.title}.')

    async def _open_locked(self, name: str) -> _Opened:
        if (opened := self._opened.get(name)) is not None:
            return opened
        folder = self.root / name
        try:
            check_label(name)  # not '..'
        except ValueError:
            raise NotFound(f'There is no workspace {name}.') from None
        if not (folder / WORKSPACE_FILE).is_file():
            raise NotFound(f'There is no workspace {name}.')
        with reject_on_error(f'Cannot open {name}'):
            header = read_workspace_header(folder)
            if (problem := not_offered(header.files, self.sources)) is not None:
                raise ValueError(problem)
        stack = AsyncExitStack()
        title = header.header.title
        try:
            with reject_on_error(f'Cannot open {title}'):
                pages = await to_thread.run_sync(
                    stack.enter_context, self._open_pages(folder)
                )
            app = create_app(pages)
            await stack.enter_async_context(app.router.lifespan_context(app))
        except BaseException:
            await stack.aclose()
            raise
        opened = _Opened(title, pages, Mount(f'/{name}', app=app), stack)
        self.mounts.routes.insert(0, opened.mount)
        self._opened[name] = opened
        return opened

    async def _arrive(self, request: Request) -> Response:
        """
        Open the workspace of a request to its batch app, then send the browser
        to the same URL, which the app then serves. So a page of a workspace
        works again after a restart of the server.
        """
        await self.open(request.path_params['name'])
        return RedirectResponse(request.url, status_code=307)

    async def remove(self, name: str) -> Notice:
        """
        Delete the workspace ``name`` with its folder: its batches, history and
        stored outputs.

        Opens it if it is not open, so that a workspace that another process
        holds is not deleted. Stops Follow, and raises
        :class:`~.errors.Rejected` if a record is pending
        (:meth:`.pages.Pages.stop`). Otherwise it closes the workspace, then
        deletes the folder.
        """
        async with self._lock:
            opened = await self._open_locked(name)
            await to_thread.run_sync(opened.pages.stop)
            del self._opened[name]
            self.mounts.routes.remove(opened.mount)
            await opened.stack.aclose()
            with reject_on_error(f'The folder of {opened.title} was not deleted'):
                await to_thread.run_sync(remove_workspace, self.root, name)
        return Notice(notice=f'Removed {opened.title}.')

    async def close(self) -> None:
        """Close every open workspace, the last opened first."""
        async with self._lock:
            while self._opened:
                _, opened = self._opened.popitem()
                self.mounts.routes.remove(opened.mount)
                await opened.stack.aclose()


def create_launch_app(launcher: Launcher) -> FastAPI:
    """
    The app of the launch page of ``launcher``, at ``/``, and of the batch app
    of each open workspace, at ``/workspaces/<name>/``.

    Its lifespan closes the open workspaces at the end.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await launcher.close()

    app = FastAPI(title=launcher.title or 'Workspaces', lifespan=lifespan)
    answer_errors(app)

    @app.get('/', include_in_schema=False)
    def home() -> FileResponse:
        return FileResponse(STATIC / 'launch.html')

    @app.get('/api/workspaces')
    def listed() -> WorkspaceList:
        return launcher.listed()

    @app.post('/api/workspaces')
    def create(body: NewWorkspace) -> Notice:
        return launcher.create(body)

    @app.post('/api/workspaces/{name}/open')
    async def open_named(name: str) -> Notice:
        return await launcher.open(name)

    @app.post('/api/workspaces/{name}/remove')
    async def remove(name: str) -> Notice:
        """Delete the workspace with its folder."""
        return await launcher.remove(name)

    app.mount('/workspaces', launcher.mounts)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app

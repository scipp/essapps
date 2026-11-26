# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workspaces of a root folder as a web page: :func:`create_launch_app`.

The app holds the workspaces it opened, each with the batch app of its pages
(:func:`.api.create_app`) mounted at ``/workspaces/<name>/``. It runs the
lifespan of each, since Starlette runs no lifespan of a mounted app, and the
lifespan owns the timer of Follow. Every route that opens or closes a
workspace runs in the event loop, under one ``asyncio.Lock``; the mounts are
changed only there. What blocks, such as opening a workspace or waiting for
its records when it closes, runs in a thread.

How the launch page and the batch pages relate, and what opening, closing and
removing a workspace do: ``README.md`` in this folder, "The launch page".
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount, Route, Router

from ess.apps import Client
from ess.spec import Binding

from ..corrections import CorrectedFiles
from ..definition import FolderDefinitions, Header, check_label
from ..workspaces import (
    WORKSPACE_FILE,
    FileSources,
    WorkspaceHeader,
    create_workspace,
    not_allowed,
    open_workspace,
    read_workspace_header,
    remove_workspace,
    workspaces,
)
from .api import STATIC, answer_errors, create_app
from .errors import NotFound, Rejected, reject_on_error
from .models import NewWorkspace, Notice, WorkspaceList, WorkspaceRow
from .pages import Pages

type Build = Callable[
    [Client, CorrectedFiles, FolderDefinitions, WorkspaceHeader], Pages
]
"""
Makes the pages of the batches of a workspace that was opened: of its client,
its corrected file source, its definitions and its header. The application
decides the first batches and what the pages show.
"""

_METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE']
_HOME = '../../'
"""The launch page, relative to the root of the batch app of a workspace."""


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
    build:
        Makes the pages of a workspace once it is open (:data:`Build`).
    bind:
        The bindings of every workspace.
    allowed:
        The file sources that a workspace may have, in the order the form
        lists them (:func:`~ess.batch.file_sources`).
    title:
        The title of the application.
    """

    def __init__(
        self,
        root: Path,
        build: Build,
        *,
        bind: Sequence[Binding],
        allowed: FileSources,
        title: str = '',
    ) -> None:
        self.root = root
        self.title = title
        self._build = build
        self._bind = bind
        self._allowed = allowed
        self._lock = asyncio.Lock()
        self._opened: dict[str, _Opened] = {}
        # Closed on the launch page; a request of its batch app opens it no more.
        self._closed: set[str] = set()
        self.mounts = Router(
            routes=[Route('/{name}/{path:path}', self._arrive, methods=_METHODS)]
        )
        """The batch app of each open workspace, under ``/<name>/``."""

    def listed(self) -> WorkspaceList:
        """
        The workspaces of the root, as the launch page lists them. A
        workspace whose ``workspace.json`` cannot be read is listed with the
        reason.
        """
        with reject_on_error('The workspaces cannot be listed'):
            names = workspaces(self.root)
        return WorkspaceList(
            application=self.title,
            allowed=list(self._allowed),
            rows=[self._row(name) for name in names],
        )

    def _row(self, name: str) -> WorkspaceRow:
        is_open = name in self._opened
        try:
            header = read_workspace_header(self.root / name)
        except ValueError as error:
            return WorkspaceRow(
                name=name,
                title=name,
                description='',
                proposal=None,
                files=None,
                open=is_open,
                problem=str(error),
            )
        return WorkspaceRow(
            name=name,
            title=header.header.title,
            description=header.header.description,
            proposal=header.proposal,
            files=header.files,
            open=is_open,
            problem=not_allowed(header.files, self._allowed),
        )

    def create(self, new: NewWorkspace) -> Notice:
        """
        Make the workspace of ``new``. Raises :class:`~.errors.Rejected` if
        its file source is not allowed, or as
        :func:`~ess.batch.create_workspace` raises.
        """
        with reject_on_error('The workspace was not made'):
            if (problem := not_allowed(new.files, self._allowed)) is not None:
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
            self._closed.discard(name)
            opened = await self._open_locked(name)
        return Notice(notice=f'Opened {opened.title}.')

    def _folder(self, name: str) -> Path:
        """The folder of the workspace ``name``; :class:`NotFound` if none."""
        folder = self.root / name
        try:
            check_label(name)  # not '..'
        except ValueError:
            raise NotFound(f'There is no workspace {name}.') from None
        if not (folder / WORKSPACE_FILE).is_file():
            raise NotFound(f'There is no workspace {name}.')
        return folder

    async def _open_locked(self, name: str) -> _Opened:
        if (opened := self._opened.get(name)) is not None:
            return opened
        folder = self._folder(name)
        with reject_on_error(f'Cannot open {name}'):
            header = read_workspace_header(folder)
            if (problem := not_allowed(header.files, self._allowed)) is not None:
                raise ValueError(problem)
        title = header.header.title
        stack = AsyncExitStack()
        held = ExitStack()
        stack.push_async_callback(to_thread.run_sync, held.close)
        try:
            with reject_on_error(f'Cannot open {title}'):
                pages = await to_thread.run_sync(self._enter, folder, header, held)
            app = create_app(pages, home=_HOME)
            await stack.enter_async_context(app.router.lifespan_context(app))
        except BaseException:
            await stack.aclose()
            raise
        opened = _Opened(title, pages, Mount(f'/{name}', app=app), stack)
        self.mounts.routes.insert(0, opened.mount)
        self._opened[name] = opened
        return opened

    def _enter(self, folder: Path, header: WorkspaceHeader, held: ExitStack) -> Pages:
        """Open the workspace in ``folder``, held until ``held`` closes."""
        client, files, definitions = open_workspace(
            folder, header, self._bind, allowed=self._allowed
        )
        held.enter_context(client)
        return self._build(client, files, definitions, header)

    async def _arrive(self, request: Request) -> Response:
        """
        Open the workspace of a request to its batch app, then send the browser
        to the same URL, which the app then serves. So a page of a workspace
        works again after a restart of the server. A workspace closed on the
        launch page is not opened.
        """
        name = request.path_params['name']
        async with self._lock:
            if name in self._closed:
                raise Rejected(f'{name} is closed. Open it on the launch page.')
            await self._open_locked(name)
        return RedirectResponse(request.url, status_code=307)

    async def close(self, name: str) -> Notice:
        """
        Close the workspace ``name``, so that another process may open it.

        Raises :class:`~.errors.NotFound` if it is not open, and
        :class:`~.errors.Rejected`, with nothing changed, if a record of it is
        pending (:meth:`.pages.Pages.close`). Raises it too if closing raises;
        the workspace is closed then all the same.
        """
        async with self._lock:
            if name not in self._opened:
                raise NotFound(f'{name} is not open.')
            with reject_on_error(f'{name} did not close cleanly'):
                title = await self._close_locked(name)
        return Notice(notice=f'Closed {title}. Nothing is deleted; open it to go on.')

    async def _close_locked(self, name: str, *, shutdown: bool = False) -> str:
        """
        Close the open workspace ``name`` and return its title.

        Unless at ``shutdown``, the pages close first, which raises if a
        record is pending. At shutdown, closing waits for the pending records.
        Either way, a batch that follows follows again once the workspace is
        opened again.
        """
        opened = self._opened[name]
        if not shutdown:
            await to_thread.run_sync(opened.pages.close)
        del self._opened[name]
        self.mounts.routes.remove(opened.mount)
        self._closed.add(name)
        await opened.stack.aclose()
        return opened.title

    async def remove(self, name: str) -> Notice:
        """
        Delete the workspace ``name`` with its folder: its batches, history and
        stored outputs (:func:`~ess.batch.remove_workspace`).

        Closes it first if it is open, as :meth:`close` does. A workspace that
        is not open is not opened, so one that cannot be opened can be
        removed.

        Raises :class:`~.errors.NotFound` if there is none, and
        :class:`~.errors.Rejected` if its folder is a symbolic link, before
        anything is closed; and as :meth:`close` raises, and if another
        process holds the folder or it cannot be deleted.
        """
        async with self._lock:
            if self._folder(name).is_symlink():
                raise Rejected(
                    f'Cannot remove {name}: its folder is a link to another '
                    'folder. Delete the link by hand.'
                )
            if name in self._opened:
                with reject_on_error(f'{name} did not close cleanly'):
                    await self._close_locked(name)
            with reject_on_error(f'The folder of {name} was not deleted'):
                try:
                    await to_thread.run_sync(remove_workspace, self.root, name)
                except RuntimeError:  # the lock of the folder
                    raise Rejected(
                        f'Cannot remove {name}: another process has it open.'
                    ) from None
            self._closed.discard(name)
        return Notice(notice=f'Removed {name}.')

    async def shutdown(self) -> None:
        """
        Close every open workspace, the last opened first.

        Raises an ``ExceptionGroup`` of what each close raised, after all.
        """
        errors: list[Exception] = []
        async with self._lock:
            for name in reversed(list(self._opened)):
                try:
                    await self._close_locked(name, shutdown=True)
                except Exception as error:  # noqa: PERF203
                    errors.append(error)
        if errors:  # a builtin since Python 3.11, which ruff.toml does not target
            raise ExceptionGroup(  # noqa: F821
                'Some workspaces did not close cleanly', errors
            )


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
            await launcher.shutdown()

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

    @app.post('/api/workspaces/{name}/close')
    async def close(name: str) -> Notice:
        return await launcher.close(name)

    @app.post('/api/workspaces/{name}/remove')
    async def remove(name: str) -> Notice:
        """Delete the workspace with its folder."""
        return await launcher.remove(name)

    app.mount('/workspaces', launcher.mounts)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app

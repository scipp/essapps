# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workspace folders: what each is opened with (``workspace.json``), the
workspaces of a root folder, and :func:`open_workspace`.

Guide: ``packages/essbatch/docs/batch.md``, "Workspaces of a root folder".
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import to_json

from ess.apps import Client
from ess.apps.files import replace_at_once
from ess.apps.folder import ArrivingFiles, open_folder
from ess.apps.log import hold
from ess.apps.protocol import FileSource
from ess.spec import Binding

from .corrections import CorrectedFiles
from .definition import FolderDefinitions, Header, check_label

WORKSPACE_FILE = 'workspace.json'

type FileSources = Mapping[str, Callable[[Path, str], FileSource]]
"""
The file sources an application allows, by the ``files`` of a
:class:`WorkspaceHeader`: each a function of the workspace folder and the
proposal. :func:`file_sources` makes them.
"""


@dataclass(frozen=True)
class WorkspaceHeader:
    """
    What a workspace folder is opened with, stored in its ``workspace.json``.

    ``files`` names the file source: ``'local'`` for ``files.csv`` of the
    folder, ``'scicat:PROFILE'`` for the raw datasets of the proposal in the
    SciCat of the scitacean profile ``PROFILE``. ``ValueError`` for an empty
    proposal.
    """

    header: Header
    proposal: str = 'local'
    files: str = 'local'

    def __post_init__(self) -> None:
        if not self.proposal.strip():
            raise ValueError('give a proposal')


class _Stored(BaseModel, frozen=True):
    """``workspace.json``."""

    model_config = ConfigDict(extra='forbid')

    proposal: str
    files: str
    title: str
    description: str = ''


def read_workspace_header(folder: Path) -> WorkspaceHeader:
    """
    The header of the workspace in ``folder``.

    Raises ``ValueError`` naming the file if there is none or it cannot be
    read.
    """
    path = folder / WORKSPACE_FILE
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise ValueError(
            f'{folder} has no {WORKSPACE_FILE}: "essbatch -w {folder} files" writes it'
        ) from None
    except OSError as error:
        raise ValueError(f'{path}: {error.strerror}') from None
    try:
        stored = _Stored.model_validate_json(content)
        return WorkspaceHeader(
            Header(stored.title, stored.description), stored.proposal, stored.files
        )
    except (ValidationError, ValueError) as error:
        raise ValueError(f'{path}: {error}') from None


def write_workspace_header(folder: Path, header: WorkspaceHeader) -> None:
    """Replace ``workspace.json`` in ``folder`` at once."""
    stored = _Stored(
        proposal=header.proposal,
        files=header.files,
        title=header.header.title,
        description=header.header.description,
    )
    content = to_json(stored.model_dump(), indent=1)
    replace_at_once(folder / WORKSPACE_FILE, lambda path: path.write_bytes(content))


def workspaces(root: Path) -> list[str]:
    """
    The names of the workspaces of ``root``, sorted: of each folder with a
    ``workspace.json`` whose name is a label (:func:`~.definition.check_label`).
    Empty if ``root`` does not exist.
    """
    if not root.is_dir():
        return []
    return sorted(
        folder.name
        for folder in root.iterdir()
        if _is_label(folder.name) and (folder / WORKSPACE_FILE).is_file()
    )


def _is_label(name: str) -> bool:
    try:
        check_label(name)
    except ValueError:
        return False
    return True


def create_workspace(root: Path, name: str, header: WorkspaceHeader) -> Path:
    """
    Make the folder ``name`` in ``root`` with its ``workspace.json``, and
    return it.

    Raises ``ValueError`` if ``name`` is not a label
    (:func:`~.definition.check_label`) or the folder exists.
    """
    check_label(name)
    folder = root / name
    root.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir()
    except FileExistsError:
        raise ValueError(f'{name}: the folder exists') from None
    write_workspace_header(folder, header)
    return folder


def remove_workspace(root: Path, name: str) -> None:
    """
    Delete the folder of the workspace ``name`` of ``root``, with everything in
    it, unless a process holds it.

    With the folder held, as :func:`~ess.apps.folder.open_folder` holds it,
    the folder is renamed to ``.removing-<name>-<id>``, which is no label, and
    then deleted. So a deletion that fails halfway leaves a folder that
    :func:`workspaces` does not list, and the name free for a new workspace.

    Raises ``KeyError`` if ``root`` has no workspace ``name``, ``ValueError``
    if its folder is a symbolic link, ``RuntimeError`` if a process holds it,
    and ``OSError`` if it cannot be renamed or deleted.
    """
    check_label(name)
    folder = root / name
    if folder.is_symlink():
        raise ValueError(
            f'{name} is a link to {folder.readlink()}; delete the link itself'
        )
    if not (folder / WORKSPACE_FILE).is_file():
        raise KeyError(name)
    removing = root / f'.removing-{name}-{uuid.uuid4().hex}'
    with hold(folder / 'log.jsonl'):
        folder.rename(removing)
        shutil.rmtree(removing)


def file_sources(given: Iterable[str]) -> FileSources:
    """
    The file sources of ``given``, such as ``['local', 'scicat:staging.ess']``.

    A source of SciCat needs the extra ``scicat``. It logs in to its profile
    with :func:`ess.apps.scicat.login` at its first query, so once per profile
    and process. Raises ``ValueError`` for a name of no file source.
    """
    sources: dict[str, Callable[[Path, str], FileSource]] = {}
    for name in given:
        kind, colon, profile = name.partition(':')
        if name == 'local':
            sources[name] = lambda folder, _: ArrivingFiles(folder)
        elif kind == 'scicat' and colon and profile:
            sources[name] = _scicat(profile)
        else:
            raise ValueError(f"{name!r}: a file source is 'local' or 'scicat:PROFILE'")
    return sources


def not_allowed(files: str, allowed: Collection[str]) -> str | None:
    """
    Why an application that allows the file sources ``allowed`` cannot open a
    workspace of the file source ``files``; ``None`` if it can.
    """
    if files in allowed:
        return None
    return (
        f'its file source is {files}, which this application does not allow '
        f'(it allows {", ".join(allowed) or "none"})'
    )


def _scicat(profile: str) -> Callable[[Path, str], FileSource]:
    """The raw datasets of a proposal, read with the login to ``profile``."""
    from ess.apps.scicat import ScicatFiles, login  # needs the extra scicat

    return lambda _, proposal: ScicatFiles(lambda: login(profile), proposal)


def open_workspace(
    folder: Path,
    header: WorkspaceHeader,
    bind: Sequence[Binding],
    *,
    allowed: FileSources,
) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the workspace in ``folder``, its corrected file source, and
    the folder's definitions.

    The proposal and the file source are those of ``header``, the header of
    the folder as :func:`read_workspace_header` reads it. The corrections are
    stored in the folder. A local workspace lists the files of ``files.csv``,
    read again when it is replaced (:class:`~ess.apps.folder.ArrivingFiles`).

    Raises ``ValueError`` if ``allowed`` lacks the file source of the header,
    and as :func:`ess.apps.folder.open_folder` and
    :class:`~.corrections.CorrectedFiles` do.
    """
    if (problem := not_allowed(header.files, allowed)) is not None:
        raise ValueError(f'{folder.resolve().name}: {problem}')
    source = allowed[header.files]
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(source(folder, header.proposal), definitions=definitions)
    client = open_folder(folder, bind, files=files, proposal=header.proposal)
    return client, files, definitions

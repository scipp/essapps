# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workspace folders: what each is opened with (``workspace.json``), the
workspaces of a root folder, and :func:`open_workspace`.

Guide: ``packages/essbatch/docs/batch.md``, "Workspaces of a root folder".
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import to_json

from ess.apps import Client
from ess.apps.files import replace_at_once
from ess.apps.folder import ArrivingFiles, open_folder
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

    A folder without ``workspace.json`` is a local workspace of the proposal
    ``local``, titled by the folder's name. Raises ``ValueError`` naming the
    file if it cannot be read.
    """
    path = folder / WORKSPACE_FILE
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return WorkspaceHeader(Header(folder.resolve().name))
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


def workspaces(root: Path) -> dict[str, WorkspaceHeader]:
    """
    The header of each folder of ``root`` with a ``workspace.json``, by the
    folder's name, sorted by name. Empty if ``root`` does not exist.

    Raises ``ValueError`` if a ``workspace.json`` cannot be read.
    """
    if not root.is_dir():
        return {}
    return {
        folder.name: read_workspace_header(folder)
        for folder in sorted(root.iterdir())
        if (folder / WORKSPACE_FILE).is_file()
    }


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
    Delete the folder ``name`` in ``root``, with everything in it.

    Raises ``KeyError`` if it holds no ``workspace.json``. The caller makes
    sure that no process holds the workspace.
    """
    check_label(name)
    folder = root / name
    if not (folder / WORKSPACE_FILE).is_file():
        raise KeyError(name)
    shutil.rmtree(folder)


def file_sources(given: Iterable[str]) -> FileSources:
    """
    The file sources of ``given``, such as ``['local', 'scicat:staging.ess']``.

    Logs in to the SciCat of each profile, here, which needs the extra
    ``scicat``. Raises ``ValueError`` for a name of no file source, and what
    ``scitacean.Client.login`` raises.
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


def not_offered(files: str, offered: Collection[str]) -> str | None:
    """
    Why an application that offers the file sources ``offered`` cannot open a
    workspace of the file source ``files``; ``None`` if it can.
    """
    if files in offered:
        return None
    return (
        f'its files are of {files}, which this application does not offer '
        f'(it offers {", ".join(offered) or "none"})'
    )


def _scicat(profile: str) -> Callable[[Path, str], FileSource]:
    """The raw datasets of a proposal, with one login to ``profile``."""
    import scitacean

    from ess.apps.scicat import ScicatFiles  # needs the extra scicat

    client = scitacean.Client.login(profile)
    return lambda _, proposal: ScicatFiles(client, proposal)


def open_workspace(
    folder: Path, bind: Sequence[Binding], *, allowed: FileSources
) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the workspace in ``folder``, its corrected file source, and
    the folder's definitions.

    The proposal and the file source are those of its header
    (:func:`read_workspace_header`). The corrections are stored in the folder.
    A local workspace lists the files of ``files.csv``, read again when it is
    replaced (:class:`~ess.apps.folder.ArrivingFiles`).

    Raises ``ValueError`` if ``allowed`` lacks the file source of the header,
    and as :func:`read_workspace_header`, :func:`ess.apps.folder.open_folder`
    and :class:`~.corrections.CorrectedFiles` do.
    """
    header = read_workspace_header(folder)
    if (problem := not_offered(header.files, allowed)) is not None:
        raise ValueError(f'{folder.resolve().name}: {problem}')
    source = allowed[header.files]
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(source(folder, header.proposal), definitions=definitions)
    client = open_folder(folder, bind, files=files, proposal=header.proposal)
    return client, files, definitions

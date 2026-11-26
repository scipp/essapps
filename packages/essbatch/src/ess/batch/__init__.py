# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toolbox for applications that reduce many samples, built on :mod:`ess.apps`.

The page is in :mod:`ess.batch.web`, the command in :mod:`ess.batch.cli`.
Guide for application developers: ``packages/essbatch/docs/batch.md``.
"""

from .corrections import CorrectedFiles
from .definition import DefinitionStore, DefinitionWriteError, FolderDefinitions, Header
from .intake import Intake
from .manager import BatchManager
from .reduction import Batch
from .source import FileInfo, Range, RowFunction, RowSource, rows_of
from .state import (
    RowState,
    left_to_reduce,
    not_reduced,
    publishable,
    ready,
    to_reduce,
    with_changed_settings,
)
from .workspaces import (
    FileSources,
    WorkspaceHeader,
    create_workspace,
    file_sources,
    open_workspace,
    read_workspace_header,
    remove_workspace,
    workspaces,
    write_workspace_header,
)

__all__ = [
    'Batch',
    'BatchManager',
    'CorrectedFiles',
    'DefinitionStore',
    'DefinitionWriteError',
    'FileInfo',
    'FileSources',
    'FolderDefinitions',
    'Header',
    'Intake',
    'Range',
    'RowFunction',
    'RowSource',
    'RowState',
    'WorkspaceHeader',
    'create_workspace',
    'file_sources',
    'left_to_reduce',
    'not_reduced',
    'open_workspace',
    'publishable',
    'read_workspace_header',
    'ready',
    'remove_workspace',
    'rows_of',
    'to_reduce',
    'with_changed_settings',
    'workspaces',
    'write_workspace_header',
]

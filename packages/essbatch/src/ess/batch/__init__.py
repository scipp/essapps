# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toolbox for applications that reduce many samples, built on :mod:`ess.apps`.

The page is in :mod:`ess.batch.web`, the command in :mod:`ess.batch.cli`.
Guide for application developers: ``packages/essbatch/docs/batch.md``.
"""

from .corrections import CorrectedFiles, open_workspace
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

__all__ = [
    'Batch',
    'BatchManager',
    'CorrectedFiles',
    'DefinitionStore',
    'DefinitionWriteError',
    'FileInfo',
    'FolderDefinitions',
    'Header',
    'Intake',
    'Range',
    'RowFunction',
    'RowSource',
    'RowState',
    'left_to_reduce',
    'not_reduced',
    'open_workspace',
    'publishable',
    'ready',
    'rows_of',
    'to_reduce',
    'with_changed_settings',
]

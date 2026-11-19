# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toolbox for applications that reduce many samples, built on :mod:`ess.apps`.

This package depends on the rest of ``ess.apps``, which knows nothing of it, and
imports no widget library. The page is in :mod:`ess.apps.batch.web`. The guide
for application developers is ``packages/essapps/docs/batch.md``.
"""

from .corrections import CorrectedFiles
from .definition import DefinitionStore, FolderDefinitions, Header, NotKept
from .manager import BatchManager
from .reduction import Batch
from .source import FileInfo, RowSource, Source, file_infos, rows_of
from .state import (
    RowState,
    earlier_settings,
    left_to_reduce,
    not_reduced,
    ready,
    refused_again,
    status_texts,
)

__all__ = [
    'Batch',
    'BatchManager',
    'CorrectedFiles',
    'DefinitionStore',
    'FileInfo',
    'FolderDefinitions',
    'Header',
    'NotKept',
    'RowSource',
    'RowState',
    'Source',
    'earlier_settings',
    'file_infos',
    'left_to_reduce',
    'not_reduced',
    'ready',
    'refused_again',
    'rows_of',
    'status_texts',
]

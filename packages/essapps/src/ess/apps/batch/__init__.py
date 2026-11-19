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
from .manager import BatchManager, Counts, Defaults, NotEnded, fits, scope_of
from .reduction import Batch, FileInfo, RowSource, Source, Words, file_infos
from .state import (
    EARLIER,
    RowState,
    arrived,
    compared,
    earlier_settings,
    left_to_reduce,
    ready,
    refused_again,
    status_texts,
)
from .table import EMPTY, Index, Row, RowTable, Scope

__all__ = [
    'EARLIER',
    'EMPTY',
    'Batch',
    'BatchManager',
    'CorrectedFiles',
    'Counts',
    'Defaults',
    'DefinitionStore',
    'FileInfo',
    'FolderDefinitions',
    'Header',
    'Index',
    'NotEnded',
    'NotKept',
    'Row',
    'RowSource',
    'RowState',
    'RowTable',
    'Scope',
    'Source',
    'Words',
    'arrived',
    'compared',
    'earlier_settings',
    'file_infos',
    'fits',
    'left_to_reduce',
    'ready',
    'refused_again',
    'scope_of',
    'status_texts',
]

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
from .manager import BatchManager, Counts, Defaults, Source, Words, bound_spec
from .reduction import Batch, FileInfo, RowSource, file_infos
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
from .table import EMPTY, Row, RowTable

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
    'NotKept',
    'Row',
    'RowSource',
    'RowState',
    'RowTable',
    'Source',
    'Words',
    'arrived',
    'bound_spec',
    'compared',
    'earlier_settings',
    'file_infos',
    'left_to_reduce',
    'ready',
    'refused_again',
    'status_texts',
]

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toolbox for applications that reduce many samples, built on :mod:`ess.apps`.

This package depends on the rest of ``ess.apps``, which knows nothing of it, and
imports no widget library; widgets are in :mod:`ess.apps.batch.panel`. The guide
for application developers is ``packages/essapps/docs/batch.md``.
"""

from .corrections import CorrectedFiles
from .reduction import Batch, FileInfo, RowSource, file_infos
from .state import RowState, ready, remaining, summary
from .table import EMPTY, Row, RowTable

__all__ = [
    'EMPTY',
    'Batch',
    'CorrectedFiles',
    'FileInfo',
    'Row',
    'RowSource',
    'RowState',
    'RowTable',
    'file_infos',
    'ready',
    'remaining',
    'summary',
]

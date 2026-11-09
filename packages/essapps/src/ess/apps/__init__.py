# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Framework for ESS data-reduction applications."""

from .backend import Backend, local
from .client import Accumulator, Client, Files, Provenance, Stage
from .errors import ClientEnded, FieldError, NotReadable, RecordFailed, SubmitError
from .files import FolderFiles, Selector, file
from .listing import Listing
from .protocol import BackendProtocol, ClientId, FileSource, Store
from .records import (
    ArrayInfo,
    Changes,
    Record,
    RecordStatus,
    Request,
    SpecId,
    Status,
    Template,
)
from .rules import Rule, RuleStatus, TriggerLoop

__all__ = [
    'Accumulator',
    'ArrayInfo',
    'Backend',
    'BackendProtocol',
    'Changes',
    'Client',
    'ClientEnded',
    'ClientId',
    'FieldError',
    'FileSource',
    'Files',
    'FolderFiles',
    'Listing',
    'NotReadable',
    'Provenance',
    'Record',
    'RecordFailed',
    'RecordStatus',
    'Request',
    'Rule',
    'RuleStatus',
    'Selector',
    'SpecId',
    'Stage',
    'Status',
    'Store',
    'SubmitError',
    'Template',
    'TriggerLoop',
    'file',
    'local',
]

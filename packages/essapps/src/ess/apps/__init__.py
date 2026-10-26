# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Framework for ESS data-reduction applications."""

from .backend import Backend
from .batch import LastBefore, Lookup, apply
from .client import Accumulator, Client, Datasets, Provenance, Stage, local
from .datasets import DatasetSource, Selector, dataset
from .errors import ClientEnded, FieldError, NotReadable, RecordFailed, SubmitError
from .records import Record, Request, SpecId, Status, Template
from .rules import Rule, RuleStatus, TriggerLoop
from .store import Store

__all__ = [
    'Accumulator',
    'Backend',
    'Client',
    'ClientEnded',
    'DatasetSource',
    'Datasets',
    'FieldError',
    'LastBefore',
    'Lookup',
    'NotReadable',
    'Provenance',
    'Record',
    'RecordFailed',
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
    'apply',
    'dataset',
    'local',
]

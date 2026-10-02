# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Framework for ESS data-reduction applications."""

from .accumulators import combine
from .backend import Backend, ClientEnded
from .batch import LastBefore, Lookup, apply
from .bindings import Binding
from .client import Accumulator, Client, Datasets, Provenance, Stage, local
from .datasets import DatasetSource, Selector, dataset
from .records import (
    Record,
    Request,
    Snapshot,
    SpecId,
    Status,
    SubmitError,
    Template,
)
from .rules import Rule, RuleStatus, TriggerLoop

__all__ = [
    'Accumulator',
    'Backend',
    'Binding',
    'Client',
    'ClientEnded',
    'DatasetSource',
    'Datasets',
    'LastBefore',
    'Lookup',
    'Provenance',
    'Record',
    'Request',
    'Rule',
    'RuleStatus',
    'Selector',
    'Snapshot',
    'SpecId',
    'Stage',
    'Status',
    'SubmitError',
    'Template',
    'TriggerLoop',
    'apply',
    'combine',
    'dataset',
    'local',
]

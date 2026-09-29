# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Framework for ESS data-reduction applications."""

from .accumulators import SUM, AccumulatorSpec, combine
from .backend import Backend
from .batch import LastBefore, Lookup, apply
from .bindings import Binding
from .client import Client, Provenance, local
from .datasets import DatasetSource, Selector, dataset
from .records import Failure, Record, Request, SpecId, Status, SubmitError, Template
from .rules import Rule, RuleStatus, TriggerLoop
from .sessions import Accumulator, Session, Stage

__all__ = [
    'SUM',
    'Accumulator',
    'AccumulatorSpec',
    'Backend',
    'Binding',
    'Client',
    'DatasetSource',
    'Failure',
    'LastBefore',
    'Lookup',
    'Provenance',
    'Record',
    'Request',
    'Rule',
    'RuleStatus',
    'Selector',
    'Session',
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

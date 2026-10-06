# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the protocol of the
code behind a spec (bindings), with ``combine``.

See :mod:`ess.spec._workflow_spec` for the design, :mod:`~.data` for
data fields, :mod:`~.binding` for the protocol of the code behind a spec, and
ADR 0001 (docs/developer/adr in the package) for the rationale.

The package does not import :mod:`~.conversions` (needs scipp) and
:mod:`~.pipeline` (``PipelineBinding``, needs sciline). Import them directly.
"""

from ._workflow_spec import NoParams, SerializedWorkflowSpec, WorkflowSpec
from .binding import Binding, Function, HeldState, HeldStateBinding
from .combine import combine
from .data import (
    AccumulatorRef,
    Array,
    ArraySpec,
    DataField,
    DatasetRef,
    Format,
    NexusFile,
    OpaqueFile,
    OutputRef,
    Ref,
    as_ref,
    data_fields,
    ref_fields,
    table_fields,
    walk_refs,
)
from .parameters import Quantity

__all__ = [
    'AccumulatorRef',
    'Array',
    'ArraySpec',
    'Binding',
    'DataField',
    'DatasetRef',
    'Format',
    'Function',
    'HeldState',
    'HeldStateBinding',
    'NexusFile',
    'NoParams',
    'OpaqueFile',
    'OutputRef',
    'Quantity',
    'Ref',
    'SerializedWorkflowSpec',
    'WorkflowSpec',
    'as_ref',
    'combine',
    'data_fields',
    'ref_fields',
    'table_fields',
    'walk_refs',
]

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the bindings that
the backend calls to compute a spec's requests.

See :mod:`ess.spec._workflow_spec` for the design, :mod:`~.data` for
data fields, :mod:`~.binding` for bindings and their protocols, and
ADR 0001 (docs/developer/adr in the package) for the rationale.

The package does not import :mod:`~.conversions` (needs scipp),
:mod:`~.pipeline` (``PipelineBinding``, needs sciline), and :mod:`~.testing`
(checks of the symmetries a binding promises). Import them directly.
"""

from ._workflow_spec import NoParams, SerializedWorkflowSpec, WorkflowSpec
from .binding import (
    AccumulatorBinding,
    Binding,
    Driver,
    DriverBinding,
    StageBinding,
    StageCall,
    function_binding,
)
from .data import (
    REPEATED_ROWS,
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
    distinct_row_tables,
    group_fields,
    ref_fields,
    table_fields,
    walk_refs,
)
from .parameters import Quantity

__all__ = [
    'REPEATED_ROWS',
    'AccumulatorBinding',
    'AccumulatorRef',
    'Array',
    'ArraySpec',
    'Binding',
    'DataField',
    'DatasetRef',
    'Driver',
    'DriverBinding',
    'Format',
    'NexusFile',
    'NoParams',
    'OpaqueFile',
    'OutputRef',
    'Quantity',
    'Ref',
    'SerializedWorkflowSpec',
    'StageBinding',
    'StageCall',
    'WorkflowSpec',
    'as_ref',
    'data_fields',
    'distinct_row_tables',
    'function_binding',
    'group_fields',
    'ref_fields',
    'table_fields',
    'walk_refs',
]

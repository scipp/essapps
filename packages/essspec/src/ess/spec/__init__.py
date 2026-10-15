# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the bindings that
the backend calls to compute a spec's requests.

See :mod:`ess.spec._workflow_spec` for the design, :mod:`~.refs` for the
references a request gives its inputs by, :mod:`~.field_types` for what the
framework reads from field types, :mod:`~.binding` for bindings and their
protocols, and ADR 0001 (docs/developer/adr in the package) for the
rationale.

The package does not import :mod:`~.conversions` (needs scipp),
:mod:`~.pipeline` (``PipelineBinding``, needs sciline), and :mod:`~.testing`
(checks of the symmetries a binding promises). Import them directly.
"""

from ._workflow_spec import NoInputs, NoParams, SerializedWorkflowSpec, WorkflowSpec
from .binding import (
    AccumulatorBinding,
    Binding,
    Driver,
    DriverBinding,
    StageBinding,
    StageCall,
    function_binding,
)
from .field_types import fits, is_file
from .parameters import Quantity
from .refs import AccumulatorRef, DatasetRef, OutputRef, Ref, as_ref, walk_refs

__all__ = [
    'AccumulatorBinding',
    'AccumulatorRef',
    'Binding',
    'DatasetRef',
    'Driver',
    'DriverBinding',
    'NoInputs',
    'NoParams',
    'OutputRef',
    'Quantity',
    'Ref',
    'SerializedWorkflowSpec',
    'StageBinding',
    'StageCall',
    'WorkflowSpec',
    'as_ref',
    'fits',
    'function_binding',
    'is_file',
    'walk_refs',
]

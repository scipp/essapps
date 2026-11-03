# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the bindings that
the backend calls to compute a spec's requests.

The package exports the specs, the references to outputs, the file models,
the bindings and their protocols, and :class:`Quantity`. See
:mod:`ess.spec._workflow_spec` for the rules of a spec's models and ADR 0001
(docs/developer/adr in the package) for the rationale. The helpers are in
their modules: :mod:`~.field_types` for what the framework reads from field
types, such as :func:`~.field_types.fits`, and how it validates a value by
its field, :mod:`~.refs` for finding the references of a request,
:mod:`~.places` for what a request may write at each parameter, read from
its type, and how the call phase makes a value, :mod:`~.request_types` for
the request type, which validates a request and is the params schema, and
:mod:`~.binding` for the values a binding is made with.

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
from .parameters import FileModel, Quantity
from .refs import AccumulatorRef, FileRef, OutputRef, Ref

__all__ = [
    'AccumulatorBinding',
    'AccumulatorRef',
    'Binding',
    'Driver',
    'DriverBinding',
    'FileModel',
    'FileRef',
    'NoParams',
    'OutputRef',
    'Quantity',
    'Ref',
    'SerializedWorkflowSpec',
    'StageBinding',
    'StageCall',
    'WorkflowSpec',
    'function_binding',
]

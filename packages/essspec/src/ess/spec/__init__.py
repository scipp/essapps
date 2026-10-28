# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the bindings that
the backend calls to compute a spec's requests.

The package exports the specs, the references a request gives data by,
the dataset models that take references to datasets, the bindings and their
protocols, and :class:`Quantity`. See
:mod:`ess.spec._workflow_spec` for the design and ADR 0001
(docs/developer/adr in the package) for the rationale. The helpers are in
their modules: :mod:`~.field_types` for what the framework reads from field
types, such as :func:`~.field_types.fits`, and how it validates a value by
its field, :mod:`~.refs` for finding the
references of a request, :mod:`~.places` for what a request writes at each
root place by its type, and :mod:`~.binding` for the values a binding is made
with.

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
from .parameters import DatasetModel, Quantity
from .refs import AccumulatorRef, DatasetRef, OutputRef, Ref

__all__ = [
    'AccumulatorBinding',
    'AccumulatorRef',
    'Binding',
    'DatasetModel',
    'DatasetRef',
    'Driver',
    'DriverBinding',
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

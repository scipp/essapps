# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of workflows, which UIs and backends read, and the bindings that
the backend calls to compute a spec's requests.

The package exports the specs, the references to outputs, the markers
:class:`File` and :data:`Referenced`, and the bindings and their protocols.
See :mod:`ess.spec._workflow_spec` for the rules of a spec's models and ADR
0001 (docs/developer/adr in the package) for the
rationale. The helpers are in their modules: :mod:`~.field_types` for what
the framework reads from field types, such as :func:`~.field_types.fits`,
:mod:`~.refs` for finding the references of a request,
:mod:`~.request_types` for the request type, which validates a request and
is the params schema, and how the call phase makes a value, and
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
from .parameters import (
    BackgroundRunFile,
    EmptyBeamRunFile,
    File,
    MaskFile,
    Referenced,
    RunFile,
    SampleRunFile,
    TransmissionRunFile,
    VanadiumRunFile,
)
from .refs import FileRef, OutputRef, Ref

__all__ = [
    'AccumulatorBinding',
    'BackgroundRunFile',
    'Binding',
    'Driver',
    'DriverBinding',
    'EmptyBeamRunFile',
    'File',
    'FileRef',
    'MaskFile',
    'NoParams',
    'OutputRef',
    'Ref',
    'Referenced',
    'RunFile',
    'SampleRunFile',
    'SerializedWorkflowSpec',
    'StageBinding',
    'StageCall',
    'TransmissionRunFile',
    'VanadiumRunFile',
    'WorkflowSpec',
    'function_binding',
]

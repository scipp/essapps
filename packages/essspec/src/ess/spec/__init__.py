# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Specs of workflows, and the bindings that compute a spec's requests.

``packages/essspec/docs/params.md`` is the guide to writing a spec, and
``packages/essspec/docs/bindings.md`` to writing a binding.

The package does not import :mod:`~.conversions` (needs scipp),
:mod:`~.pipeline` (needs sciline) and :mod:`~.testing`. Import them directly.
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Selections: the part of an output that a read with ``select=`` copies (see
:meth:`ess.apps.protocol.WorkspaceProtocol.outputs`). Neither function needs
the workspace's lock or state.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from ess.spec import WorkflowSpec, field_types

from .protocol import Selection
from .records import SpecId


def check_select(
    spec: WorkflowSpec[Any, Any] | None, names: Sequence[str] | None
) -> None:
    """
    Refuse a selection unless it names outputs, each declared by ``spec`` as
    an array or as a group with an array field. A selection names the
    dimensions of the outputs it reads, so it reads only outputs named.
    ``spec`` is ``None`` for a record of an unbound spec, whose outputs read as
    stored are checked by slicing.
    """
    if names is None:
        raise ValueError('select= needs the name of an output')
    if spec is None:
        return
    declared = spec.outputs.model_fields
    for name in (n for n in names if n in declared):  # others raise when read
        if not field_types.has_array(declared[name].annotation):
            raise ValueError(
                f'{SpecId.of(spec)} output {name!r}: select= needs an array, or a '
                'group with an array field'
            )


def selected(value: Any, select: Selection) -> Any:
    """
    A copy of the part of an array, or of a group, that ``select`` names.

    An array is a value with named dimensions that slices as
    ``value[dim, index]``, such as a scipp variable or data array. A
    group is a pydantic model, or a mapping, such as the ``sc.DataGroup`` of an
    output read as stored. Of a group, each dimension slices the array fields
    that have it, and every other field is copied whole. Unlike a
    ``sc.DataGroup``, a group refuses a dimension that no array field has, with
    ``ValueError``.
    """
    if not isinstance(value, BaseModel | Mapping):
        for dim, index in select.items():
            value = value[dim, index]
        return value.copy()
    fields = dict(value)
    for dim, index in select.items():
        sliced = {
            n: v[dim, index] for n, v in fields.items() if dim in getattr(v, 'dims', ())
        }
        if not sliced:
            raise ValueError(
                f'{type(value).__name__} has no array field with dimension {dim!r}'
            )
        fields |= sliced
    # A deep copy of a slice copies the slice only.
    if isinstance(value, BaseModel):
        return value.model_copy(update=copy.deepcopy(fields))
    return type(value)(copy.deepcopy(fields))

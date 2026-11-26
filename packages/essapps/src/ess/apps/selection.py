# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The part of an output that a read with ``select=`` copies.

See :meth:`ess.apps.protocol.WorkspaceProtocol.outputs`. Neither function needs
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
    Raises ``ValueError`` if a selection may not read the outputs ``names``.

    A selection slices by dimension, so it needs the names of the outputs. Each
    output that ``spec`` declares must be an array or a group with an array
    field. ``spec`` is ``None`` for a record of an unbound spec. Its outputs are
    checked when sliced.
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
    A copy of the part of an array or a group that ``select`` picks.

    An array slices as ``value[dim, index]``, such as a scipp variable or data
    array. A group is a pydantic model or a mapping, such as the ``sc.DataGroup``
    of an output read as stored. Each dimension slices the array fields of a
    group that have it, and other fields are copied whole. Unlike
    ``sc.DataGroup``, a group raises ``ValueError`` for a dimension that no array
    field has.
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

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Decisions about plots that need no widget library.

Which dimensions of an array get a slider, and which values a plot configured
for a spec reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ess.spec import OutputRef

from .client import Client
from .errors import NotReadable
from .records import Record


@dataclass(frozen=True)
class Plot:
    """
    A plot configured for the records of a spec.

    ``function`` accepts the record's outputs, as an instance of ``spec.outputs``,
    and returns a figure. Each param in ``inputs`` refers to a whole output.
    ``function`` receives the value of that output as a keyword argument named as
    the param. For example, a plot draws a mask on the detector image that the
    mask was computed from::

        Plot(masks_on_image, inputs=('detector',))

    """

    function: Callable[..., Any]
    inputs: tuple[str, ...] = ()


def read_inputs(plot: Plot, client: Client, record: Record) -> dict[str, Any]:
    """
    The value of each input of ``plot`` that the request of ``record`` refers to.

    Raises
    ------
    ValueError
        If a param holds anything except a reference to a whole output.
    NotReadable
        If the output is no longer readable.
    """
    values = {}
    for name in plot.inputs:
        ref = record.request.params[name]
        if not isinstance(ref, OutputRef) or ref.key is not None:
            raise ValueError(f'{name} does not reference a whole output')
        (source,) = client.records(ids=[ref.record])
        try:
            values[name] = client.output(source, ref.output)
        except NotReadable as error:
            raise NotReadable(
                f'{name} is no longer available. Compute it again, and this '
                f'record from the result ({error})'
            ) from error
    return values


def slider_dims(sizes: Mapping[str, int]) -> list[str]:
    """The dimensions of an array that get a slider: all but the last two."""
    return list(sizes)[:-2]

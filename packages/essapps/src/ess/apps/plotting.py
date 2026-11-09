# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What an application decides about plots before it draws any, which needs no
widget library: which dimensions of an array get a slider, and which values a plot
configured for a spec reads.

An application learns what an output is from :meth:`~ess.apps.Client.describe`,
which reads no array, and reads the part of an array that it draws with
``select=`` (see :meth:`~ess.apps.Client.output`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Plot:
    """
    A plot configured for the records of a spec.

    ``function`` takes the record's outputs, as an instance of ``spec.outputs``, and
    returns a figure. ``inputs`` names params that hold a reference to a whole output,
    each passed to ``function`` by that name as the value it references. So a workflow
    with a small output, such as a mask, draws it on the large value that it was
    computed from, such as a detector, which its record names::

        Plot(masks_on_image, inputs=('detector',))

    An input that is no longer readable fails the plot.
    """

    function: Callable[..., Any]
    inputs: tuple[str, ...] = ()


def slider_dims(sizes: Mapping[str, int]) -> list[str]:
    """
    The dimensions of an array that get a slider: all but the last two, which
    are drawn. An array of one or two dimensions has none.
    """
    return list(sizes)[:-2]

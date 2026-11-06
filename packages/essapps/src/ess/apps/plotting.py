# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What an application decides about plots before it draws any, which needs no
widget library: which dimensions of an array get a slider.

An application learns what an output is from :meth:`~ess.apps.Client.describe`,
which reads no array, and reads the part of an array that it draws with
``select=`` (see :meth:`~ess.apps.Client.output`).
"""

from __future__ import annotations

from collections.abc import Mapping


def slider_dims(sizes: Mapping[str, int]) -> list[str]:
    """
    The dimensions of an array that get a slider: all but the last two, which
    are drawn. An array of one or two dimensions has none.
    """
    return list(sizes)[:-2]

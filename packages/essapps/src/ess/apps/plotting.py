# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What an application decides about plots before it draws any, which needs no
widget library: which dimensions of an array get a slider, which arrays of a
listing may be drawn on one plot, and the plot.

An application learns what an output is from :meth:`~ess.apps.Client.describe`,
which reads no array, and reads the part of an array that it draws with
``select=`` (see :meth:`~ess.apps.Client.output`). Drawing needs plopp, which
is imported when the first figure is made: ``pip install 'essapps[panel]'``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .client import Client
from .errors import NotReadable
from .listing import Listing
from .records import ArrayInfo, Record, Status


def slider_dims(sizes: Mapping[str, int]) -> list[str]:
    """
    The dimensions of an array that get a slider: all but the last two, which
    are drawn. An array of one or two dimensions has none.
    """
    return list(sizes)[:-2]


type LineKey = tuple[str, str, str | None]
"""The ID of a record, the name of its output, and the field of the output."""


@dataclass(frozen=True)
class Line:
    """
    A 1-D array that may be drawn on a plot: ``field`` of the output
    ``output`` of ``record``, or the output itself if it is an array
    (``field`` is ``None``), along dimension ``dim``.
    """

    label: str
    record: Record
    output: str
    field: str | None
    dim: str

    @property
    def key(self) -> LineKey:
        """What identifies the array, which a label does not: it can change."""
        return (self.record.id, self.output, self.field)


class Lines:
    """
    The 1-D arrays of the outputs of a client's records that may be drawn
    together. What an output is comes from the client once per output, since a
    record never changes.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._described: dict[tuple[str, str], dict[str, Any]] = {}

    def of(self, listing: Listing) -> dict[LineKey, Line]:
        """
        The 1-D array fields of the outputs of completed records that the
        client may read, newest first, by key. An output that cannot be
        read now is left out.
        """
        lines = {}
        for record in reversed(listing.records):
            if listing.status[record.id] is not Status.COMPLETED:
                continue
            for name in listing.readable[record.id]:
                label = listing.output_label(record.ref(name))
                group = listing.is_group(record, name)
                for field, info in self._fields(record, name).items():
                    if isinstance(info, ArrayInfo) and len(info.sizes) == 1:
                        (dim,) = info.sizes
                        part = field if group else None
                        text = label if part is None else f'{label} {field}'
                        line = Line(text, record, name, part, dim)
                        lines[line.key] = line
        return lines

    def _fields(self, record: Record, name: str) -> dict[str, Any]:
        key = (record.id, name)
        if key not in self._described:
            try:
                self._described[key] = self._client.describe(record, name)
            except NotReadable:
                return {}
        return self._described[key]

    def read(self, line: Line) -> Any:
        """The array of ``line``."""
        value = self._client.output(line.record, line.output)
        return value if line.field is None else getattr(value, line.field)


def same_dim[K](lines: Mapping[K, Line], chosen: Sequence[K]) -> dict[K, Line]:
    """
    The ``lines`` that may be drawn with those ``chosen``: all of them if none
    is chosen, else those along the dimension of the first chosen.
    """
    chosen = [c for c in chosen if c in lines]
    if not chosen:
        return dict(lines)
    dim = lines[chosen[0]].dim
    return {key: line for key, line in lines.items() if line.dim == dim}


def overlay(arrays: Mapping[str, Any], log: bool = False) -> Any:
    """
    A plopp figure of 1-D arrays along one dimension, a line each, named by
    their keys; the y axis is logarithmic if ``log``.
    """
    try:
        import plopp as pp
    except ImportError as error:
        raise ImportError(
            "ess.apps.plotting needs plopp to draw: pip install 'essapps[panel]'"
        ) from error
    return pp.plot(dict(arrays), norm='log' if log else 'linear')

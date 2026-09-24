# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a session holds between runs: stages.

A run request and the parameters it was submitted as varying name the stage a
session holds for it: the spec, the values of the parameters not varied, the
names of those varied, and the outputs. The session holds the stage it built for
that name, so a later request naming the same stage computes only what lies
downstream of the stage's inputs. A stage over a list of runs
holds the accumulation of the runs it has seen, which is how adding a run to a
sum reduces only the new run. A file that changed on disk empties the store
(:class:`ess.apps.runner.Runner`), so no stage serves it from its earlier bytes.

A held stage is a cache, so dropping one is always safe: the store is bounded
and the least recently used goes first.

See docs/developer/stages.md.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

from .binding import StageCall

T = TypeVar('T')


class _Held(Generic[T]):
    """A bounded store, least recently used first."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[Hashable, T] = {}

    def clear(self) -> None:
        self._items.clear()

    def get(self, key: Hashable) -> T | None:
        item = self._items.pop(key, None)
        if item is not None:
            self._items[key] = item
        return item

    def put(self, key: Hashable, item: T) -> None:
        self._items.pop(key, None)
        self._items[key] = item
        while len(self._items) > self._limit:
            del self._items[next(iter(self._items))]


class Stages:
    """The stages of one session."""

    def __init__(self, *, limit: int = 4) -> None:
        self._stages: _Held[StageCall] = _Held(limit)

    def clear(self) -> None:
        """Drop everything held, as when workflow code changed."""
        self._stages.clear()

    def stage(
        self, name: Hashable, build: Callable[[], StageCall]
    ) -> tuple[StageCall, bool]:
        """The stage held under ``name``, built if there is none; and whether held."""
        held = self._stages.get(name)
        if held is not None:
            return held, True
        call = build()
        self._stages.put(name, call)
        return call, False

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a session holds between runs: stages and accumulators.

A run request names the stage it cuts: the workflow ID of the request (a hash
of spec, the parameters not varied, instrument, and proposal), the names of the
parameters it varies, the names of the intermediates it supplies, and its
outputs. The session holds the stage it built for that name, so a later request
naming the same stage computes only what lies downstream of the stage's inputs.
The checksums of the datasets the parameters not varied name are part of the
name, so a file that changed on disk does not find the stage built from its
earlier bytes.

An accumulator is held by the workflow ID, the input it fills, and the
outputs pushed into it so far. A request whose list of outputs to accumulate
begins with those pushes only the rest. Any other list, such as one with a
corrected member, starts a fresh accumulator; nothing is ever taken out.

Both are caches, so dropping one is always safe: each store is bounded and the
least recently used goes first.

See docs/developer/stages.md.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import Any, Generic, TypeVar

from .binding import Accumulator, StageCall
from .spec import OutputRef

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


class _Accumulated:
    def __init__(self, accumulator: Accumulator) -> None:
        self.accumulator = accumulator
        self.pushed: list[OutputRef] = []


class Stages:
    """The stages and accumulators of one session."""

    def __init__(self, *, limit: int = 4) -> None:
        self._stages: _Held[StageCall] = _Held(limit)
        self._accumulated: _Held[_Accumulated] = _Held(limit)

    def clear(self) -> None:
        """Drop everything held, as when workflow code changed."""
        self._stages.clear()
        self._accumulated.clear()

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

    def accumulate(
        self,
        name: Hashable,
        refs: list[OutputRef],
        make: Callable[[], Accumulator],
        load: Callable[[OutputRef], Any],
    ) -> tuple[Any, bool]:
        """
        The accumulation of ``refs``, and whether a held accumulator served it.

        A held accumulator serves if what it has pushed is where ``refs``
        begins; then only the rest is loaded and pushed.
        """
        held = self._accumulated.get(name)
        reused = held is not None and refs[: len(held.pushed)] == held.pushed
        if held is None or not reused:
            held = _Accumulated(make())
        for ref in refs[len(held.pushed) :]:
            held.accumulator.push(load(ref))
            held.pushed.append(ref)
        self._accumulated.put(name, held)
        return held.accumulator.value, reused

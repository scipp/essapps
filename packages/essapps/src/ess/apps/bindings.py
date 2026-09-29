# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: the code behind a spec.

A binding is staged with the values that stay fixed and returns a callable
over the rest, the blanks. It may compute what depends only on the fixed
values when staged or on the first call, and hold it; a request outside a
stage is staged with no blanks and called once::

    binding.stage(values, ())()                      # a plain request
    call = binding.stage(fixed, ('bins',))           # a stage in a session
    call(bins=100)

A plain function is a binding that computes nothing ahead. How a binding
computes is invisible in the records: a call through a stage returns what the
plain request returns.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

Function = Callable[..., Mapping[str, Any]]
"""Takes parameter values by field name, with data read, and returns the outputs."""


@runtime_checkable
class Binding(Protocol):
    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        """
        A callable over ``blanks`` that computes the outputs with ``fixed``.

        The callable may be called from several threads at once.
        """
        ...


@dataclass(frozen=True)
class FunctionBinding:
    """A plain function as a binding: every call computes everything."""

    function: Function

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self.function, **fixed)


def as_binding(code: Binding | Function) -> Binding:
    """``code`` as a binding; a plain function is wrapped."""
    return code if isinstance(code, Binding) else FunctionBinding(code)

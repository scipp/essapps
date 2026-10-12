# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The binding protocol: the code behind a spec.

A binding is staged with the values that stay fixed and returns a callable
over the rest, the blanks. It may compute what depends only on the fixed
values when staged or on the first call, and hold it; a request outside a
stage is staged with no blanks and called once::

    binding.stage(values, ())()                      # a plain request
    call = binding.stage(fixed, ('bins',))           # a stage
    call(bins=100)

How a binding computes is invisible in the records: a call through a stage
returns what the plain request returns. A framework accepts a plain
:data:`Function` too, as a binding that computes nothing ahead.

An accumulator keeps a held state, made for the values of every field but the
tables it fills. A binding of a spec with table fields may make held states,
like ``ess.reduce.streaming.StreamProcessor``::

    held = binding.held_state({'scale': 2.0})    # what depends on them, once
    held.push({'runs': {'run': run_611}})        # a row per named table, data read
    held.outputs()                               # {'normalized': ...}, every output

Its outputs after rows are pushed in order are those of the plain request
whose tables hold these rows, with the same other values. It may add each push
in place, and its outputs may share memory with what it holds: the caller is
done with the outputs of a state before it pushes the next rows (see
:meth:`HeldState.outputs`). A binding without ``held_state`` works in an
accumulator too: the framework keeps the rows and computes the plain request
over them when a state is read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

Function = Callable[..., Mapping[str, Any]]
"""
Takes parameter values by field name, with data read, and returns the outputs.

It gets every field of the params model, defaults filled in, so it declares no
defaults of its own.
"""


@runtime_checkable
class Binding(Protocol):
    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        """
        A callable over ``blanks`` that computes the outputs with ``fixed``.

        Both this method and the callable may be called from several threads
        at once. Neither may modify the values it is given, and the callable
        may return the same object in several calls: records made through one
        stage share what does not depend on the blanks.
        """
        ...


class HeldState(Protocol):
    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        """
        Add one row to each named table field, with data read.

        The rows of one push enter one state, so a binding may add them at
        once. It may modify what it holds in place, and so change an output
        returned before it, but not the rows. A held state that starts from
        the first row's values copies them, so that adding in place never
        changes the output they came from. Whether a push needs memory for a
        second copy of what it holds is the binding's choice: binning a run's
        events into a held grid needs none, histogramming the run and adding
        the histogram needs one.
        """
        ...

    def outputs(self) -> Mapping[str, Any]:
        """
        Every output of the spec over the rows pushed so far.

        They may share memory with what the held state holds. An output the
        spec declares optional may be left out. The caller calls this once per
        state that is read, keeps what it returns until the next push, and
        pushes only once every reader of these outputs is done.
        """
        ...


@runtime_checkable
class HeldStateBinding(Binding, Protocol):
    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        """
        A new held state with nothing pushed.

        ``fixed`` holds every parameter but the tables it fills, with data
        read and defaults filled in. Each value is typed by its own field and
        that field's validators; the params model's own validators run on
        the plain request of a state when it is read. Its ``push`` and
        ``outputs`` are called from one thread at a time.

        The held state keeps what it holds for each table apart, so that the
        order of pushes into different tables does not change a state. A
        binding that cannot do so for the tables ``fixed`` leaves out raises,
        as it may for any values it refuses; the accumulator then stops.
        """
        ...

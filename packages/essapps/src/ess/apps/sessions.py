# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Sessions and the holders in them: stages and accumulators.

A holder never changes what a record says: a call through a stage makes the
record of the filled template, and computing an accumulator makes the record
of its accumulator spec over the elements pushed so far.

These holders keep their definition, not computed values: every call computes
the full request. Keeping values in memory, so that the next call computes
less, needs ``sciline.Stage`` and ``sciline.Accumulator`` behind the bound
workflows, and changes no record.
"""

from __future__ import annotations

from typing import Any, Self

from ess.reduce.spec import OutputRef

from .accumulators import AccumulatorSpec
from .records import Record, Request, Template


class Session:
    """A lifetime for holders; ending it releases them."""

    def __init__(self, where: str | None = None) -> None:
        self.where = where
        self.open = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.open = False

    def stage(self, template: Template) -> Stage:
        return Stage(self, template)

    def accumulator(self, spec: AccumulatorSpec) -> Accumulator:
        if not isinstance(spec, AccumulatorSpec):
            raise TypeError(f'{spec.name} is not an accumulator spec')
        return Accumulator(self, spec)


class _Holder:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _check_open(self) -> None:
        if not self._session.open:
            raise RuntimeError('the session of this holder has ended')


class Stage(_Holder):
    """A template held in a session; a call fills its blanks."""

    def __init__(self, session: Session, template: Template) -> None:
        super().__init__(session)
        self.template = template

    def request(self, values: dict[str, Any]) -> Request:
        self._check_open()
        return self.template.fill(values)


class Accumulator(_Holder):
    """The combination of the elements pushed into it, under an accumulator spec."""

    def __init__(self, session: Session, spec: AccumulatorSpec) -> None:
        super().__init__(session)
        self.spec = spec
        self._fields = tuple(spec.element.model_fields)
        self._elements: list[dict[str, OutputRef]] = []

    def push(self, element: Record | dict[str, OutputRef]) -> None:
        """
        Push a record's outputs named like the element's fields, or references.

        The record may still be pending; its other outputs are not pushed.
        """
        self._check_open()
        if isinstance(element, Record):
            element = {f: element.ref(f) for f in self._fields}
        if set(element) != set(self._fields):
            raise ValueError(f'an element has the fields {self._fields}')
        self._elements.append(element)

    def request(self) -> Request:
        """The accumulator spec over the elements pushed so far."""
        self._check_open()
        return Request(
            self.spec, {f: [e[f] for e in self._elements] for f in self._fields}
        )

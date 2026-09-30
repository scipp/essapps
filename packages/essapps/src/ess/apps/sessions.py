# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Sessions and the holders in them: stages and accumulators.

A holder never changes what a record says: a call through a stage makes the
record of the filled template, and computing an accumulator makes the record
of its accumulator spec over the elements pushed so far.

Both live in the backend: a stage keeps what its binding computed from the
template's values, and an accumulator keeps its elements and, if its binding
can accumulate, their combined value.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Self

from .accumulators import AccumulatorSpec
from .records import Element, Record, Request, SpecId, Template

if TYPE_CHECKING:
    from .backend import Backend


class Session:
    """A lifetime for holders; ending it releases them once their requests have run."""

    def __init__(
        self, backend: Backend, proposal: str, where: str | None = None
    ) -> None:
        self.where = where
        self.open = True
        self._backend = backend
        self._proposal = proposal
        self._id = backend.open_session(proposal)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.open:
            self.open = False
            self._backend.close_session(self._id)

    def stage(self, template: Template) -> Stage:
        if not self.open:
            raise RuntimeError('this session has ended')
        stage_id = self._backend.open_stage(
            self._id, template.spec, template.blanks, self._proposal
        )
        return Stage(self, template, stage_id)

    def accumulator(self, spec: AccumulatorSpec) -> Accumulator:
        if not isinstance(spec, AccumulatorSpec):
            raise TypeError(f'{spec.name} is not an accumulator spec')
        if not self.open:
            raise RuntimeError('this session has ended')
        accumulator_id = self._backend.open_accumulator(
            self._id, SpecId.of(spec), self._proposal
        )
        pushing = functools.partial(self._backend.push, proposal=self._proposal)
        return Accumulator(self, spec, accumulator_id, pushing)


class _Holder:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _check_open(self) -> None:
        if not self._session.open:
            raise RuntimeError('the session of this holder has ended')


class Stage(_Holder):
    """
    A template held in a session; a call fills its blanks.

    ``id`` names the stage in the backend.
    """

    def __init__(self, session: Session, template: Template, stage_id: str) -> None:
        super().__init__(session)
        self.template = template
        self.id = stage_id

    def request(self, values: dict[str, Any]) -> Request:
        self._check_open()
        return self.template.fill(values)


class Accumulator(_Holder):
    """
    The combination of the elements pushed into it, under an accumulator spec.

    ``id`` names the accumulator in the backend; submitting it makes a
    snapshot, a record of the accumulator spec over the elements pushed so far.
    """

    def __init__(
        self,
        session: Session,
        spec: AccumulatorSpec,
        accumulator_id: str,
        push: Callable[[str, Element], None],
    ) -> None:
        super().__init__(session)
        self.spec = spec
        self.id = accumulator_id
        self._fields = tuple(spec.element.model_fields)
        self._push = push

    def push(self, element: Record | Element) -> None:
        """
        Push a record's outputs named like the element's fields, or references.

        The record may still be pending; its other outputs are not pushed.
        """
        self._check_open()
        if isinstance(element, Record):
            element = {f: element.ref(f) for f in self._fields}
        self._push(self.id, element)

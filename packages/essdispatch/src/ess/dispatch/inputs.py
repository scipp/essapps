# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The inputs factory: what turns the references of a request into the inputs
of a workflow.

A request gives each input by reference, or a list of references for a list
field. In the *request phase*, before any data is read, the backend checks
each reference by its place: :meth:`InputsFactory.refs` walks the inputs of a
request alongside the fields of the spec's inputs model and gives each
reference with the type of the data it must fit. In the *call phase*, the
factory reads the data the references name and validates them by the model's
fields::

    factory = InputsFactory(IofQInputs, read)
    factory.refs(request.inputs)              # request phase
    inputs = factory.make(request.inputs)     # call phase: an IofQInputs
    values = factory.make_fields(fixed)       # call phase: each value by its field

:meth:`~InputsFactory.make` validates a whole inputs instance, the model's
own validators included, for a plain request. A stage, an accumulator, and a
push have some of the inputs only, so :meth:`~InputsFactory.make_fields`
validates each value by its own field.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from ess.spec import Ref, as_ref
from ess.spec.field_types import element, optional

from .records import SubmitError, model_of


class InputsFactory:
    """
    The inputs of a spec, by the fields of its inputs ``model``.

    ``read`` takes inputs by name, each a reference, ``None``, or a list of
    them, and returns them with each reference replaced by the data it
    names: a local path for a dataset, the value of an output, or one element
    of it for a reference with a key. A dataset fits a file field only (see
    :func:`ess.spec.fits`), so a file field gets a path, and any other field
    the object.
    """

    def __init__(
        self,
        model: type[BaseModel],
        read: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.model = model
        self._read = read

    def refs(self, inputs: Mapping[str, Any]) -> list[tuple[str, Ref, Any]]:
        """
        Each reference in ``inputs``, with its place and the type of the data
        it must fit: the field's type, or its element's for a list field.

        Raises ``SubmitError`` for a name the model lacks, and for a value
        that is not a reference, ``None`` for an optional field, or for a list
        field a list of them. So a file given as a string is refused: a file
        is always given by reference.
        """
        found = []
        for name, value in inputs.items():
            field = self.model.model_fields.get(name)
            if field is None:
                raise SubmitError(f'{name}: not an input of {self.model.__name__}')
            tp, nullable = optional(field.annotation)
            if (item := element(tp)) is None:
                found += _ref(name, value, tp, nullable)
            elif not isinstance(value, list):
                raise SubmitError(f'{name}: {value!r} is not a list of references')
            else:
                item, nullable = optional(item)
                for i, cell in enumerate(value):
                    found += _ref(f'{name}[{i}]', cell, item, nullable)
        return found

    def make(self, inputs: Mapping[str, Any]) -> BaseModel:
        """The inputs instance from every input of a request, read."""
        return self.model.model_validate(self._read(dict(inputs)))

    def make_fields(
        self, inputs: Mapping[str, Any], *, rows: bool = False
    ) -> dict[str, Any]:
        """
        Some inputs of a request, read, each validated by its own field; with
        ``rows``, as columns of rows pushed (see :func:`.records.model_of`).
        """
        data = self._read(dict(inputs))
        made = model_of(self.model, data.keys(), rows=rows).model_validate(data)
        return {name: getattr(made, name) for name in data}


def _ref(where: str, value: Any, tp: Any, nullable: bool) -> list[tuple[str, Ref, Any]]:
    """The reference ``value`` at ``where``, or none for an allowed ``None``."""
    if value is None and nullable:
        return []
    if (ref := as_ref(value)) is None:
        raise SubmitError(
            f'{where}: {value!r} is not a reference; data are given by reference, '
            'a file as dataset(path=...)'
        )
    return [(where, ref, tp)]


def map_inputs(inputs: Mapping[str, Any], fn: Callable[[Ref], Any]) -> dict[str, Any]:
    """
    ``inputs`` by name, each a reference, ``None``, or a list of them, with
    each reference replaced by ``fn(ref)``; other values are left as they are.
    """

    def one(value: Any) -> Any:
        return fn(value) if isinstance(value, Ref) else value

    return {
        name: [one(v) for v in value] if isinstance(value, list) else one(value)
        for name, value in inputs.items()
    }

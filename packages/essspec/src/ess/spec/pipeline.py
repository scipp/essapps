# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec: :class:`PipelineBinding`.

``packages/essspec/docs/bindings.md`` is a guide for workflow authors.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).

:meth:`PipelineBinding.pipeline` copies what ``make_pipeline`` returns before
it sets the fixed values at their keys, so that ``make_pipeline`` may hold one
pipeline and return it for every request. A stage cuts that pipeline with two
``sciline.Stage`` objects (:class:`_Cut`): the first, with no inputs, finds the
keys that the outputs depend on, so that a blank whose keys no output uses is
not fed; the second, with the used keys of the blanks as inputs, computes on
its first call what does not depend on them, and holds it. An optional output
is computed only if the pipeline has a value or a provider at its key, so that
``sciline.Stage`` is never asked for a key that it cannot compute.

A union of groups with several classes keeps the key of each member by class
(``PipelineBinding._unions``), and a value sets the key of the first class in
its MRO that is a member.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from types import MappingProxyType, UnionType
from typing import Any, NewType, Union, get_args, get_origin

import sciline
from pydantic import BaseModel

from . import field_types
from ._workflow_spec import WorkflowSpec
from .binding import StageCall, fixed_values

Key = Any


class PipelineBinding[P: BaseModel, O: BaseModel]:
    """
    A sciline pipeline behind a spec: plain requests and stages.

    Each parameter sets the sciline key of its type, and each output is
    computed at the key of its type. How parameters map to keys, ``keys=``,
    unions, ``None``, and what ``make_pipeline`` may hold are in
    ``packages/essspec/docs/bindings.md``.

    Parameters
    ----------
    spec:
        The spec whose requests the binding computes.
    make_pipeline:
        Makes the pipeline from the fixed values, by name, with defaults
        filled in.
    keys:
        A tuple of sciline keys for a parameter or an output, by name:
        ``(K,)``, a key for each member of a union, or ``()`` for a parameter
        that sets none. Fields not named take the keys of their types.

    Raises
    ------
    ValueError
        If ``keys`` names no field of the spec or gives a wrong number of
        keys, if a parameter whose class names no role lacks its ``keys``,
        if one typed ``X | None`` has keys other than ``()``, or if two
        fields share a key.
    TypeError
        If a value of ``keys`` is not a tuple.
    """

    def __init__(
        self,
        spec: WorkflowSpec[P, O],
        make_pipeline: Callable[[Mapping[str, Any]], sciline.Pipeline],
        *,
        keys: Mapping[str, tuple[Key, ...]] | None = None,
    ) -> None:
        keys = dict(keys or {})
        declared = spec.params.model_fields
        outputs = spec.outputs.model_fields
        if unknown := keys.keys() - declared.keys() - outputs.keys():
            raise ValueError(f'{sorted(unknown)}: not fields of {spec.name}')
        if not_tuples := sorted(n for n, k in keys.items() if type(k) is not tuple):
            raise TypeError(f'{not_tuples}: keys= gives a tuple of keys')
        if wrong := sorted(n for n in outputs if n in keys and len(keys[n]) != 1):
            raise ValueError(f'{wrong}: an output is computed as one key')
        unions = {
            n: members
            for n in declared
            if (members := _members(spec.params, n)) is not None
        }
        own = {
            n: unions.get(n, (field_types.plain(f.annotation),))
            for n, f in declared.items()
        }
        if wrong := sorted(
            n for n in declared if len(keys.get(n, ())) not in (0, len(own[n]))
        ):
            raise ValueError(
                f'{wrong}: a parameter sets one key, or none; a union one for '
                'each member'
            )
        if keyless := sorted(
            n for n in declared if n not in keys and not _grouped(spec.params, n)
        ):
            raise ValueError(
                f'{spec.name}: {keyless}: a parameter whose type, or whose element '
                'type for a list, is not a group or a union of groups gives its '
                'keys=: a key such as int, or the Path of a file field, names no '
                'role, so no provider would read it, and the value would have no '
                'effect'
            )
        if optional_params := sorted(
            n
            for n, f in declared.items()
            if field_types.optional(f.annotation)[1] and keys.get(n) != ()
        ):
            raise ValueError(
                f'{spec.name}: {optional_params}: a parameter typed X | None sets no '
                'key; give it keys=() and use it in make_pipeline'
            )
        all_keys = {
            **{n: keys.get(n, own[n]) for n in declared},
            **{
                n: keys.get(n, (field_types.optional(f.annotation)[0],))
                for n, f in outputs.items()
            },
        }
        names: dict[Key, list[str]] = {}
        for name, field_keys in all_keys.items():
            for key in dict.fromkeys(field_keys):  # a union may give one key twice
                names.setdefault(key, []).append(name)
        if shared := [
            f'{" and ".join(n)} have the key {_key_text(key)}'
            for key, n in names.items()
            if len(n) > 1
        ]:
            raise ValueError(
                f'{spec.name}: {"; ".join(shared)}; no two fields share a key'
            )
        self.spec = spec
        self._keys = MappingProxyType(all_keys)
        # Of each union with keys: the key of each member, by its class.
        self._unions = {
            n: dict(zip(members, all_keys[n], strict=True))
            for n, members in unions.items()
            if all_keys[n]
        }
        self._make_pipeline = make_pipeline

    @property
    def keys(self) -> Mapping[str, tuple[Key, ...]]:
        """The sciline keys of each parameter and output, by name, as in ``keys=``."""
        return self._keys

    def pipeline(self, fixed: Mapping[str, Any]) -> sciline.Pipeline:
        """
        The pipeline ``make_pipeline`` makes from ``fixed``, by name, with each
        value of ``fixed`` set at its key: the graph that a stage cuts.

        Raises
        ------
        ValueError
            If ``fixed`` names no parameter of the spec, or if
            ``make_pipeline`` holds a value at a key of a union whose members
            have keys of their own.
        """
        spec = self.spec
        if unknown := sorted(fixed.keys() - spec.params.model_fields.keys()):
            raise ValueError(f'{unknown}: not parameters of {spec.name}')
        made = self._make_pipeline(fixed)
        if held := [
            f'{_key_text(key)}, a key of the union {name}'
            for name in self._unions
            if len(set(self._keys[name])) > 1
            for key in dict.fromkeys(self._keys[name])
            if _holds_value(made, key)
        ]:
            raise ValueError(
                f'{spec.name}: make_pipeline holds a value at {"; ".join(held)}: '
                'a member whose key leads there would have no effect'
            )
        pipeline = made.copy()
        for key, value in self._by_key(fixed).items():
            pipeline[key] = value
        return pipeline

    def compute(self, params: P) -> O:
        return self.make_stage(dict(params), ())({})

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[O]:
        values = fixed_values(self.spec, fixed, blanks)
        left_open = frozenset(blanks)
        if keyless := sorted(n for n in left_open if not self._keys[n]):
            raise ValueError(f'{keyless}: without a key, cannot be blanks')
        if unions := sorted(left_open & self._unions.keys()):
            raise ValueError(
                f'{unions}: a union cannot be a blank, since its key may depend '
                'on its value, and make_pipeline may branch on it'
            )
        pipeline = self.pipeline(values)
        output_fields = self.spec.outputs.model_fields
        output_keys = {name: self._keys[name][0] for name in output_fields}
        outputs = [
            key
            for name, key in output_keys.items()
            if output_fields[name].is_required() or _computable(pipeline, key)
        ]
        cut = _Cut(pipeline, outputs, [k for n in left_open for k in self._keys[n]])
        output_model = self.spec.outputs

        def call(blank_values: Mapping[str, Any]) -> O:
            if blank_values.keys() != left_open:
                raise ValueError(f'give the blanks {sorted(left_open)}')
            results = cut(self._by_key(blank_values))
            return output_model(
                **{n: results.get(key) for n, key in output_keys.items()}
            )

        return call

    def _by_key(self, values: Mapping[str, Any]) -> dict[Key, Any]:
        """
        ``values`` by the key each sets, a union's by the member that the
        value is; a field without a key is left out.
        """
        return {key: v for n, v in values.items() for key in self._set(n, v)}

    def _set(self, name: str, value: Any) -> tuple[Key, ...]:
        """
        The keys that ``value`` of the field ``name`` sets: of a union, that
        of the member nearest to the class of ``value``.
        """
        if (key_of := self._unions.get(name)) is None:
            return self._keys[name]
        for cls in type(value).__mro__:
            if cls in key_of:
                return (key_of[cls],)
        raise TypeError(f'{name}: {value!r} is of no member of the union')


def _members(model: type[BaseModel], name: str) -> tuple[Any, ...] | None:
    """
    The members of the parameter ``name`` of ``model``, if it is a union of
    groups of more than one class, without ``| None`` and in the order of
    the union. ``None`` for any other parameter, such as a list or
    ``BeamCentre | Referenced[BeamCentre]``.
    """
    if field_types.is_list(model, name):
        return None
    tp = field_types.optional(model.model_fields[name].annotation)[0]
    return get_args(tp) if get_origin(tp) in (Union, UnionType) else None


def _grouped(model: type[BaseModel], name: str) -> bool:
    """
    Whether the parameter ``name`` of ``model``, or its element for a list,
    is typed a group or a union of groups, ``Referenced[T]`` among them,
    possibly ``X | None``.
    """
    tp = field_types.value_type(model.model_fields[name].annotation)
    members = get_args(tp) if get_origin(tp) in (Union, UnionType) else (tp,)
    return all(field_types.is_group(m) for m in members)


def _key_text(key: Key) -> str:
    """A key as a message names it: ``BeamCentre``, ``list[Run]``."""
    return key.__name__ if isinstance(key, type | NewType) else repr(key)


# sciline keeps a node's value or provider as an attribute of the node in its
# graph, under the names 'value' and 'provider'.


def _computable(pipeline: sciline.Pipeline, key: Key) -> bool:
    """Whether the pipeline has a value or a provider for ``key``."""
    node = pipeline.underlying_graph.nodes.get(key, {})
    return 'value' in node or 'provider' in node


def _holds_value(pipeline: sciline.Pipeline, key: Key) -> bool:
    """Whether the pipeline holds a value at ``key``, rather than a provider."""
    return 'value' in pipeline.underlying_graph.nodes.get(key, {})


class _Cut:
    """A ``sciline.Stage`` of ``outputs``, fed those of ``inputs`` that they use."""

    def __init__(
        self, pipeline: sciline.Pipeline, outputs: Sequence[Key], inputs: Sequence[Key]
    ) -> None:
        if inputs:
            used = sciline.Stage(pipeline, outputs=outputs, inputs=()).keys
            inputs = [k for k in inputs if k in used]
        self._inputs = inputs
        self._stage = sciline.Stage(pipeline, outputs=outputs, inputs=inputs)

    def __call__(self, values: Mapping[Key, Any]) -> dict[Key, Any]:
        return self._stage.run({k: values[k] for k in self._inputs})

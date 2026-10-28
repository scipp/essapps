# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec: :class:`PipelineBinding`.

This is the only module of ``ess.spec`` that imports sciline (extra
``[sciline]``).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any, NewType

import sciline
from pydantic import BaseModel

from . import field_types, places
from ._workflow_spec import WorkflowSpec
from .binding import StageCall, fixed_values

Key = Any


class PipelineBinding[P: BaseModel, O: BaseModel]:
    """
    A sciline pipeline behind a spec: plain requests and stages.

    ``make_pipeline`` makes the pipeline from the fixed values, by name, with
    defaults filled in, so that the graph may depend on them; it gets no
    blank. The binding copies what it returns before it sets values in it, so
    ``make_pipeline`` may return a pipeline that it holds. Each parameter
    sets the sciline key of its type, without ``Annotated`` metadata, such as
    ``Reduction``; a list parameter, such as a table, sets the key of its type,
    a list, to its list, ``list[Run]`` or ``list[GivenCentre | FindCentre]``.
    A union of groups sets the key of the alternative that the value's tag
    names (see below). Each output is computed as the key of its type without
    ``| None``, since an optional output means that the graph may lack it,
    not that a provider returns ``None``; an output that a provider returns
    as ``X | None`` needs ``keys=``.

    A parameter typed ``X | None`` sets no key. Each value of a union sets
    one key, but ``None`` has no key to set: setting ``X`` to ``None`` would
    replace a provider of ``X``, and setting nothing would leave the meaning
    of ``None`` to whatever the pipeline holds or computes at ``X``. So
    ``keys=`` must give such a parameter no key, ``()``, and
    ``make_pipeline`` says what ``None`` means::

        def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
            pipeline = sciline.Pipeline(providers)
            if fixed['direct_beam'] is None:
                pipeline[sans.DirectBeam] = None  # esssans' providers take None
            else:
                pipeline[sans.DirectBeamFilename] = fixed['direct_beam'].path
            return pipeline

        PipelineBinding(SPEC, make_pipeline, keys={'direct_beam': ()})

    A union of groups has a key for each alternative, the alternative as
    declared, in the order of the union. The binding sets the key of the
    alternative that the value's tag names, read from the value's tag field,
    and not that of the value's type: the value may be an instance of a
    subclass of an alternative. Here one graph serves both alternatives, since
    ``find_centre`` leads from one alternative's key to the other's::

        class IofQParams(BaseModel):
            sample: Sample
            beam_centre: Annotated[
                GivenCentre | FindCentre, Field(discriminator='kind')
            ] = GivenCentre()

        def find_centre(settings: FindCentre, data: SampleData) -> GivenCentre: ...

        binding = PipelineBinding(
            IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq])
        )
        binding.keys['beam_centre']  # (GivenCentre, FindCentre)
        # tag 'given': pipeline[GivenCentre] = value, which replaces find_centre
        # tag 'find':  pipeline[FindCentre] = value, and find_centre computes
        #              GivenCentre

    Where the alternatives need different providers for one key,
    ``make_pipeline`` branches on the fixed value. That always works, since a
    union is never a blank, even with one key for all alternatives: its key
    may depend on its value. A union typed ``U | None`` sets no key, as
    ``X | None`` does. A union in a list, such as a table, sets no key of its
    own: the list's key holds it, and a provider reads the tag of each row.

    ``keys`` gives a tuple of keys for a parameter or an output: ``(K,)``,
    for a union a key for each alternative in the order of the union, or
    ``()`` for a parameter that sets none. It names keys of a workflow's own,
    such as the key at which the graph computes an output, or keys for two
    parameters of one class, which may instead each have a class of their
    own, ``class Can(Run): ...``::

        PipelineBinding(
            IOFQ, make_pipeline, keys={'iofq': (BackgroundSubtractedIofQ,)}
        )

    No two fields, parameters or outputs, share a key, over all
    alternatives: an output computed at a parameter's key would be the value
    the binding was given, which an output must not share memory with. A
    union's own ``keys=`` may give its alternatives one key, ``(K, K)``: the
    value of either alternative is then set at ``K``.

    Defaults belong to the spec: ``make_pipeline`` holds no value at a key
    that a parameter's key leads to, since the value replaces the provider
    that would read the parameter, and the request's value has no effect.
    The binding cannot check this: the provider is gone from the graph. A
    value held at the key of a parameter is harmless, since the binding sets
    the request's value over it, except at a key of a union whose
    alternatives have keys of their own, which one alternative sets and the
    others do not. If the pipeline of the beam-centre example held a default
    ``BeamCentre``, the tag 'find' would set ``FindCentre``, but ``iofq``
    would read the default, and ``find_centre`` would never run. The binding
    refuses a value held at any key of such a union, since it cannot tell
    which key leads to which: the held value has replaced the provider that
    would show it. ``make_pipeline`` replaces such a value with a provider,
    ``pipeline.insert(...)``, such as an adapter from the spec's group to the
    workflow's key. ``make_pipeline`` sets no value in a pipeline it holds,
    since the binding copies the pipeline only after ``make_pipeline``
    returns it, and a value set for one request would stay for the next.

    :attr:`keys` and :meth:`pipeline` give these keys and the graph, so that
    the driver of a multi-run spec can reuse them.

    A stage cuts :meth:`pipeline` of the fixed values at the keys of the
    blanks with ``sciline.Stage``, which computes what does not depend on
    them on the first call and caches it. A plain request is a stage with no
    blanks, called once. ``make_pipeline`` gets no blank, so a field that
    changes the graph cannot be a blank. A key that no output depends on is
    not fed: its value cannot change the outputs. An optional output is
    computed if the pipeline has a value or a provider for its key, and is
    ``None`` otherwise. ``fixed`` and ``blanks`` are checked by
    ``fixed_values`` of :mod:`ess.spec.binding`, and a field without a key,
    or a union, cannot be a blank.

    The author of the pipeline promises that it is pure: it modifies no value
    it is given, and returns the same results for the same values.
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
            n: union
            for n, f in declared.items()
            if (union := places.alternatives(f.annotation, f.discriminator)) is not None
        }
        own = {
            n: tuple(unions[n][1].values())
            if n in unions
            else (field_types.plain(f.annotation),)
            for n, f in declared.items()
        }
        if wrong := sorted(
            n for n in declared if len(keys.get(n, ())) not in (0, len(own[n]))
        ):
            raise ValueError(
                f'{wrong}: a parameter sets one key, or none; a union one for '
                'each alternative'
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
        # The tag field of each union with keys, and its key by tag.
        self._tags: dict[str, tuple[str, dict[str, Key]]] = {}
        for name, (tag, groups) in unions.items():
            if all_keys[name]:
                key_of = dict(zip(own[name], all_keys[name], strict=True))
                self._tags[name] = (tag, {t: key_of[m] for t, m in groups.items()})
        self._make_pipeline = make_pipeline

    @property
    def keys(self) -> Mapping[str, tuple[Key, ...]]:
        """
        The sciline keys of each parameter and output, by name: ``(K,)``, a
        key for each alternative of a union, or ``()`` for a parameter that
        sets none.
        """
        return self._keys

    def pipeline(self, fixed: Mapping[str, Any]) -> sciline.Pipeline:
        """
        The pipeline ``make_pipeline`` makes from ``fixed``, by name, with each
        value of ``fixed`` set at its key: the graph that a stage cuts.

        A stage passes its fixed values with defaults filled in. A driver
        passes the fixed values it is made with: every value that is not a
        table, defaults filled in. So a field that is a table of the
        multi-run spec cannot shape the graph, as a blank cannot.
        """
        spec = self.spec
        if unknown := sorted(fixed.keys() - spec.params.model_fields.keys()):
            raise ValueError(f'{unknown}: not parameters of {spec.name}')
        made = self._make_pipeline(fixed)
        if held := [
            f'{_key_text(key)}, a key of the union {name}'
            for name, (_, by_tag) in self._tags.items()
            if len(set(by_tag.values())) > 1
            for key in dict.fromkeys(by_tag.values())
            if _holds_value(made, key)
        ]:
            raise ValueError(
                f'{spec.name}: make_pipeline holds a value at {"; ".join(held)}: '
                'an alternative whose key leads there would have no effect'
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
        if unions := sorted(left_open & self._tags.keys()):
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
        ``values`` by the key each sets, a union's by its tag; a field without
        a key is left out.
        """
        return {key: v for n, v in values.items() for key in self._set(n, v)}

    def _set(self, name: str, value: Any) -> tuple[Key, ...]:
        """The keys that ``value`` of the field ``name`` sets."""
        if name not in self._tags:
            return self._keys[name]
        tag, by_tag = self._tags[name]
        return (by_tag[getattr(value, tag)],)


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
        return self._stage.apply({k: values[k] for k in self._inputs})

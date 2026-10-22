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
from typing import Any

import sciline
from pydantic import BaseModel

from ._workflow_spec import WorkflowSpec
from .binding import StageCall, fixed_values
from .field_types import optional, plain
from .places import alternatives

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
    a list, to its list, ``list[Run]`` or ``list[BeamCentre | FindCentre]``.
    A union of groups sets the key of the alternative that the value's tag
    names (see below). Each output is computed as the key of its type without
    ``| None``, since an optional output means that the graph may lack it,
    not that a provider returns ``None``; an output that a provider returns
    as ``X | None`` needs ``keys=``.

    A parameter typed ``X | None`` sets no key. For ``None``, setting ``X`` to
    ``None`` would replace a provider of ``X``, and setting no key would leave
    whatever the pipeline holds at ``X``, which the request did not choose.
    So ``keys=`` must give such a parameter no key, ``()``, and
    ``make_pipeline`` uses the field itself::

        def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
            pipeline = sciline.Pipeline(providers)
            if fixed['direct_beam'] is None:
                pipeline[DirectBeam] = None
            else:
                pipeline[DirectBeamFilename] = fixed['direct_beam']
            return pipeline

        PipelineBinding(SPEC, make_pipeline, keys={'direct_beam': ()})

    A union of groups has a key for each alternative, the alternative as
    declared, a ``NewType`` included, in the order of the union. The binding
    sets the key of the alternative that the value's tag names, read from the
    value's tag field, and not that of the value's type: the value may be an
    instance of a subclass of an alternative, or of a ``NewType``
    alternative's group. Here one graph serves both alternatives, since
    ``find_centre`` leads from one alternative's key to the other's::

        class IofQParams(BaseModel):
            sample: SampleFile
            beam_centre: Annotated[
                BeamCentre | FindCentre, Field(discriminator='kind')
            ] = BeamCentre()

        def find_centre(settings: FindCentre, data: SampleData) -> BeamCentre: ...

        binding = PipelineBinding(
            IOFQ, lambda fixed: sciline.Pipeline([find_centre, ..., iofq])
        )
        binding.keys['beam_centre']  # (BeamCentre, FindCentre)
        # tag 'given': pipeline[BeamCentre] = value, which replaces find_centre
        # tag 'find':  pipeline[FindCentre] = value, and find_centre computes
        #              BeamCentre

    Where the alternatives need different providers for one key,
    ``make_pipeline`` branches on the fixed value. That always works, since a
    union cannot be a blank: its key depends on its value. A union typed
    ``U | None`` sets no key, as ``X | None`` does.

    ``keys`` gives a tuple of keys for a parameter or an output: ``(K,)``,
    for a union a key for each alternative in the order of the union, or
    ``()`` for a parameter that sets none. It names keys of a workflow's own,
    such as for two parameters of one type::

        PipelineBinding(
            IOFQ,
            make_pipeline,
            keys={
                'sample': (Filename[SampleRun],),
                'iofq': (BackgroundSubtractedIofQ,),
            },
        )

    No two parameters share a key, over all alternatives, and no two
    outputs. An output may share a parameter's key: an output
    ``centre: BeamCentre`` reports the centre given or found.

    Defaults belong to the spec: ``make_pipeline`` holds no value at a key
    that a parameter sets, or at a key that such a key leads to. A value held
    there can make a request's value have no effect, and nothing says so. If
    the pipeline above held a default ``BeamCentre``, the tag 'find' would
    set ``FindCentre``, but ``iofq`` would read the default, and
    ``find_centre`` would never run. The binding does not check this.

    :attr:`keys` and :meth:`pipeline` give these keys and the graph, so that
    the driver of a multi-run spec can reuse them.

    A stage cuts :meth:`pipeline` of the fixed values at the keys of the
    blanks with ``sciline.Stage``, which computes what does not depend on
    them on the first call and holds it. A plain request is a stage with no
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
            if (union := alternatives(f.annotation, f.discriminator)) is not None
        }
        own = {
            n: tuple(unions[n][1].values()) if n in unions else (plain(f.annotation),)
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
            if optional(f.annotation)[1] and keys.get(n) != ()
        ):
            raise ValueError(
                f'{spec.name}: {optional_params}: a parameter typed X | None sets no '
                'key; give it keys=() and use it in make_pipeline'
            )
        all_keys = {
            **{n: keys.get(n, own[n]) for n in declared},
            **{
                n: keys.get(n, (optional(f.annotation)[0],)) for n, f in outputs.items()
            },
        }
        shared: dict[str, None] = {}  # a union's fields once, not once per key
        for fields in (declared, outputs):
            names: dict[Key, list[str]] = {}
            for name in fields:
                for key in dict.fromkeys(all_keys[name]):
                    names.setdefault(key, []).append(name)
            shared |= dict.fromkeys(
                ' and '.join(n) for n in names.values() if len(n) > 1
            )
        if shared:
            raise ValueError(
                f'{spec.name}: {"; ".join(shared)}: two fields have one key'
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
        pipeline = self._make_pipeline(fixed).copy()
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
                f'{unions}: a union cannot be a blank, since its key depends on '
                'its value'
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


def _computable(pipeline: sciline.Pipeline, key: Key) -> bool:
    """Whether the pipeline has a value or a provider for ``key``."""
    # sciline keeps a node's value or provider as an attribute of the node in
    # its graph, under the names 'value' and 'provider'.
    node = pipeline.underlying_graph.nodes.get(key, {})
    return 'value' in node or 'provider' in node


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
        return self._stage.compute({k: values[k] for k in self._inputs})

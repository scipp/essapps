# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for, and what the workspace made of it.

Both are plain data. ``docs/developer/README.md``, "Requests and records",
introduces them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, with_config
from pydantic.fields import FieldInfo

from ess.spec import FileRef, OutputRef, Ref, WorkflowSpec
from ess.spec.refs import ParamsError, iter_refs, map_refs

from .errors import FieldError, SubmitError


def _is_tag(info: FieldInfo) -> bool:
    """Whether the type of a field is a ``Literal`` of one value."""
    return (
        get_origin(info.annotation) is Literal and len(get_args(info.annotation)) == 1
    )


def _with_tags(value: Any, dumped: Any) -> Any:
    """``dumped``, the dump of ``value`` without unset fields, with tags added."""
    if isinstance(value, BaseModel) and not isinstance(value, Ref):
        for name, info in type(value).model_fields.items():
            if name in dumped:
                dumped[name] = _with_tags(getattr(value, name), dumped[name])
            elif _is_tag(info):
                dumped[name] = getattr(value, name)
    elif isinstance(value, list | tuple):
        return type(dumped)(
            _with_tags(v, d) for v, d in zip(value, dumped, strict=True)
        )
    elif isinstance(value, dict):
        return {k: _with_tags(v, dumped[k]) for k, v in value.items()}
    return dumped


def as_values(value: Any) -> Any:
    """
    ``value`` with each model in it, except a reference, as a dict of the fields
    that were given.

    So a request, a template, and a push accept a params model or a group as a
    model or as a dict alike. Defaults are left out, so that the workspace fills
    in those of its own spec. A reference stays whole, since its kind is a
    default.

    A tag stays, given or not. A tag is a field whose type is a ``Literal`` of
    one value, such as ``mode: Literal['time'] = 'time'``, on which a union
    discriminates. The workspace cannot pick the member of the union without it.
    """
    if isinstance(value, BaseModel) and not isinstance(value, Ref):
        return _with_tags(value, value.model_dump(exclude_unset=True))
    if isinstance(value, dict):
        return {k: as_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [as_values(v) for v in value]
    return value


def read_refs(params: Any) -> Any:
    """
    Params by name, with each reference in its JSON form read as a
    :data:`~ess.spec.Ref`.

    Returns anything except a mapping unchanged, for validation to reject.
    Raises ``SubmitError`` for a malformed reference.
    """
    if not isinstance(params, Mapping):
        return params
    try:
        return map_refs(params, lambda ref: ref)
    except ParamsError as error:
        raise SubmitError.of(error) from None


Params = Annotated[dict[str, Any], BeforeValidator(read_refs)]
"""
The params of a request by name.

Every way of making them, the log's JSON included, holds references as
:data:`~ess.spec.Ref`.
"""


def _given(values: Mapping[str, Any] | BaseModel | None) -> dict[str, Any]:
    """Params by name, as :func:`as_values` returns them, with references read."""
    return {} if values is None else read_refs(as_values(values))


class SpecId(BaseModel, frozen=True):
    """
    The identity of a spec: its name and interface version.

    A caller without the workflow package passes
    ``SpecId(name='sans-iofq', version=1)`` wherever a spec is expected.
    ``record.spec`` is a ``SpecId``.
    """

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec[Any, Any] | SpecId | dict[str, Any]) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        if isinstance(spec, WorkflowSpec):
            return cls(name=spec.name, version=spec.version)
        return cls.model_validate(spec)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class Request(BaseModel, frozen=True):
    """
    A spec and its params, by name.

    ``params`` is a dict, or an instance of the params model if it holds no
    reference to an output. ``packages/essspec/docs/params.md`` states how each
    value may be written. A JSON object with the key ``"@ref"`` is a reference
    at any depth, so the references and files of a request are found without
    its spec.

    Making a request checks only the form of each reference. The workspace
    validates the rest at submission. ``params`` may be left out if every
    parameter has a default.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid')

    spec: SpecId
    params: Params = Field(default_factory=dict)

    def __init__(
        self,
        spec: WorkflowSpec[Any, Any] | SpecId,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
    ) -> None:
        super().__init__(spec=SpecId.of(spec), params=_given(params))

    def upstream(self) -> list[OutputRef]:
        """The outputs of other records that this request reads, without files."""
        return [ref for _, ref in iter_refs(self.params) if isinstance(ref, OutputRef)]

    def files(self) -> list[FileRef]:
        """The files this request names directly."""
        return [ref for _, ref in iter_refs(self.params) if isinstance(ref, FileRef)]


@dataclass(frozen=True, init=False)
@with_config(extra='forbid')
class Template:
    """
    A spec, some fixed values, and blanks that each use fills.

    Values are written as in a request. A blank is a whole parameter, possibly a
    table or a required parameter, never a field of a group. A value given for a
    blank is dropped, so a template can be made from any request's values::

        final = client.latest('iofq', member='250K')
        beamtime = Template(final.spec, params=final.request.params,
                            blanks=('sample',))

    Making a template checks no value. The workspace checks the fixed values as
    it does a request's when :meth:`~ess.apps.Client.stage` or
    :meth:`~ess.apps.Client.accumulator` opens the template. Change a template
    with ``dataclasses.replace``.
    """

    spec: SpecId
    params: Params = field(default_factory=dict)
    blanks: tuple[str, ...] = ()

    def __init__(
        self,
        spec: WorkflowSpec[Any, Any] | SpecId,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        blanks: Collection[str] = (),
    ) -> None:
        blanks = tuple(blanks)
        kept = {k: v for k, v in _given(params).items() if k not in blanks}
        object.__setattr__(self, 'spec', SpecId.of(spec))
        object.__setattr__(self, 'params', kept)
        object.__setattr__(self, 'blanks', blanks)

    def fill(self, *, params: Mapping[str, Any] | BaseModel | None = None) -> Request:
        """
        The request with the blanks filled by ``params``.

        Raises ``SubmitError`` for each blank not filled, each value the template
        fixes, and each other name given. The values are checked when the
        request is submitted.
        """
        params = _given(params)
        missing = [b for b in self.blanks if b not in params]
        fixed = sorted(params.keys() & self.params.keys())
        other = sorted(params.keys() - self.params.keys() - set(self.blanks))
        if missing or fixed or other:
            raise SubmitError(
                [FieldError(None, (b,), 'a blank, not filled') for b in missing]
                + [FieldError(None, (f,), 'fixed by the template') for f in fixed]
                + [FieldError(None, (o,), 'not a blank of the template') for o in other]
            )
        return Request(self.spec, params={**self.params, **params})


class Status(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class RecordStatus:
    """
    The status of a record, and why it failed or why the workspace cancelled it.

    ``failure`` is ``None`` for a record that is pending, completed, or
    cancelled by a client. Examples of ``failure``::

        specimens[0]: Value error, ...
        the workflow of sans-iofq/v1 returned None as iofq
        the write failed: ...
        KeyError: 'counts'

    The last is an error that a binding, its driver, the file source, or the
    store raised, shown with its type.

    ``software`` holds the installed distributions of the process that ran the
    record, as ``name==version``, as the workspace's ``software`` callable
    returned them. The default, ``installed_software``, sorts them by name. It
    is ``None`` while the record is pending, and for a record whose workflow
    never ended, such as a cancelled record or one that failed at a restart.
    """

    status: Status
    failure: str | None = None
    software: tuple[str, ...] | None = None


class Record(BaseModel, frozen=True):
    """
    A request as the workspace accepted it, with the names of its outputs but
    no output values.

    ``request`` has file names resolved, defaults filled in, inline values in
    their JSON form, and references as :data:`~ess.spec.Ref`. ``outputs`` lists
    the output names that the spec declares. ``label`` and ``member`` do not
    change the result.

    A record never changes, so every copy of it is equal. Its status is read
    with :meth:`~ess.apps.Client.status`. Running the same request again makes
    a new record.
    """

    id: str
    request: Request
    proposal: str
    submitter: str
    created: datetime
    outputs: tuple[str, ...]
    label: str | None = None
    member: str | None = None

    @property
    def spec(self) -> SpecId:
        return self.request.spec

    def ref(self, output: str, key: str | None = None) -> OutputRef:
        """
        A reference to ``output``, or to its element ``key`` if the output is a
        list or a dict.

        The key of an element of a list is its index, such as ``'0'``. A key
        that the output lacks fails the record that reads it. The record may be
        pending, so a chain is submitted without waiting.
        """
        if output not in self.outputs:
            raise KeyError(f'{self.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output, key=key)

    def refs(self, *outputs: str) -> dict[str, OutputRef]:
        """A reference to each of ``outputs`` by name, or to every output."""
        return {name: self.ref(name) for name in outputs or self.outputs}


@dataclass(frozen=True)
class ArrayInfo:
    """
    What an array is, without its data.

    ``sizes`` by dimension name, ``unit`` as text if it has one, and the names
    of its ``coords``.
    """

    sizes: Mapping[str, int]
    unit: str | None
    coords: tuple[str, ...]

    @classmethod
    def of(cls, array: Any) -> ArrayInfo:
        """The info of a scipp variable or data array, or a value like one."""
        unit = getattr(array, 'unit', None)
        return cls(
            sizes=dict(zip(array.dims, array.shape, strict=True)),
            unit=None if unit is None else str(unit),
            coords=tuple(getattr(array, 'coords', ())),
        )


@dataclass(frozen=True)
class Changes:
    """
    The records that changed after a position of the proposal's feed.

    ``records`` holds their IDs. ``cursor`` is the position to pass next. See
    :meth:`~ess.apps.protocol.WorkspaceProtocol.changes`.
    """

    records: tuple[str, ...]
    cursor: int

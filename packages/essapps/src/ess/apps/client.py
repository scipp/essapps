# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a controller calls.

A client is a connection to a workspace. It calls the workspace through
:class:`~ess.apps.protocol.WorkspaceProtocol`, which holds the rules behind the
calls. A client holds nothing, so every client of a workspace reads and
references the same values. A call that accepts records or requests accepts one,
a list, or a dict, and returns its results in the same shape.

:class:`~ess.apps.protocol.WorkspaceProtocol` states which calls wait.
:meth:`Client.wait`, :meth:`Client.compute` and :meth:`Files.watch` also wait, and
so does :meth:`Client.close` if ``on_close`` does. A GUI application makes the
other calls from its UI thread.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, Self, cast, overload

from pydantic import BaseModel

from ess.spec import FileRef, WorkflowSpec

from .errors import FieldError, SubmitError
from .files import Selector
from .protocol import Entry, Selection, WorkspaceProtocol
from .records import (
    Changes,
    Record,
    RecordStatus,
    Request,
    SpecId,
    Status,
    Template,
    as_values,
)

_WAIT = 10.0
"""How long :meth:`Client.wait` waits in one call for a change, in seconds."""


type _Items[T] = T | list[T] | Mapping[Any, T]
"""What a call on several items accepts: one item, a list, or a dict."""


def _items(what: object) -> list[Any]:
    """The items of one item, a list, or a dict."""
    if isinstance(what, Mapping):
        return list(what.values())
    if isinstance(what, list):
        return list(what)
    return [what]


def _reshape(what: object, items: list[Any]) -> Any:
    """``items`` in the shape of ``what``."""
    if isinstance(what, Mapping):
        return dict(zip(what, items, strict=True))
    if isinstance(what, list):
        return items
    return items[0]


def _filled(template: Template, each: Any) -> Any:
    """
    The template filled by each item of ``each``, in its shape.

    Raises ``SubmitError`` with the key or index of each rejected item.
    """
    requests = []
    errors: list[FieldError] = []
    for name, item in zip(_names(each), _items(each), strict=True):
        try:
            requests.append(template.fill(params=item))
        except SubmitError as error:  # noqa: PERF203
            errors += [replace(e, request_key=name) for e in error.errors]
    if errors:
        raise SubmitError(errors)
    return _reshape(each, requests)


def _names(what: object) -> list[str | int | None]:
    """How each item of ``what`` is named in errors: its key or its index."""
    if isinstance(what, Mapping):
        return [str(key) for key in what]
    if isinstance(what, list):
        return list(range(len(what)))
    return [None]


class _Shaped[I, O](Protocol):
    """A call that takes items ``I`` and returns ``O`` for each, in the same shape."""

    @overload
    def __call__(self, what: I, /) -> O: ...

    @overload
    def __call__(self, what: list[I], /) -> list[O]: ...

    @overload
    def __call__[K](self, what: Mapping[K, I], /) -> dict[K, O]: ...


class _ShapedMethod[S, I, O](Protocol):
    """A method of ``S`` that, called on an instance, is a :class:`_Shaped`."""

    def __get__(self, instance: S, owner: type[S] | None = None) -> _Shaped[I, O]: ...


def _shaped[S, I, O](
    method: Callable[[S, list[I]], list[O]],
) -> _ShapedMethod[S, I, O]:
    """Makes a method on a list of items take one item, a list, or a dict."""

    @functools.wraps(method)
    def call(self: S, what: object, /) -> Any:
        return _reshape(what, method(self, _items(what)))

    return cast('_ShapedMethod[S, I, O]', call)


def _typed_as[F](method: F) -> Callable[[Callable[..., Any]], F]:
    """Types the decorated forwarding function as ``method``."""

    def typed(forwarding: Callable[..., Any]) -> F:
        return cast('F', forwarding)

    return typed


def _named(error: FieldError, names: list[str | int | None]) -> FieldError:
    """The problem with the index of its request replaced by the caller's key."""
    if isinstance(error.request_key, int):
        return replace(error, request_key=names[error.request_key])
    return error


class Provenance(BaseModel, frozen=True):
    """
    Where a record came from: what it ran, what it read, and their files.

    ``request`` is the record's request, or for an accumulator, the plain
    request of its logged state. ``upstream`` holds every
    record read through all references, directly or through other records,
    nearest first. It stops at files.
    """

    request: Request
    upstream: tuple[Record, ...]

    def records(self) -> list[Record]:
        """Every record read through all references (``upstream``), nearest first."""
        return list(self.upstream)

    def files(self) -> list[FileRef]:
        """The files that the request and the records read name, each once."""
        ran = (self.request, *(r.request for r in self.upstream))
        return list(dict.fromkeys(d for r in ran for d in r.files()))


class Files:
    """
    The files of the workspace's file source that the proposal may read.

    A selector picks files by metadata (see :class:`~ess.apps.files.Selector`).
    A request names a file with ``file(...)``.
    """

    def __init__(self, workspace: WorkspaceProtocol) -> None:
        self._workspace = workspace

    @_shaped
    def metadata(self, refs: list[FileRef]) -> list[dict[str, Any]]:
        """
        The metadata of each file, in the shape of ``refs``.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.file_metadata`.
        """
        return self._workspace.file_metadata(refs)

    def list(self, selector: Selector) -> list[FileRef]:
        """See :meth:`~ess.apps.protocol.WorkspaceProtocol.files`."""
        return self._workspace.files(selector)

    def watch(self, selector: Selector, *, interval: float = 1.0) -> Iterator[FileRef]:
        """
        Yields the matching files: the existing ones first, then new ones, each
        once.

        Never ends. Lists the files again every ``interval`` seconds while none
        is new.
        """
        seen: set[FileRef] = set()
        while True:
            fresh = [ref for ref in self.list(selector) if ref not in seen]
            seen.update(fresh)
            yield from fresh
            if not fresh:
                time.sleep(interval)


@dataclass(frozen=True)
class Stage:
    """
    A template whose fixed part the workspace computes once.

    A call through the stage fills its blanks, and makes the record of the filled
    template, as a plain request would. ``id`` is the stage's ID in the workspace.
    """

    template: Template
    id: str


class Accumulator:
    """
    A template whose blanks are tables, filled one push at a time.

    Its outputs are those of the plain request of its logged state (see
    ``docs/developer/accumulators.md``). A client reads them with
    :meth:`Client.output`. ``template`` holds the fixed values as the workspace
    accepted them, and ``id`` is the accumulator's ID in the workspace.
    """

    def __init__(
        self,
        template: Template,
        accumulator_id: str,
        push: Callable[[dict[str, list[Any]]], None],
    ) -> None:
        self.template = template
        self.id = accumulator_id
        self._push = push

    def push(self, **rows: Sequence[Any]) -> None:
        """
        Logs rows of blank tables of the template. The rows enter one state::

            acc.push(sample=[file(run=611), file(run=612)])
            acc.push(sample=[file(run=615)], can=[file(run=614)])

        Parameters
        ----------
        **rows:
            For each table, a list of rows, each as :meth:`Client.submit`
            accepts a value of that type.

        Raises
        ------
        TypeError
            If the rows of a table are not a list.
        SubmitError
            See :meth:`~ess.apps.protocol.WorkspaceProtocol.push`.
        """
        for name, given in rows.items():
            if isinstance(given, str | BaseModel | Mapping) or not isinstance(
                given, Sequence
            ):
                raise TypeError(
                    f'{name}: the rows of a table are a list, such as [row], '
                    f'not {given!r}'
                )
        self._push({name: as_values(list(r)) for name, r in rows.items()})


class Client:
    """
    A connection to a workspace. It holds nothing.

    ``with client:`` calls :meth:`close` at the end of the block.

    Parameters
    ----------
    submitter:
        Who submits, as records name them.
    on_close:
        Called by each :meth:`close`, such as the workspace's own close for a client
        of :func:`~ess.apps.local`.
    """

    def __init__(
        self,
        workspace: WorkspaceProtocol,
        *,
        submitter: str,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._on_close = on_close
        self._position = 0  # in the proposal's feed, for wait
        self.submitter = submitter
        self.files = Files(workspace)

    @property
    def proposal(self) -> str:
        """The proposal of the workspace."""
        return self._workspace.proposal

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Calls ``on_close``, if given. The client holds nothing to release."""
        if self._on_close is not None:
            self._on_close()

    # Specs

    def spec(self, spec_id: SpecId) -> WorkflowSpec[Any, Any]:
        """
        The spec the workspace runs as ``spec_id``, such as ``record.spec``.

        Raises ``KeyError`` for a spec the workspace does not run.
        """
        for spec in self.specs():
            if SpecId.of(spec) == spec_id:
                return spec
        raise KeyError(f'unknown spec {spec_id}')

    def spec_named(self, name: str) -> WorkflowSpec[Any, Any]:
        """
        The highest version of the spec named ``name`` that the workspace binds.

        Raises ``KeyError`` if it binds none.
        """
        specs = [spec for spec in self.specs() if spec.name == name]
        if not specs:
            raise KeyError(f'the workspace binds no spec {name}')
        return max(specs, key=lambda spec: spec.version)

    def specs(self) -> list[WorkflowSpec[Any, Any]]:
        """The specs the workspace runs, one for each binding, in the order bound."""
        return self._workspace.specs()

    # Submitting

    @overload
    def submit(
        self,
        what: WorkflowSpec[Any, Any] | SpecId | Stage,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        label: str | None = None,
        member: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> Record: ...

    @overload
    def submit(
        self,
        what: Request,
        *,
        label: str | None = None,
        member: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> Record: ...

    @overload
    def submit(
        self,
        what: list[Request],
        *,
        label: str | None = None,
        member: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> list[Record]: ...

    @overload
    def submit[K](
        self,
        what: Mapping[K, Request],
        *,
        label: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> dict[K, Record]: ...

    @overload
    def submit(
        self,
        what: Stage,
        *,
        each: list[Any],
        label: str | None = None,
        member: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> list[Record]: ...

    @overload
    def submit[K](
        self,
        what: Stage,
        *,
        each: Mapping[K, Any],
        label: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> dict[K, Record]: ...

    def submit(
        self,
        what: Any,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        each: list[Any] | Mapping[Any, Any] | None = None,
        label: str | None = None,
        member: str | None = None,
        persist: bool | Sequence[str] = False,
    ) -> Any:
        """
        Submits requests, or a spec or a stage with values.

        Returns the pending records in the shape of ``what``, once the workspace
        has logged the requests. A request never references a record of its own
        call.

        Parameters
        ----------
        what:
            A spec (or its ``SpecId``) with ``params``, a :class:`Stage` with the
            values of its blanks in ``params``, or requests: one, a list, or a
            dict.
        params:
            The parameters by name, or an instance of the params model if it holds
            no reference to an output. Each parameter, or each row of a list
            parameter, is given as its field type allows (see
            ``packages/essspec/docs/params.md``)::

                client.submit(IOFQ, params={'reduction': Reduction(bins=4),
                                            'sample': file(run=60339),
                                            'transmission': t.ref('t')})
        each:
            With a :class:`Stage`, in place of ``params``: the values of its blanks
            for each of several requests, a list or a dict. They are submitted
            together, and the records come back in its shape::

                client.submit(stage, each={'water': {'sample': file(run=1)},
                                           'sds': {'sample': file(run=2)}})
        label:
            Names a sequence of records, such as all I(Q) reductions of an
            experiment. Under a label, the keys of a dict become the members.
        member:
            Names one part of the sequence, such as one sample. Not allowed with
            a dict. :meth:`records` and :meth:`latest` find records by label and
            member. Neither changes the request or its result.
        persist:
            ``True``, or the names of outputs that the store holds in place of the
            workspace (see :meth:`~ess.apps.protocol.WorkspaceProtocol.submit`).

        Raises
        ------
        SubmitError
            If a request or ``persist`` is rejected (see
            :meth:`~ess.apps.protocol.WorkspaceProtocol.submit`). Then no request
            of the call is submitted. The ``request_key`` of each problem is the
            request's key or index.
        """
        stage = None
        if each is not None and (params is not None or not isinstance(what, Stage)):
            raise TypeError('each goes with a stage, in place of params')
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params=params)
        elif isinstance(what, Stage):
            template = what.template
            stage = what.id
            what = (
                template.fill(params=params)
                if each is None
                else _filled(template, each)
            )
        elif isinstance(what, Accumulator):
            raise TypeError(
                'an accumulator is not submitted: read it with client.output, '
                'or freeze it and reference the record of the freeze'
            )
        elif params is not None:
            raise TypeError('params go with a spec or a stage')
        if isinstance(what, Mapping):
            if member is not None:
                raise TypeError('the keys of a dict are the members')
            members = [str(k) if label is not None else None for k in what]
        else:
            members = [member] * len(_items(what))
        entries = [
            Entry(request, label=label, member=m, stage=stage)
            for request, m in zip(_items(what), members, strict=True)
        ]
        names = _names(what)
        try:
            records = self._workspace.submit(entries, self.submitter, persist=persist)
        except SubmitError as error:
            raise SubmitError(_named(e, names) for e in error.errors) from None
        return _reshape(what, records)

    def resolve(self, what: _Items[Request | Template]) -> Any:
        """
        Each request or template as its record would hold it, in the shape of
        ``what``.

        Logs nothing. A rejected item is the ``SubmitError`` that :meth:`submit`
        raises for it, with the item's key or index as ``request_key``. See
        :meth:`~ess.apps.protocol.WorkspaceProtocol.resolve`.
        """
        names = _names(what)
        resolved = self._workspace.resolve(_items(what))
        return _reshape(
            what,
            [
                SubmitError(_named(e, names) for e in r.errors)
                if isinstance(r, SubmitError)
                else r
                for r in resolved
            ],
        )

    @_typed_as(submit)
    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """
        :meth:`submit`, then :meth:`wait`. Returns the records.

        A failed record is returned, not raised.
        """
        records = self.submit(*args, **kwargs)
        self.wait(records)
        return records

    @_shaped
    def status(self, records: list[Record]) -> list[Status]:
        """The status of each record now: pending, completed, failed, or cancelled."""
        return [s.status for s in self._statuses(records)]

    @_shaped
    def wait(self, records: list[Record]) -> list[Status]:
        """
        The status of each record once all have finished.

        A failure is not raised. Raises :class:`~ess.apps.errors.Stopped` if the
        workspace closes meanwhile.
        """
        ids = [r.id for r in records]
        # The position comes first, so that no finish after it is missed.
        position = self._position
        current = self._workspace.status(ids)
        statuses = {i: s.status for i, s in zip(ids, current, strict=True)}
        pending = {i for i, s in statuses.items() if s is Status.PENDING}
        while pending:
            changes = self._workspace.changes(position, timeout=_WAIT)
            position = self._position = changes.cursor
            # Only the records that changed are asked again, so that waiting for
            # many records costs time in proportion to their changes.
            if changed := [i for i in changes.records if i in pending]:
                current = self._workspace.status(changed)
                for i, s in zip(changed, current, strict=True):
                    statuses[i] = s.status
                    if s.status is not Status.PENDING:
                        pending.remove(i)
        return [statuses[i] for i in ids]

    @_shaped
    def failure(self, records: list[Record]) -> list[str | None]:
        """
        Why each record failed, or why the workspace cancelled it.

        See :class:`~ess.apps.RecordStatus`.
        """
        return [s.failure for s in self._statuses(records)]

    def _statuses(self, records: list[Record]) -> list[RecordStatus]:
        return self._workspace.status([r.id for r in records])

    def cancel(self, records: _Items[Record]) -> None:
        """See :meth:`~ess.apps.protocol.WorkspaceProtocol.cancel`."""
        self._workspace.cancel([r.id for r in _items(records)])

    # Stages, accumulators, and releases

    def stage(self, template: Template) -> Stage:
        """
        Opens a stage of the template.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.open_stage`.
        """
        stage_id, resolved = self._workspace.open_stage(template)
        return Stage(resolved, stage_id)

    def accumulator(self, template: Template) -> Accumulator:
        """
        Opens an accumulator of the template.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.open_accumulator`.
        """
        accumulator_id, resolved = self._workspace.open_accumulator(template)
        push = functools.partial(self._workspace.push, accumulator_id)
        return Accumulator(resolved, accumulator_id, push)

    def freeze(
        self, accumulator: Accumulator, *, persist: bool | Sequence[str] = False
    ) -> Record:
        """
        The record of the accumulator's logged state.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.freeze`.
        """
        return self._workspace.freeze(accumulator.id, self.submitter, persist=persist)

    def release[T: Record | Stage | Accumulator](self, what: _Items[T]) -> None:
        """
        Releases records, stages, or accumulators: one, a list, or a dict.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.release`.
        """
        self._workspace.release([x.id for x in _items(what)])

    # Reading

    @overload
    def output(
        self,
        what: Record | Accumulator,
        name: None = None,
        *,
        select: Selection | None = None,
    ) -> dict[str, Any]: ...

    @overload
    def output(
        self,
        what: Record | Accumulator,
        name: str,
        *,
        select: Selection | None = None,
    ) -> Any: ...

    def output(
        self,
        what: Record | Accumulator,
        name: str | None = None,
        *,
        select: Selection | None = None,
    ) -> Any:
        """
        The value of the output ``name``, or every output returned, by name.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.outputs` for a record and
        :meth:`~ess.apps.protocol.WorkspaceProtocol.accumulator_outputs` for an
        accumulator.

        Parameters
        ----------
        select:
            A part of the output: an index or a slice for each dimension it names,
            such as ``{'q': slice(0, 10)}``.
        """
        names = None if name is None else [name]
        if isinstance(what, Accumulator):
            values = self._workspace.accumulator_outputs(what.id, names, select=select)
        else:
            values = self._workspace.outputs(what.id, names, select=select)
        return values if name is None else values[name]

    def describe(self, record: Record, name: str) -> dict[str, Any]:
        """
        What an output is, without its data::

            client.describe(record, 'image')['counts'].sizes  # {'y': 200, 'x': 200}

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.describe`.
        """
        return self._workspace.describe(record.id, name)

    @_shaped
    def readable(self, records: list[Record]) -> list[tuple[str, ...]]:
        """
        The outputs of each record that may be read now::

            for record, names in zip(records, client.readable(records)):
                ...  # offer record.ref(name) for each name

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.readable`.
        """
        return self._workspace.readable([r.id for r in records])

    @_shaped
    def bound(self, records: list[Record]) -> list[bool]:
        """
        Whether each record is of the bound spec::

            for record, bound in zip(records, client.bound(records)):
                spec = client.spec(record.spec) if bound else None

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.bound`.
        """
        return self._workspace.bound([r.id for r in records])

    def records(
        self, *, label: str | None = None, ids: Sequence[str] | None = None
    ) -> list[Record]:
        """
        The proposal's records.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.records`. The newest
        record of each member is
        ``{r.member: r for r in client.records(label=...)}``.
        """
        return self._workspace.records(label=label, ids=ids)

    def changes(self, *, after: int = 0, timeout: float = 0.0) -> Changes:
        """
        The IDs of the records that changed after position ``after``, and the
        position to pass next.

        See :meth:`~ess.apps.protocol.WorkspaceProtocol.changes`, and the loop in
        ``docs/developer/README.md``, "What changed".
        """
        return self._workspace.changes(after, timeout=timeout)

    def latest(self, label: str, member: str | None = None) -> Record:
        """
        The newest record under a label, of ``member`` if given, pending or not.

        Raises ``KeyError`` if there is none.
        """
        matching = [
            r for r in self.records(label=label) if member is None or r.member == member
        ]
        if not matching:
            raise KeyError(f'no record under {label!r}, member {member!r}')
        return matching[-1]

    def provenance(self, what: Record | Accumulator) -> Provenance:
        """
        Where a record, or the logged state of an accumulator, came from.

        The provenance of an accumulator's logged state is that of the record
        of its plain request. Returns at once, without waiting for the logged
        pushes to be added.
        """
        if isinstance(what, Accumulator):
            request = self._workspace.plain_request(what.id)
        else:
            request = what.request
        upstream: dict[str, Record] = {}
        layer = [request]
        while layer:
            ids = {
                ref.record: None
                for r in layer
                for ref in r.upstream()
                if ref.record not in upstream
            }
            found = self._workspace.records(ids=list(ids))
            upstream |= {r.id: r for r in found}
            layer = [r.request for r in found]
        return Provenance(request=request, upstream=tuple(upstream.values()))

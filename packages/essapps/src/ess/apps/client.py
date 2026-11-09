# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a controller calls.

A client talks to one backend for one proposal, through
:class:`~ess.apps.protocol.BackendProtocol`, which holds the rules behind the calls.
The client only adapts: calls that take records or requests accept one, a list, or
a dict, and return the same shape, it wraps IDs in objects, and it adds shortcuts.
:meth:`~Client.cancel` and :meth:`~Client.release` accept the same and return nothing.

See :class:`~ess.apps.protocol.BackendProtocol` for which calls wait.
:meth:`Client.wait`, :meth:`Client.compute`, and :meth:`Files.watch` also wait, and so
does :meth:`Client.close` if ``on_close`` does. A GUI application makes the others
from its UI thread.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, Self, cast, overload

from pydantic import BaseModel

from ess.spec import FileRef, OutputRef, WorkflowSpec

from .errors import FieldError, SubmitError
from .files import Selector
from .protocol import BackendProtocol, ClientId, Entry, Selection
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
"""What a call on several items takes: one item, a list, or a dict."""


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
    The template filled by each item of ``each``, in its shape. Raises
    ``SubmitError`` for the items refused, each reason naming its item.
    """
    requests = []
    errors: list[FieldError] = []
    for name, item in zip(_names(each), _items(each), strict=True):
        try:
            requests.append(template.fill(params=item))
        except SubmitError as error:  # noqa: PERF203
            errors += [replace(e, entry=name) for e in error.errors]
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
    """Gives a function that forwards its arguments to ``method`` the same type."""

    def typed(forwarding: Callable[..., Any]) -> F:
        return cast('F', forwarding)

    return typed


def _named(error: FieldError, names: list[str | int | None]) -> FieldError:
    """The reason with the index of its request replaced by what the caller named."""
    if isinstance(error.entry, int):
        return replace(error, entry=names[error.entry])
    return error


class Provenance(BaseModel, frozen=True):
    """
    Where a record came from: what it ran, what it read, and their files.

    ``request`` is the record's request, or for the state of an accumulator,
    the plain request over the rows pushed so far. ``upstream`` holds every
    record read through all references, directly or through other records,
    nearest first; where a value is an output of an accumulator's state, the
    record listed is the record of that state. It stops at files.
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
    The files of the backend's file source that the proposal may read.

    Forms and controllers find files here: they list them, wait for new ones,
    and read their metadata. A selector picks files by metadata (see
    :class:`~ess.apps.files.Selector`). A request names a file with ``file(...)``.
    """

    def __init__(self, backend: BackendProtocol, client_id: ClientId) -> None:
        self._backend = backend
        self._id = client_id

    @_shaped
    def metadata(self, refs: list[FileRef]) -> list[dict[str, Any]]:
        """
        The metadata of each file, in the shape of ``refs``; see
        :meth:`~ess.apps.protocol.BackendProtocol.file_metadata`.
        """
        return self._backend.file_metadata(refs, self._id)

    def list(self, selector: Selector) -> list[FileRef]:
        """See :meth:`~ess.apps.protocol.BackendProtocol.files`."""
        return self._backend.files(selector, self._id)

    def watch(self, selector: Selector, *, interval: float = 1.0) -> Iterator[FileRef]:
        """
        Matching files: the existing ones first, then new ones, each once. It never
        ends, and lists the files again every ``interval`` seconds while none is new.
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
    A template whose fixed part the backend computes once; a call fills its blanks.

    A stage never changes what a record says: a call through it makes the
    record of the filled template. ``id`` names the stage in the backend.
    """

    template: Template
    id: str


class Accumulator:
    """
    A template whose blanks, tables, are filled one push at a time.

    Its outputs are those of the plain request over the rows pushed so far. The
    client that opened it reads them with :meth:`Client.output`, or references
    them in a request through :meth:`ref` and :meth:`refs`. See
    :meth:`~ess.apps.protocol.BackendProtocol.open_accumulator`.

    ``template`` holds the other values as the backend accepted them,
    ``outputs`` the output names the spec declares, and ``id`` names the
    accumulator in the backend.
    """

    def __init__(
        self,
        template: Template,
        accumulator_id: str,
        outputs: tuple[str, ...],
        push: Callable[[dict[str, list[Any]]], None],
    ) -> None:
        self.template = template
        self.id = accumulator_id
        self.outputs = outputs
        self._push = push

    def push(self, **rows: Sequence[Any]) -> None:
        """
        Add rows to blank tables of the template, the rows entering one state::

            acc.push(sample=[file(run=611), file(run=612)])
            acc.push(sample=[file(run=615)], can=[file(run=614)])

        Parameters
        ----------
        **rows:
            For each table, a list of rows, each given as :meth:`Client.submit`
            takes a value of that type. Rows of different tables are not paired.

        Raises
        ------
        TypeError
            If the rows of a table are not a list.
        SubmitError
            See :meth:`~ess.apps.protocol.BackendProtocol.push`.
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

    def ref(self, output: str, key: str | None = None) -> OutputRef:
        """
        A reference to an output of the accumulator, or to its element ``key`` if the
        output is a list or a dict, for a request.

        Submitting the request pins the state after the rows pushed so far, so the
        request waits until the accumulator has reached it, and is refused if the
        state's plain request would be refused. Raises ``KeyError`` for an output the
        spec lacks.
        """
        if output not in self.outputs:
            raise KeyError(f'{self.template.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output, key=key)

    def refs(self, *outputs: str) -> dict[str, OutputRef]:
        """A reference to each of ``outputs`` by name; to every output if none."""
        return {name: self.ref(name) for name in outputs or self.outputs}


class Client:
    """
    A connection to a backend, for one proposal.

    ``with client:`` closes the client at the end of the block. Any call of a client
    that has ended raises ``ClientEnded``, except :meth:`close`, which then ends
    nothing further.

    Parameters
    ----------
    backend:
        The backend.
    proposal:
        The proposal of the client.
    submitter:
        Who submits, as records name them.
    on_close:
        Called by each :meth:`close`, also if ending the client raised.
    """

    def __init__(
        self,
        backend: BackendProtocol,
        *,
        proposal: str,
        submitter: str,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._backend = backend
        self._on_close = on_close
        self._id = backend.open_client(proposal, submitter)
        self._position = 0  # in the proposal's feed, for wait
        self.proposal = proposal
        self.submitter = submitter
        self.files = Files(backend, self._id)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """
        End the client: release everything it keeps, which cancels the work that
        nothing else keeps.
        """
        try:
            self._backend.close_client(self._id)
        finally:
            if self._on_close is not None:
                self._on_close()

    # Specs

    def spec(self, spec_id: SpecId) -> WorkflowSpec[Any, Any]:
        """
        The spec the backend runs as ``spec_id``, such as ``record.spec``;
        raises ``KeyError`` for one it does not run.
        """
        for spec in self.specs():
            if SpecId.of(spec) == spec_id:
                return spec
        raise KeyError(f'unknown spec {spec_id}')

    def specs(self) -> list[WorkflowSpec[Any, Any]]:
        """
        The specs the backend runs, one for each binding, in the order bound.

        A form builds itself from these and :meth:`spec`, so it needs no specs
        of its own.
        """
        return self._backend.specs(self._id)

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
        Submit a spec or a stage with values, or requests.

        Returns the records, pending, in the shape of ``what``, once the backend has
        logged the requests. A request never references a record of its own call.

        Parameters
        ----------
        what:
            A spec (or its ``SpecId``) with ``params``; a :class:`Stage` with the
            values of its blanks in ``params``; or requests, one, a list, or a dict.
            An accumulator is not submitted: a request reads it through
            :meth:`Accumulator.ref`.
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
            Names one part of the sequence, such as one sample. It is refused with a
            dict. Records are found by label and member with :meth:`records` and
            :meth:`latest`. Neither changes the request or its result.
        persist:
            ``True``, or the names of outputs: the store keeps these outputs instead
            of the client (see :meth:`~ess.apps.protocol.BackendProtocol.submit`).

        Raises
        ------
        SubmitError
            If a request cannot run: an invalid value, an unknown spec or file, a
            reference at a field that the spec does not mark ``Referenced[...]``, a
            reference the client may not read, or one whose data do not fit its
            field. No request of the call is submitted. Each of its ``errors`` names
            the field at fault by its position and the request by its key or index
            (see :class:`~ess.apps.errors.FieldError`).
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
                'an accumulator is read by referencing it in a request: '
                'accumulator.ref(output)'
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
            records = self._backend.submit(entries, self._id, persist=persist)
        except SubmitError as error:
            raise SubmitError(_named(e, names) for e in error.errors) from None
        return _reshape(what, records)

    @_typed_as(submit)
    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """
        :meth:`submit`, then :meth:`wait`, and return the records, which a
        failure leaves failed, not raised.
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
        The status of each record once all have finished; a failure is not raised. If
        the client ends meanwhile, raises ``ClientEnded``.
        """
        while True:
            # The position comes first, so that no finish after it is missed.
            position = self._position
            statuses = [s.status for s in self._statuses(records)]
            if Status.PENDING not in statuses:
                return statuses
            changes = self._backend.changes(position, self._id, timeout=_WAIT)
            self._position = changes.cursor

    @_shaped
    def failure(self, records: list[Record]) -> list[str | None]:
        """
        Why each record failed, or why the backend cancelled it; see
        :class:`~ess.apps.RecordStatus`.
        """
        return [s.failure for s in self._statuses(records)]

    def _statuses(self, records: list[Record]) -> list[RecordStatus]:
        return self._backend.status([r.id for r in records], self._id)

    def cancel(self, records: _Items[Record]) -> None:
        """See :meth:`~ess.apps.protocol.BackendProtocol.cancel`."""
        self._backend.cancel([r.id for r in _items(records)], self._id)

    # Stages, accumulators, and what the client keeps

    def stage(self, template: Template) -> Stage:
        """
        A stage of the template; see
        :meth:`~ess.apps.protocol.BackendProtocol.open_stage`.
        """
        stage_id, resolved = self._backend.open_stage(template, client_id=self._id)
        return Stage(resolved, stage_id)

    def accumulator(self, template: Template) -> Accumulator:
        """
        An accumulator of the template; see
        :meth:`~ess.apps.protocol.BackendProtocol.open_accumulator`.
        """
        accumulator_id, resolved = self._backend.open_accumulator(
            template, client_id=self._id
        )
        outputs = tuple(self.spec(resolved.spec).outputs.model_fields)
        push = functools.partial(self._backend.push, accumulator_id, client_id=self._id)
        return Accumulator(resolved, accumulator_id, outputs, push)

    def freeze(
        self, accumulator: Accumulator, *, persist: bool | Sequence[str] = False
    ) -> Record:
        """
        The record of the accumulator's last state; see
        :meth:`~ess.apps.protocol.BackendProtocol.freeze`.
        """
        return self._backend.freeze(accumulator.id, self._id, persist=persist)

    def release[T: Record | Stage | Accumulator](self, what: _Items[T]) -> None:
        """
        Release records, stages, or accumulators: one, a list, or a dict. See
        :meth:`~ess.apps.protocol.BackendProtocol.release`.
        """
        self._backend.release([x.id for x in _items(what)], client_id=self._id)

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
        The value of an output, or every output returned, by name, if ``name`` is
        None. See :meth:`~ess.apps.protocol.BackendProtocol.outputs` for a record and
        :meth:`~ess.apps.protocol.BackendProtocol.accumulator_outputs` for an
        accumulator, for what is read and what is raised.

        Parameters
        ----------
        what:
            A record or an accumulator.
        name:
            The output to read.
        select:
            Picks part of the named array output by dimension name, an index or a
            slice for each dimension it names, such as ``{'q': slice(0, 10)}``.
        """
        names = None if name is None else [name]
        if isinstance(what, Accumulator):
            values = self._backend.accumulator_outputs(
                what.id, names, self._id, select=select
            )
        else:
            values = self._backend.outputs(what.id, names, self._id, select=select)
        return values if name is None else values[name]

    def describe(self, record: Record, name: str) -> dict[str, Any]:
        """
        What an output is, without its data; takes a record, not an accumulator::

            client.describe(record, 'image')['counts'].sizes  # {'y': 200, 'x': 200}

        See :meth:`~ess.apps.protocol.BackendProtocol.describe`.
        """
        return self._backend.describe(record.id, name, self._id)

    @_shaped
    def readable(self, records: list[Record]) -> list[tuple[str, ...]]:
        """
        The outputs of each record, by name, that a plain submission of this client
        may reference now. A picker of records calls it at each refresh::

            for record, names in zip(records, client.readable(records)):
                ...  # offer record.ref(name) for each name

        See :meth:`~ess.apps.protocol.BackendProtocol.readable`.
        """
        return self._backend.readable([r.id for r in records], self._id)

    def records(
        self, *, label: str | None = None, ids: Sequence[str] | None = None
    ) -> list[Record]:
        """
        The proposal's records; see :meth:`~ess.apps.protocol.BackendProtocol.records`.

        Each record has its ``member``, so
        ``{r.member: r for r in client.records(label=...)}`` is the newest of each
        member.
        """
        return self._backend.records(self._id, label=label, ids=ids)

    def changes(self, *, after: int = 0, timeout: float = 0.0) -> Changes:
        """
        The IDs of the proposal's records that changed since the position ``after``,
        and the position to pass next. An application stays up to date by reading
        what it shows of them::

            cursor = 0
            while True:
                changes = client.changes(after=cursor, timeout=30)
                cursor = changes.cursor
                for record in client.records(ids=changes.records):
                    ...

        See :meth:`~ess.apps.protocol.BackendProtocol.changes`.
        """
        return self._backend.changes(after, self._id, timeout=timeout)

    def latest(self, label: str, member: str | None = None) -> Record:
        """
        The newest record under a label, of ``member`` if given, pending or
        not; raises ``KeyError`` if there is none.
        """
        matching = [
            r for r in self.records(label=label) if member is None or r.member == member
        ]
        if not matching:
            raise KeyError(f'no record under {label!r}, member {member!r}')
        return matching[-1]

    def provenance(self, what: Record | Accumulator) -> Provenance:
        """
        Where a record, or the current state of an accumulator, came from.

        An accumulator's state is the plain request over the rows pushed so
        far, so its provenance is that of the record of this request. It pins
        the state, but returns at once, without waiting for the pushes to be
        added.
        """
        if isinstance(what, Accumulator):
            request = self._backend.plain_request(what.id, self._id)
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
            found = self._backend.records(self._id, ids=list(ids))
            upstream |= {r.id: r for r in found}
            layer = [r.request for r in found]
        return Provenance(request=request, upstream=tuple(upstream.values()))

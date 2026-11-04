# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a controller calls.

A client talks to one backend for one proposal. Calls that take records or
requests accept one, a list, or a dict, and return the same shape;
:meth:`~Client.cancel`, :meth:`~Client.persist`, and :meth:`~Client.release`
accept the same and return nothing.

A call that starts work returns once the backend has checked and logged it:
:meth:`Client.submit`, :meth:`Client.accumulator`, :meth:`Accumulator.push`,
and :meth:`Client.freeze`. It leaves reading data and computing to the
backend's workers. Only the calls that read results wait for that work:
:meth:`Client.wait`, :meth:`Client.compute`, and :meth:`Client.output`; and
:meth:`Files.watch` waits for new files. So a GUI application can make
every other call from its UI thread.

The rules behind the calls are in ``docs/developer/``: ``lifetimes.md`` for
what a client keeps and may read, ``accumulators.md`` for stages and
accumulators. ``packages/essspec/docs/params.md`` defines the terms of a
request's params.
"""

from __future__ import annotations

import functools
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel

from ess.spec import Binding, FileRef, OutputRef, WorkflowSpec

from .backend import Backend, Entry, Selection
from .files import FileSource, Selector
from .records import Record, Request, SpecId, Template, as_values
from .store import Store


def _items(what: Any) -> list[Any]:
    """The items of one item, a list, a tuple, or a dict."""
    if isinstance(what, Mapping):
        return list(what.values())
    if isinstance(what, list | tuple):
        return list(what)
    return [what]


def _reshape(what: Any, items: list[Any]) -> Any:
    """``items`` in the shape of ``what``."""
    if isinstance(what, Mapping):
        return dict(zip(what, items, strict=True))
    if isinstance(what, list | tuple):
        return type(what)(items)
    return items[0]


def _names(what: Any) -> list[str | int | None]:
    """How each item of ``what`` is named in errors: its key or its index."""
    if isinstance(what, Mapping):
        return [str(key) for key in what]
    if isinstance(what, list | tuple):
        return list(range(len(what)))
    return [None]


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
    and read their metadata, from the backend's file source (see
    :class:`~ess.apps.files.FileSource`). A client sees only the files its
    proposal may read. A selector picks files by metadata (see
    :class:`~ess.apps.files.Selector`). A request names a file with
    ``file(...)``.
    """

    def __init__(self, backend: Backend, client: str) -> None:
        self._backend = backend
        self._client = client

    def list(self, selector: Selector) -> list[FileRef]:
        """The files the selector matches, in the order they were measured."""
        return self._backend.files(selector, self._client)

    def watch(self, selector: Selector) -> Iterator[FileRef]:
        """Matching files: the existing ones first, then new ones, each once."""
        return self._backend.watch_files(selector, self._client)

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        """The file's current metadata; raises ``KeyError`` if not readable."""
        return self._backend.file_metadata(ref, self._client)


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
    them in a request through :meth:`ref` and :meth:`refs`. The rules are in
    ``docs/developer/accumulators.md``.

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

        Returns once the push is logged. The backend adds it later.

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
            If a table is not a blank of the template, a table gets no rows, a row
            is invalid, repeats, or is in its table already, a row references a
            record that failed or was cancelled, or the accumulator has stopped.
            The reason of a stopped accumulator names the step, such as ``push 0``.
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

        Submitting the request pins the state after the rows pushed so far (see
        ``docs/developer/accumulators.md``). The request waits until the accumulator
        has reached the state, and is refused if the state's plain request would be
        refused. Raises ``KeyError`` for an output the spec lacks.
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

    ``with client:`` closes the client at the end of the block. A client that
    owns its backend, as :func:`local` makes one, also closes the backend.
    Any call of a client that has ended raises ``ClientEnded``, except
    :meth:`close`, which then does nothing.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        proposal: str,
        submitter: str,
        owns_backend: bool = False,
    ) -> None:
        self._backend = backend
        self._owns_backend = owns_backend
        self._id = backend.open_client(proposal, submitter)
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

        It first waits until the outputs it asked to persist with :meth:`persist`
        are written, and raises ``RuntimeError`` naming each whose write failed,
        once it has ended. A client that owns its backend then closes it, which waits
        for the work that still runs. A client that is never closed ends with its
        process.
        """
        try:
            self._backend.close_client(self._id)
        finally:
            if self._owns_backend:
                self._backend.close()

    # Specs

    def spec(self, spec_id: SpecId) -> WorkflowSpec[Any, Any]:
        """
        The spec the backend runs as ``spec_id``, such as ``record.spec``;
        raises ``KeyError`` for one it does not run.
        """
        return self._backend.spec(spec_id, self._id)

    def specs(self) -> list[WorkflowSpec[Any, Any]]:
        """
        The specs the backend runs, one for each binding, in the order bound.

        A form builds itself from these and :meth:`spec`, so it needs no specs
        of its own.
        """
        return self._backend.specs(self._id)

    # Submitting

    def submit(
        self,
        what: Any,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
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
        label:
            Names a sequence of records, such as all I(Q) reductions of an
            experiment. Under a label, the keys of a dict become the members.
        member:
            Names one part of the sequence, such as one sample. It is refused with a
            dict. Records are found by label and member with :meth:`records` and
            :meth:`latest`. Neither changes the request or its result.
        persist:
            ``True``, or the names of outputs: the client does not keep these
            records, the store keeps the outputs named, and each record completes
            once they are written (see ``docs/developer/lifetimes.md``).

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
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params=params)
        elif isinstance(what, Stage):
            stage, what = what.id, what.template.fill(params=params)
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
            Entry(request, label=label, member=m, name=name, stage=stage)
            for request, m, name in zip(
                _items(what), members, _names(what), strict=True
            )
        ]
        records = self._backend.submit(entries, client=self._id, persist=persist)
        return _reshape(what, records)

    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """
        :meth:`submit`, then :meth:`wait`, and return the records, which a
        failure leaves failed, not raised.
        """
        records = self.submit(*args, **kwargs)
        self.wait(records)
        return records

    def status(self, records: Any) -> Any:
        """The status of each record now: pending, completed, failed, or cancelled."""
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.status(ids, self._id))

    def wait(self, records: Any) -> Any:
        """The status of each record once all have finished; a failure is not raised."""
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.wait(ids, self._id))

    def failure(self, records: Any) -> Any:
        """
        Why each record failed, or why the backend cancelled it; ``None`` for a
        record pending, completed, or cancelled by a client.

        The reason is the framework's own words, such as ``specimens[0]: Value error,
        ...``, ``the workflow of sans-iofq/v1 returned None as iofq``, or ``the write
        failed: ...``. An error raised by a binding, its driver, the file source, or
        the store is given with its type, such as ``KeyError: 'counts'``.
        """
        failures = [self._backend.failure(r.id, self._id) for r in _items(records)]
        return _reshape(records, failures)

    def cancel(self, records: Any) -> None:
        """
        End the unfinished records as cancelled.

        A client may cancel any record of its proposal. See
        ``docs/developer/lifetimes.md`` for what cancelling passes along.
        """
        self._backend.cancel([r.id for r in _items(records)], self._id)

    # Stages, accumulators, and what the client keeps

    def stage(self, template: Template) -> Stage:
        """
        A stage of the template; the client keeps it until it releases it.

        The spec's binding must make stages, as a ``PipelineBinding`` does. A template
        that a request would refuse is refused. The stage's template holds the values
        as the backend resolved them. The rules are in
        ``docs/developer/accumulators.md``.
        """
        stage_id, resolved = self._backend.open_stage(template, client=self._id)
        return Stage(resolved, stage_id)

    def accumulator(self, template: Template) -> Accumulator:
        """
        An accumulator of the template; the client keeps it until it releases it.

        The template's blanks are one or more of the spec's tables. The spec's binding
        must make accumulators, as a ``DriverBinding`` does. A template that a request
        would refuse is refused, with the blanks left out, and so is one that
        references an accumulator. Returns once the template is checked; the backend
        makes the driver later. The rules are in ``docs/developer/accumulators.md``.
        """
        accumulator_id, resolved = self._backend.open_accumulator(
            template, client=self._id
        )
        outputs = tuple(self.spec(resolved.spec).outputs.model_fields)
        push = functools.partial(self._backend.push, accumulator_id, client=self._id)
        return Accumulator(resolved, accumulator_id, outputs, push)

    def freeze(
        self, accumulator: Accumulator, *, persist: bool | Sequence[str] = False
    ) -> Record:
        """
        The record of the accumulator's last state; the client keeps it, and the
        accumulator takes no more pushes.

        Parameters
        ----------
        accumulator:
            The accumulator to freeze.
        persist:
            As in :meth:`submit`: the store keeps the outputs instead of the client.

        Raises
        ------
        SubmitError
            If the plain request of the state would be refused.
        """
        return self._backend.freeze(accumulator.id, client=self._id, persist=persist)

    def persist(self, records: Any, *outputs: str) -> None:
        """
        Write the named outputs, or every output, of records the client keeps to the
        store, each once its record has completed. Returns at once.

        Raises ``SubmitError``, and persists nothing, for a record the client does
        not keep, a failed or cancelled one, or an output a record lacks. See
        ``docs/developer/lifetimes.md`` for what happens if a write fails.
        """
        ids = [r.id for r in _items(records)]
        self._backend.persist(ids, outputs, client=self._id)

    def release(self, what: Any) -> None:
        """
        Release records, stages, or accumulators: one, a list, or a dict.

        Releasing what the client does not keep does nothing. See
        ``docs/developer/lifetimes.md`` for what a release drops and cancels, and
        ``docs/developer/accumulators.md`` for a released accumulator.
        """
        self._backend.release([x.id for x in _items(what)], client=self._id)

    # Reading

    def output(
        self,
        what: Record | Accumulator,
        name: str | None = None,
        *,
        select: Selection | None = None,
    ) -> Any:
        """
        The value of an output, or every output returned, by name, if ``name`` is
        None; an optional output that was not returned is left out.

        A record's value is the one the workflow returned, not a copy: the caller
        must not change it, and the framework never does. An accumulator's are
        those of its state after the pushes so far, once they are added, copied. The
        read makes no record.

        Parameters
        ----------
        what:
            A record or an accumulator.
        name:
            The output to read.
        select:
            Picks part of the named array output by dimension name, an index or a
            slice for each dimension it names, such as ``{'q': slice(0, 10)}``, and
            copies only that part. Of a group output, it picks that part of each
            array field that has the dimension, as ``sc.DataGroup`` slices its items,
            and copies the other fields whole. It needs ``name``.

        Raises
        ------
        NotReadable
            For a record the client neither keeps nor has persisted, or whose write
            failed or whose value the store no longer holds; for an accumulator
            released or stopped, with nothing pushed, or whose state's plain request
            would be refused.
        RecordFailed
            For a record that failed or was cancelled.
        ValueError
            If ``select`` is given without ``name``, for an output that is neither
            an array nor a group with an array field, or if no array field of a group
            has a dimension of ``select``. A dimension that an array lacks, or an
            index out of range, raises what the slice raises.
        """
        names = None if name is None else [name]
        if isinstance(what, Accumulator):
            values = self._backend.accumulator_outputs(
                what.id, names, self._id, select=select
            )
        else:
            values = self._backend.outputs(what.id, names, self._id, select=select)
        return values if name is None else values[name]

    def readable(self, records: Any) -> Any:
        """
        The outputs of each record, by name, that a plain submission of this client
        may reference now (see ``docs/developer/lifetimes.md``).

        It waits for nothing and reads no value, so a picker of records calls it at
        each refresh::

            for record, names in zip(records, client.readable(records)):
                ...  # offer record.ref(name) for each name

        A record that failed or was cancelled has no outputs, and one that has
        completed only those its workflow returned.
        """
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.readable(ids, self._id))

    def records(self, *, label: str | None = None) -> list[Record]:
        """
        The proposal's records, oldest first, under ``label`` if given;
        every record of the proposal without it. Each record has its
        ``member``, so ``{r.member: r for r in client.records(label=...)}``
        is the newest of each member.
        """
        return self._backend.records(self._id, label=label)

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
        todo = deque([request])
        while todo:
            for ref in todo.popleft().upstream():
                if ref.record not in upstream:
                    upstream[ref.record] = self._backend.record(ref.record, self._id)
                    todo.append(upstream[ref.record].request)
        return Provenance(request=request, upstream=tuple(upstream.values()))


def local(
    *,
    proposal: str,
    files: FileSource,
    bind: Sequence[Binding],
    submitter: str = 'user',
    store: Store | None = None,
) -> Client:
    """
    A client of its own backend in this process, running the workflows bound here.

    ``bind`` lists a binding for each spec the backend runs, such as
    ``[PipelineBinding(IOFQ, ...), DriverBinding(IOFQ_MULTI, ...)]``. Since the
    code runs in this process, a binding may also be of code defined in a
    notebook, such as ``function_binding(IOFQ, iofq)``.

    Closing the client closes the backend, which waits until what is persisted
    is written. Without a ``store``, persisting is refused. The backend keeps
    history in memory, so its records end with the process.
    """
    return Client(
        Backend(files, bind, store=store),
        proposal=proposal,
        submitter=submitter,
        owns_backend=True,
    )

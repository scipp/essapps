# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The interfaces between a client and a workspace, and between a workspace and
its file source and store.

:class:`WorkspaceProtocol` is what a client calls. :class:`FileSource` and
:class:`Store` are what a workspace calls. The protocol holds every rule that a
client relies on. A client only adapts it: it reshapes arguments, wraps IDs in
objects, and adds shortcuts.

The guides are ``docs/developer/README.md`` for requests, records and
references, and ``docs/developer/accumulators.md`` for stages and accumulators.

Lifetimes
---------
A record stays in the proposal's history. A deployment may drop a history that
no person holds, such as that of automatic reduction, once its proposal has
been idle for days to weeks
(``docs/developer/adr/0004-history-is-append-only-lists.md``).

An output value may be large. It is available only while one of these holds it:

- the workspace, for a record whose outputs are not persisted, until the record
  is released or the workspace closes;
- a pending request that references it, until the request's workflow returns;
- a push, a stage, or an accumulator's template that references it, until the
  binding has received it, or until the push, stage, or accumulator is
  released, stopped, or dropped;
- the store, for a persisted output, until the proposal's history is dropped.

A record object or a reference holds nothing. A value that is gone does not
come back. An output is written to the store only if a submission or a freeze
persists it, since the framework cannot tell which outputs are worth writing.

A request may reference, and a client may read, only outputs that are held, and
files. A client may also read the states of open accumulators. Every client of
the workspace reads the same values. A request may reference an output of a
pending record. :meth:`~WorkspaceProtocol.submit` rejects the reference once
that record has failed or been cancelled.

A pending record whose outputs nothing would hold finishes as cancelled. A
cancelled request no longer holds what it references, so the cancellation
passes along a chain. A running workflow is not interrupted: its record is
cancelled at once, and its outputs are dropped when the workflow returns.

Bound and unbound specs
-----------------------
A record names its spec by name, version, and digest
(:attr:`ess.spec.WorkflowSpec.digest`). The record is *of the bound spec* if
the workspace has a binding with the same three, and otherwise *of an unbound
spec*, such as after a version bump or a change of the spec's models. A record
of an unbound spec stays in history, and a client reads its persisted outputs
as stored (see :meth:`Store.read`). A request may not reference these outputs,
since their types cannot be checked
(``docs/developer/adr/0009-a-record-of-another-spec-stays-readable.md``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ess.spec import FileRef, WorkflowSpec

from .errors import SubmitError
from .files import Selector
from .records import Changes, Record, RecordStatus, Request, Template

Selection = Mapping[str, int | slice]
"""Part of an array: an index or a slice for each dimension it names."""


@dataclass(frozen=True)
class Entry:
    """
    A request of a submission, with its label and member.

    ``label`` names a sequence of records, and ``member`` one part of it.
    Neither changes the result. ``stage`` is the ID of the stage that the
    request goes through, if any.
    """

    request: Request
    label: str | None = None
    member: str | None = None
    stage: str | None = None


class WorkspaceProtocol(Protocol):
    """
    The calls a client makes on the workspace of one proposal.

    A record or accumulator ID that the history lacks raises ``KeyError``.
    Inside a request, a template, or a push, it raises ``SubmitError``.
    :meth:`release` ignores it.

    Once the workspace closes (:meth:`ess.apps.Workspace.close`), every call
    raises :class:`~ess.apps.errors.Stopped`, also a call that is waiting when
    it closes. If an append to the workspace's log fails, the call that appends
    raises that error, and every later call raises ``Stopped``.

    A call that logs, such as :meth:`submit`, returns once it is checked and
    logged. Reading data and computing are left to the workers. Only
    :meth:`changes`, up to its timeout, and the reads of values, :meth:`outputs`,
    :meth:`describe` and :meth:`accumulator_outputs`, wait for work to finish.

    The *plain request* of a call through a stage, or of a state of an
    accumulator, is the same request without the stage or accumulator. The
    record of such a call, and of a freeze, is the record of its plain request.
    """

    @property
    def proposal(self) -> str:
        """The proposal of the workspace."""
        ...

    # Specs

    def specs(self) -> list[WorkflowSpec[Any, Any]]:
        """The specs the workspace runs, in the order bound."""
        ...

    # Requests

    def submit(
        self,
        entries: list[Entry],
        submitter: str,
        *,
        persist: bool | Sequence[str] = False,
    ) -> list[Record]:
        """
        Logs a pending record for each entry and returns the records, in order.

        Every request is checked before any is logged. If one is rejected, none
        is logged. A record waits until the records it references have
        completed. If one of them fails or is cancelled, the record fails, with
        a reason that names it.

        Parameters
        ----------
        submitter:
            Who submits, as the records name them.
        persist:
            ``True``, or the names of outputs to write to the store. The store
            holds these outputs in place of the workspace, and the other
            outputs are dropped. Each record completes once they are written,
            and fails if a write fails.

        Raises
        ------
        SubmitError
            If a request cannot run, such as for an invalid value, an unknown
            or unreadable file, or a reference that may not be read or does not
            fit its field. Also if ``persist`` names an output the spec lacks,
            or the workspace has no store. The ``request_key`` of each problem
            is the index in ``entries``, or ``None`` if the problem is not
            about one request.
        """
        ...

    def resolve(
        self, items: list[Request | Template]
    ) -> list[Request | Template | SubmitError]:
        """
        Returns each item as its record would hold it, and logs nothing.

        A request comes back with file names resolved to identities, defaults
        filled in, and inline values in JSON's types. A template comes back
        with the same blanks, and with each other parameter as the record of a
        request filled from it would hold it. The template of a stage (see
        :meth:`open_stage`) differs: it holds only the values given, since a
        call through the stage must have exactly these.

        An item that :meth:`submit` would reject comes back as the
        ``SubmitError`` in its place, with its index in ``items`` as
        ``request_key``. A template is also rejected for a parameter that is
        neither given nor a blank and has no default. Each item is resolved on
        its own, so a rejected item does not hide the others.

        ``resolve`` does not check whether referenced outputs may be read,
        stages, ``persist``, or anything that shows only when the workflow
        runs, such as a file that cannot be read. So :meth:`submit` may still
        reject a request that ``resolve`` accepts.

        Resolving a resolved request returns the same request, and
        ``resolve([record.request]) == [record.request]`` for a record of the
        bound spec. A file that gains a catalogue identifier later gets another
        identity.
        So a request that names it by run number then resolves to another value
        than an older record holds. ``docs/developer/README.md``,
        "Requests and records", shows how to compare a resolved request with a
        record's.
        """
        ...

    def cancel(self, ids: Sequence[str]) -> None:
        """
        Ends the unfinished records as cancelled.

        What only these records hold is cancelled too, and a record that
        references one of them fails.
        """
        ...

    def status(self, ids: Sequence[str]) -> list[RecordStatus]:
        """The status of each record now, and why it failed, if it did."""
        ...

    # History

    def records(
        self,
        *,
        label: str | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[Record]:
        """
        The proposal's records, oldest first.

        With ``label``, only the records under it. With ``ids``, the records
        with these IDs, in that order. Raises ``ValueError`` if both are given.
        """
        ...

    def changes(self, after: int, *, timeout: float = 0.0) -> Changes:
        """
        The IDs of the records that changed after position ``after`` of the
        proposal's feed, and the position to pass next.

        A record enters the feed when it is submitted and when it finishes. The
        client holds its position, so ``after=0`` replays the proposal from its
        start. A reply holds each ID once, in the order of its first change in
        the range. It holds no values: a record's status only moves forward, so
        a client reads the current status of what changed.

        Accumulators, pushes, and releases are not in the feed. A client learns
        that a value is gone from :meth:`readable`, or when a read raises
        ``NotReadable``.

        Parameters
        ----------
        timeout:
            How long to wait for a change after ``after``, in seconds. If none
            comes, the reply holds no IDs.

        Raises
        ------
        ValueError
            If ``after`` is negative or past the position of the last change.
        """
        ...

    def plain_request(self, accumulator_id: str) -> Request:
        """
        The plain request of the accumulator's logged state.

        Raises ``NotReadable`` if the state may not be read.
        """
        ...

    # Values

    def outputs(
        self,
        record_id: str,
        names: Sequence[str] | None,
        *,
        select: Selection | None = None,
    ) -> dict[str, Any]:
        """
        Values of a record's outputs by name, once the record has completed.

        A persisted output is read from the store. Without ``select``, a value
        is the object the workflow returned, not a copy, and the caller must not
        change it. An output of a record of an unbound spec is read as stored
        (see :meth:`Store.read`).

        Parameters
        ----------
        names:
            The outputs to read, or ``None`` for every output that may be read
            and that the workflow returned.
        select:
            Copies only this part of each output. Requires ``names``. Each
            output must be declared as an array, or as a group with an array
            field. Of a group, it slices each array field that has the
            dimension, as ``sc.DataGroup`` slices its items, and copies the
            other fields whole. An output read as stored is checked by slicing
            it, and a ``DataGroup`` is sliced as a group.

        Raises
        ------
        KeyError
            For an output the spec lacks.
        NotReadable
            If an output may not be read, also once its record is released
            while something else still holds the value. Also if the store no
            longer holds an output, or the workflow did not return it.
        RecordFailed
            If the record failed or was cancelled, also when releasing it
            cancelled it.
        ValueError
            If ``select`` is given without ``names``, for an output that
            declares no array, or if no array field of a group has a dimension
            of ``select``. A dimension that an array lacks, or an index out of
            range, raises what the slice raises.
        """
        ...

    def describe(self, record_id: str, name: str) -> dict[str, Any]:
        """
        What an output is, without its data.

        A group returns its fields by name. Any other output returns
        ``{name: info}``. An array of at least one dimension is an
        :class:`~ess.apps.ArrayInfo`, a group or a dict is a dict, a list is a
        list of these, and any other value is a copy. An output of a record of
        an unbound spec is read as stored, so any output except an array
        returns the fields of its ``DataGroup``, such as ``{'value': 0.5}`` of a
        number. Reads and raises as :meth:`outputs` does.
        """
        ...

    def bound(self, ids: Sequence[str]) -> list[bool]:
        """
        Whether each record is of the bound spec. Reads no value.
        """
        ...

    def readable(self, ids: Sequence[str]) -> list[tuple[str, ...]]:
        """
        The outputs of each record that may be read now, in declared order.

        These are the outputs that :meth:`outputs` returns once the record has
        completed. A request may reference them, except the outputs of a record
        of an unbound spec. Reads no value.
        """
        ...

    def release(self, ids: Sequence[str]) -> None:
        """
        Releases records, stages, and accumulators. Other IDs are ignored.

        A released record stays in history. Its outputs are dropped once
        nothing else holds them. A released stage accepts no more calls. Calls
        already made through it still run, and the stage releases what its
        template references if it has not staged yet. A released accumulator
        accepts no more pushes or reads. The pushes that a read in progress or
        the record of a freeze needs are still added, and later pushes never
        are.
        """
        ...

    # Stages and accumulators

    def open_stage(self, template: Template) -> tuple[str, Template]:
        """
        Opens a stage, and returns its ID and its template.

        The returned template holds the values as the stage holds them, with
        file names resolved. A request through the stage (see :class:`Entry`)
        must have these values. If the binding fails to make the stage, the
        stage stops: calls already made through it fail with the reason, and
        later calls are rejected with it.

        Raises ``SubmitError`` if the spec's binding makes no stages, or if
        :meth:`submit` would reject a fixed value.
        """
        ...

    def open_accumulator(self, template: Template) -> tuple[str, Template]:
        """
        Opens an accumulator, and returns its ID and its template as history
        holds it.

        ``docs/developer/accumulators.md`` defines states and the logged state.

        The accumulator stops if the binding fails to make the driver, if
        adding a push fails, or if a record that its template or a push
        references does not complete. Pushes are then rejected and reads raise
        ``NotReadable``, each with a reason that names the failed step. The
        record of a freeze of a state that the accumulator did not reach fails,
        and so do the requests that read it.

        Raises ``SubmitError`` if the spec's binding makes no accumulators, if
        the blanks are not tables of the spec, or if :meth:`submit` would reject
        a fixed value.
        """
        ...

    def push(self, accumulator_id: str, rows: Mapping[str, list[Any]]) -> None:
        """
        Logs rows of one or more blank tables, by table name.

        The rows of one push enter one state. A push waits while a freeze is
        being checked. It does not wait for a pending record, or for the push to
        be added. Pushes are added in the order logged.

        Raises ``SubmitError`` if a table is not a blank of the template or has
        no rows, if a row is invalid, repeats, or is in its table already, if a
        row references an output that may not be read or an accumulator, or if
        the accumulator is released, frozen, or stopped.
        """
        ...

    def freeze(
        self,
        accumulator_id: str,
        submitter: str,
        *,
        persist: bool | Sequence[str] = False,
    ) -> Record:
        """
        The record of the accumulator's logged state.

        The state is the logged state once no other freeze of the accumulator
        is being checked. A push that waited for that check may be logged
        first. The record's outputs are the driver's, not a copy. The workspace holds
        them, or with ``persist`` the store does. ``submitter`` and ``persist``
        are as for :meth:`submit`.

        The accumulator accepts no more pushes. Once the record has completed,
        the accumulator is read through it. If the record fails or is
        cancelled, the accumulator may be read and frozen again.

        Raises ``SubmitError`` if the accumulator is released or stopped, if a
        freeze of it is pending or has completed, or if :meth:`submit` would
        reject the state's plain request. The accumulator then accepts pushes
        again.
        """
        ...

    def accumulator_outputs(
        self,
        accumulator_id: str,
        names: Sequence[str] | None,
        *,
        select: Selection | None = None,
    ) -> dict[str, Any]:
        """
        Copies of outputs of the accumulator's logged state at the time of the
        call, once the accumulator has reached that state.

        Makes no record. ``names`` and ``select`` are as for :meth:`outputs`.

        Raises ``NotReadable`` if the accumulator is released, stopped, or read
        through the record of its freeze, if :meth:`submit` would reject the
        state's plain request, if the driver fails to return outputs, or for an
        output it did not return. Raises ``KeyError`` for an output the spec
        lacks.
        """
        ...

    # Files

    def files(self, selector: Selector) -> list[FileRef]:
        """The matching files the proposal may read, in the order measured."""
        ...

    def file_metadata(self, refs: Sequence[FileRef]) -> list[dict[str, Any]]:
        """
        The current metadata of each file, named by its identity.

        Raises ``KeyError`` for a file that is unknown or that the proposal may
        not read.
        """
        ...


class FileSource(Protocol):
    """
    Which files exist, and their names, paths, and metadata.

    A workspace has one file source. A client sees only the files that the
    workspace's proposal may read. Tests use ``testing.FakeFiles``.

    The workspace and the forms decide which files fit a field or a selector
    only by the metadata that :meth:`metadata` and :meth:`list` return. A
    source that corrects the metadata of a file, such as the role of a wrongly
    tagged run, must correct it in both. A role is optional. At a raw file field
    the workspace does not check it (see :meth:`Selector.of`).

    A source that cannot answer, such as when a mount is gone or a catalogue
    timed out, raises ``OSError``. If :meth:`resolve`, :meth:`metadata` or
    :meth:`path` raises it for a file that a request names,
    :meth:`WorkspaceProtocol.submit` raises ``SubmitError`` for that request,
    with the reason ``file ... cannot be read``.
    """

    def resolve(self, name: FileRef) -> FileRef:
        """
        The identity of the named file. Raises ``KeyError`` if unknown.

        An identity names its own file, so it resolves to itself. A file that
        the catalogue lists is identified by its catalogue identifier,
        ``pid:20.500.12269/<uuid>``, the same in every file source that knows
        the file. Any other file is identified by its path (see
        :class:`~.files.FolderFiles`). An identity is not a path: only
        :meth:`path` returns the path of a file.
        """
        ...

    def path(self, ref: FileRef) -> Path:
        """
        A local path to the file, for a workflow to read.

        Raises ``KeyError`` for an identity the source does not know, such as
        one that a record holds of a file moved or given a ``pid`` since.

        The workspace calls ``path`` when it checks a request or a template,
        for each file of a parameter that holds no reference, so that the
        validators of a group that holds a file see its path. So ``path``
        neither stages nor downloads the file, and does not check that it can
        be read. A file that cannot be read fails the workflow that reads
        it, so that it fails only its own record.
        """
        ...

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        """
        The file's current metadata.

        The field ``proposal`` names the proposal whose requests may read the
        file. A file without it, such as published data, is open to all.
        """
        ...

    def list(self, selector: Selector) -> list[FileRef]:
        """The files the selector matches, in the order they were measured."""
        ...


class Store(Protocol):
    """
    Where persisted outputs are written, and read back.

    Each deployment configures its own store. Tests use ``testing.FakeStore``.
    A value is found by the record's ID and the output's name, so history names
    no file. The format of each output type and the layout of the store are the
    store's own. A store may hold values that no history names, such as those of
    a record cancelled while it was written. Nothing reads them, and they are
    deleted with the proposal's area.
    """

    def write(self, record: str, output: str, value: Any) -> None:
        """Writes the value of an output. Raises if it cannot."""
        ...

    def read(self, record: str, output: str, tp: Any | None) -> Any:
        """
        The value of an output that was written.

        ``tp`` is the type that the spec declares for the output, by which a
        store whose format holds no types makes the value. ``None`` reads the
        value as stored, in scipp's types. The workspace reads an output of a
        record of an unbound spec this way, since it lacks the spec's classes.
        Every store returns this form of a value as stored::

            as the workflow returned it          as stored
            ---------------------------          ---------
            a scipp array                        the array
            a group (a pydantic model)           a DataGroup of its fields
            dict[str, X]                         a DataGroup of its items
            list[X]                              a DataGroup by index, '0', '1', ...
            None, as a field or a dict value     left out
            a member of an enum                  its value
            a number, text, or a bool alone      DataGroup({'value': ...})

        with a value inside a ``DataGroup`` in the same form.

        Raises ``LookupError`` if the store no longer holds it, for example
        once dropped to free disk space, or holds a value that does not fit
        ``tp``, as a file changed or damaged outside the workspace would: a
        client's read of it then raises ``NotReadable``, as for a value that
        nothing holds, and a request that reads it fails.
        """
        ...

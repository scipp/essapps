# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The seams of a workspace: :class:`WorkspaceProtocol` is what a client calls,
:class:`FileSource` and :class:`Store` are what a workspace calls.

The protocol holds the rules that a client relies on. Anything that changes
what happens belongs here, and the client only adapts: it reshapes, wraps IDs
in objects, and adds shortcuts.

Records and values
------------------
A workspace holds the history and the values of one proposal; its clients are
connections to it, and keep nothing.

Records and output values have different lifetimes. A record is small and is
kept with the proposal's history. History is dropped as a whole once the
proposal has been idle for days to weeks: no call and no pending record. An
output value may be large and lives only while a *keeper* keeps it:

- the workspace, for a record that a submission or a freeze made without
  persisting its outputs, until the record is released or the workspace closes;
- a pending request that reads it, until the request's workflow has returned;
- a push, a stage, or an accumulator's template that references it, until the
  binding has taken it, or the push, stage, or accumulator is released, stopped,
  or dropped;
- the store, for an output a submission or a freeze persists, until the
  proposal's history is dropped.

A record object or a reference keeps nothing. A value that is gone does not come
back. Nothing is written to the store unless a submission or a freeze persists
it, since the framework cannot tell which outputs are worth writing.

A request may reference, and a client may read, only the output values the
workspace keeps, persisted outputs, and files; a client may also read the states
of its open accumulators. A request never references an accumulator, only
the record of its freeze. Every client of the workspace reads the same values. A
pending record passes this rule, so a request is not refused because what it reads
is still running. It is refused once that record has failed or been cancelled.

Work that nothing would keep is cancelled: a pending record whose outputs have
no keeper finishes as cancelled. Since a cancelled request keeps nothing it
reads, this passes along a chain. A running workflow is not interrupted: its
record is cancelled at once, and its outputs are dropped when it returns.

In the user's process, values live in its memory. A hosted service runs a
workspace for each proposal, limits its memory, and gives it a store.
``docs/developer/accumulators.md`` explains stages and accumulators, and
``packages/essspec/docs/params.md`` the terms of params.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ess.spec import FileRef, WorkflowSpec

from .files import Selector
from .records import Changes, Record, RecordStatus, Request, Template

Selection = Mapping[str, int | slice]
"""Part of an array: an index or a slice for each dimension it names."""


@dataclass(frozen=True)
class Entry:
    """
    A request of a submission, and its label and member.

    ``label`` names a sequence of records, and ``member`` one part of it; neither
    changes the result. ``stage`` names the stage the request goes through, if any.
    """

    request: Request
    label: str | None = None
    member: str | None = None
    stage: str | None = None


class WorkspaceProtocol(Protocol):
    """
    What a client calls on a workspace of one proposal.

    Every record and accumulator of the workspace's history is of its proposal. A
    record or accumulator ID that the history lacks raises ``KeyError``; inside a
    request, template, or push it raises ``SubmitError``, and :meth:`release` leaves
    it alone. Once the workspace closes (:meth:`ess.apps.Workspace.close`), every
    later call raises ``RuntimeError``, and so does a call waiting when it closes.
    If the workspace cannot log, the call that found this raises that error, and it
    stops: every call then raises ``RuntimeError``.

    A call returns once it is checked and logged, and leaves reading data and
    computing to the workers. Only :meth:`changes`, up to its timeout, and the reads
    of values, :meth:`outputs`, :meth:`describe`, and :meth:`accumulator_outputs`,
    wait for work to finish.

    A *plain request* is a request that goes through no stage: a call through a
    stage, and every state of an accumulator, has one, and its record is the record
    of that request.
    """

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
        Check every request, then log a pending record for each, in order.

        If one request is refused, none is submitted. A record waits until the records
        it references have completed. If one fails or is cancelled, the record fails,
        with a reason that names it.

        Parameters
        ----------
        entries:
            The requests.
        submitter:
            Who submits, as the records name them.
        persist:
            ``True``, or the names of outputs: the store keeps these outputs instead
            of the workspace, and the other outputs are dropped. Each record completes
            once they are written, and fails if a write fails.

        Returns
        -------
        :
            The records, in the order of ``entries``.

        Raises
        ------
        SubmitError
            If a request cannot run: an invalid value, an unknown spec or file, a
            reference at a field that the spec does not mark ``Referenced[...]``, a
            reference that may not be read, to an accumulator, or one whose data do
            not fit its field; or if ``persist`` names an output the spec lacks, or
            the workspace has no store. A reason's ``entry`` is the request's index in
            ``entries``, or ``None`` if the reason is not about one request (see
            :class:`~ess.apps.errors.FieldError`).
        """
        ...

    def cancel(self, ids: Sequence[str]) -> None:
        """
        End the unfinished records as cancelled, and pass this along: what only they
        keep is cancelled, and a record that references one fails.
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
        The proposal's records, oldest first, under ``label`` if given; those with
        the IDs ``ids``, in that order, if given. Raises ``ValueError`` if both are.
        """
        ...

    def changes(self, after: int, *, timeout: float = 0.0) -> Changes:
        """
        The IDs of the proposal's records that changed since position ``after`` of
        its feed, and the position to pass next.

        This is how a client stays up to date. The feed is one order, a record is in
        it when it is submitted and when it finishes, and the client holds its own
        position, so ``after=0`` replays the proposal from its start. A reply
        holds each ID once, in the order of its first change in the range. It holds no
        values: a record's status only moves forward, so a client reads the current
        status of what changed.

        Accumulators, pushes, and releases are not in the feed. A value that has gone
        is found by asking :meth:`readable`, and reading it raises ``NotReadable``.

        Parameters
        ----------
        after:
            The position to read from.
        timeout:
            How long to wait, in seconds, for a change after ``after``. If none comes,
            the reply holds no IDs.

        Raises
        ------
        ValueError
            If ``after`` is negative or past the position of the last change.
        """
        ...

    def plain_request(self, accumulator_id: str) -> Request:
        """
        The plain request of the accumulator's logged state (see
        :meth:`open_accumulator`). Returns at once. Raises ``NotReadable`` if the
        state may not be read.
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
        Values of a record's outputs by name, once it has completed; a persisted output
        is read from the store.

        A value is the one the workflow returned, not a copy, unless ``select`` is
        given: the caller must not change it.

        Parameters
        ----------
        record_id:
            The record.
        names:
            The outputs to read, or ``None`` for every output that may be read and that
            the workflow returned.
        select:
            Copies only this part of each output. It needs ``names``, and each output
            must be declared as an array or a group with an array field. Of a group, it
            picks that part of each array field with the dimension, as ``sc.DataGroup``
            slices its items, and copies the other fields whole.

        Raises
        ------
        KeyError
            For an output the spec lacks.
        NotReadable
            If an output may not be read, also once its record is released while
            something else still keeps it, or if the store no longer holds an output
            or the workflow did not return it.
        RecordFailed
            If the record failed or was cancelled, also when releasing it cancelled it.
        ValueError
            If ``select`` is given without ``names``, for an output that takes no
            selection, or if no array field of a group has a dimension of ``select``.
            A dimension that an array lacks, or an index out of range, raises what the
            slice raises.
        """
        ...

    def describe(self, record_id: str, name: str) -> dict[str, Any]:
        """
        What an output is, without its data. A group gives its fields by name, any
        other output gives ``{name: info}``. An array of at least one dimension is an
        :class:`~ess.apps.ArrayInfo`, a group or a dict is a dict, a list is a list of
        these, and anything else is a copy. Reads and raises as :meth:`outputs` does.
        """
        ...

    def readable(self, ids: Sequence[str]) -> list[tuple[str, ...]]:
        """
        The outputs of each record that a request may reference now, in declared
        order. Waits for nothing and reads no value.
        """
        ...

    def release(self, ids: Sequence[str]) -> None:
        """
        Let go of records, stages, and accumulators the workspace keeps; others are
        left alone.

        A released record stays in history, and its outputs are dropped once nothing
        else keeps them. A released stage takes no more requests, those already made
        through it still run, and it lets go of what its template references if it has
        not staged. A released accumulator takes no more pushes or readers: the pushes
        that a read in progress or the record of a freeze needs are still added, and
        later ones never are.
        """
        ...

    # Stages and accumulators

    def open_stage(self, template: Template) -> tuple[str, Template]:
        """
        Open a stage. Returns its ID and the template with its values as
        the stage holds them, file names resolved.

        A request through it (see :class:`Entry`) must have these values, and its
        record is the record of the plain request. If making the binding's stage fails,
        the stage stops: requests already made through it fail with the reason, and
        later ones are refused with it.

        Raises ``SubmitError`` if the spec's binding makes no stages, or a fixed value
        would be refused in a request.
        """
        ...

    def open_accumulator(self, template: Template) -> tuple[str, Template]:
        """
        Open an accumulator. Returns its ID and the template as history
        holds it.

        Its *state n* is the accumulator after its first n pushes, and has the outputs
        of its plain request: the template with each table filled by the rows of these
        pushes. Its *logged state* is state n for the n pushes logged so far, whether
        or not they have been added. A read is of the logged state when it is called. A
        freeze is of the logged state once no other freeze of the accumulator is being
        checked; a push that waited for that check may be logged first. Adding push
        n+1 waits until the reads of state n in progress are done, so no reader sees a
        value change. A request never references an accumulator, only the record of its
        freeze.

        The accumulator stops if the binding fails to make the driver, adding a push
        fails, or a record its template or a push references does not complete. Pushes
        are then refused, and reads raise ``NotReadable``, each with the reason naming
        the step. The record of a freeze of a state it did not reach fails, and so do
        the requests that read it.

        Raises ``SubmitError`` if the spec's binding makes no accumulators, the blanks
        are not tables of the spec, or a fixed value would be refused in a request.
        """
        ...

    def push(self, accumulator_id: str, rows: Mapping[str, list[Any]]) -> None:
        """
        Log rows of one or more blank tables by name; they enter one state.

        It waits while a freeze is being checked, and otherwise does not wait for a
        record that is pending or for the push to be added. Pushes are added in the
        order logged.

        Raises ``SubmitError`` if a table is not a blank of the template or gets no
        rows, a row is invalid, repeats, or is in its table already, a row references
        an output that may not be read or an accumulator, or the accumulator is
        released, frozen, or stopped.
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
        The record of the accumulator's logged state (see :meth:`open_accumulator`),
        whose outputs are the driver's, not a copy; the workspace keeps it, or with
        ``persist``, the store (see :meth:`submit`). ``submitter`` is as for
        :meth:`submit`.

        The accumulator takes no more pushes. Once the record has completed, the
        accumulator is read through it. If it fails or is cancelled, the accumulator
        may be read and frozen again.

        Raises ``SubmitError`` if the accumulator is released or stopped, a freeze of it
        is pending or has completed, or the state's plain request would be refused; the
        accumulator then takes pushes again.
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
        Copies of outputs of the accumulator's logged state when it is called, or of
        the part ``select`` names (see :meth:`outputs`), once it has reached the
        state. It makes no record.

        Raises ``NotReadable`` for an accumulator released, stopped, or read through the
        record of its freeze, a state whose plain request would be refused, a driver
        that fails to give outputs, or an output it did not return; ``KeyError`` for an
        output the spec lacks.
        """
        ...

    # Files

    def files(self, selector: Selector) -> list[FileRef]:
        """The matching files the proposal may read, in the order measured."""
        ...

    def file_metadata(self, refs: Sequence[FileRef]) -> list[dict[str, Any]]:
        """
        The current metadata of each file, named by its identity as :meth:`files` gives
        it; raises ``KeyError`` for one unknown or that the proposal may not read.
        """
        ...


class FileSource(Protocol):
    """
    Which files exist, and what they hold.

    A workspace has one. It resolves names and finds files through it, and answers
    through it the queries of its clients: listing files and reading their metadata.
    A client sees only the files the workspace's proposal may read. Tests use a fake
    (``testing.FakeFiles``).

    The workspace, its rules, and the forms tell which files fit a field or a selector
    only by the metadata the source gives, in :meth:`metadata` and :meth:`list`. A
    source that corrects the metadata of a file, such as the role of a run tagged
    wrongly, corrects them in both, so that each sees the file as corrected.
    A role is optional, and what the workspace does not check (:meth:`Selector.of`).
    """

    def resolve(self, name: FileRef) -> FileRef:
        """The identity of the named file; raises ``KeyError`` if unknown."""
        ...

    def path(self, ref: FileRef) -> Path:
        """
        A local path to the file, for a workflow to read: a lookup by
        identity that never raises.

        The files are on a network mount, and the workspace calls ``path`` for
        each file of a request when the request is submitted, so that the
        validators of a group that holds a file see its path. So ``path``
        neither stages nor downloads the file, and does not check that it can
        be read: a file that cannot be read fails the workflow that reads it,
        so that one unreadable file fails its own record, not a batch or a
        rule's submission that holds it.
        """
        ...

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        """
        The file's current metadata.

        The field ``proposal`` names the proposal whose requests may read the
        file; a file without it, such as published data, is open to all.
        """
        ...

    def list(self, selector: Selector) -> list[FileRef]:
        """The files the selector matches, in the order they were measured."""
        ...


class Store(Protocol):
    """
    Where persisted outputs are written, and read back.

    The workspace is given a store; each deployment configures its own, and tests
    use a fake (``testing.FakeStore``). A value is found by the record's ID and
    the output's name, so history names no file. A store reads a value back as
    the workflow returned it. The format of each output type and the layout of
    the store are its own. A store may hold values that no history names, such
    as those of a record cancelled while it was written; nothing reads them, and
    they go with the proposal's area.
    """

    def write(self, record: str, output: str, value: Any) -> None:
        """Write the value of an output; raises if it cannot."""
        ...

    def read(self, record: str, output: str, tp: Any) -> Any:
        """
        The value of an output that was written. ``tp`` is the type that the
        spec declares for the output, by which a store whose format holds no
        types makes the value. Raises ``LookupError`` if the
        store no longer holds it, for example once dropped to free disk space,
        or holds a value that does not fit ``tp``, written under other outputs
        of the spec:
        a client's read of it then raises ``NotReadable``, as for a value that
        nothing keeps, and a request that reads it fails.
        """
        ...

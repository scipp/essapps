# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Reduce many runs as batches, on a workspace folder.

A batch is a template and a batch table, kept in the folder under a label.
Each row of the batch table is one reduction, such as of one sample run; the
template holds the settings that the rows share.

The options before the command name the folder, the bindings (the reductions
that the folder runs) and the row sources (which make rows from the files):

    B="essapps-batch -w FOLDER -b package.module:BINDINGS -s package.module:SOURCES"

A first session, in this order; each command has its own --help:

    cd FOLDER; essapps-batch files package.module:read --with runs.xlsx -o files.csv
                                  # the files of the folder; --with adds the role
                                  # and sample of each run from a run table
    $B specs                      # the reductions: name, version, title, outputs
    $B template SPEC -o t.yaml    # a template of SPEC: what each parameter takes
    $B sources                    # the row sources: what each fills, and how
    $B rows SOURCE                # the rows that SOURCE makes now; KEY is column 1
    $B reduce t.yaml --source SOURCE -l water --first KEY
                                  # make the batch water of the rows there are now,
                                  # from the row KEY on (or --last KEY, or all), and
                                  # reduce them; --later: none now; it does not follow
    $B reduce t.yaml runs.csv -l water   # or of a table file of your own
    $B batches                    # the batches, with how many rows are reduced
    $B show -l water              # the batch table of water, with each row's status
    $B reduce -l water --remaining       # reduce the rows that are not done
    $B template -l water -o now.yaml     # the settings of water, as a template
    $B export -l water results    # write the outputs of the rows of water
    $B offered -l water           # the rows of the row source that water lacks
    $B take -l water KEY...       # put those rows into water; reduces nothing
    $B follow -l water --steps 3  # add and reduce the rows that arrive: 3 steps,
                                  # or until Ctrl-C without --steps
    $B stop                       # no batch follows
    $B remove -l water            # remove the batch; its records stay

A template is YAML: the spec, and a value for each parameter that the rows
share. A parameter that each row gives in its own column of the batch table (a
parameter column) is left out:

    spec: {name: loki-masks, version: 1}
    params:
      detector: {'@latest': detector, output: detector}
      mask_files: [path:calibration/masks.xml]

Here detector is the output detector of the newest record under the label
detector, and mask_files a list of one file, by its path in files.csv: reduce
the detector first, under a label of its own. The template that "template SPEC"
writes says how to write each kind of value.

reduce on a label without a batch makes the batch: of the rows of --source, of
a table file, or else of one row keyed by the label; on a label with a batch, a
template replaces its settings and a table file its batch table. It reduces
every row that can be, or with --remaining the rows not done. A refused reduce
makes no batch, but a batch keeps the template and table file given to it.

The status of a row:
    ready       no result yet; it can be reduced, unless the settings are refused
    incomplete  it cannot be reduced: a value is missing, such as an empty cell,
                or refused, or a record that it reads failed or was cancelled
    running     submitted, not finished
    done        reduced with the cells and settings it has now
    earlier     reduced, but a setting it takes changed since; --remaining
                leaves it, --earlier-settings adds it
    outdated    reduced, but a cell changed since, or the spec bound is of
                another version, or the settings are refused, so not compared
    failed      the reduction failed; the column failure says why
    cancelled   the reduction was cancelled

follow -l water: water follows its row source, with a step every --interval
seconds (5). A step adds the rows of the source that are filled and not
considered (not one that a person removed or left out), and reduces each row
that is ready and has no result. It never changes a row of the batch table: a
row added with the transmission run that was newest then keeps it when a newer
one arrives (compare rows SOURCE with show). It reduces no failed row again
(reduce --remaining does). A step that did something writes a line, or with
--json an object, with "error" why the step raised (null if it did not):

    14:03:05 added 70011 | reducing 70011 | done 70009 | waiting 70013
    {"time": "2026-11-23T14:03:05+01:00", "added": ["70011"], "reducing": ["70011"],
     "ended": {"70009": "done"}, "failure": {}, "refused": {}, "waiting": ["70013"],
     "error": null}

ended: the status of each row whose reduction ended; failure, refused: why. A
run counts once files.csv lists it: "files ... -o files.csv" brings it up to
date, also while follow runs. Ctrl-C, SIGTERM or the last of --steps ends follow
once the reductions that run are done (exit status 0 or 3), but water follows
until stop: the next follow -l water, or a page, goes on with the rows that
arrived meanwhile. One batch follows at a time; one that stopped can follow
again. A second Ctrl-C ends it at once: the reduction that ran reads failed ("the
workspace ended while it ran"), and the next command that opens the folder,
show too, runs the reductions not started and waits for them.

Exit status:
    0  done; after reduce or follow, every row of the batch is done
    1  an error, such as a folder that another process holds
    2  refused, so that nothing ran: a wrong command line, template or table
       file, or a row that cannot be reduced
    3  reduce or follow left a row not done, such as one that failed
    130  follow ended at once by a second SIGINT (143: SIGTERM, 129: SIGHUP)

One process works on a folder at a time: the command refuses a folder that
another process holds, such as a page or a follow. Messages go to stderr.
"""

# The commands are thin: the rules are those of BatchManager, Intake and Batch,
# as the page applies them, so a label is one batch whoever works on it. Only
# reduce, take and the steps of follow change a batch table. A command is a
# function of _Given and an entry of _COMMANDS. The guide is
# packages/essapps/docs/batch.md, section
# "Table files and the command".

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import FrameType
from typing import Any, Literal

import numpy as np
import pandas as pd

from ess.spec import Binding

from ..client import Client
from ..errors import Stopped, SubmitError, said
from ..files import replace_at_once
from ..folder import (
    ArrivingFiles,
    described,
    folder_files,
    open_folder,
    scan_folder,
)
from ..records import Record, Status
from . import frames
from .corrections import CorrectedFiles
from .definition import FolderDefinitions, NotKept
from .export import export_table
from .intake import Intake, not_registered, step_failure
from .manager import BatchManager
from .reduction import Batch
from .source import Range, RowSource
from .table import KEY, SUFFIXES, read_table, read_text, write_table
from .templates import checked_settings, load_template, settings_text, template_to_fill


class _Wrong(Exception):
    """A wrong command line, said as argparse says one: exit status 2."""


class _Held(Exception):
    """The folder is held by another process: exit status 1."""


def _named(name: str) -> Any:
    """The object that ``package.module:NAME`` names; ``_Wrong`` if none."""
    module, colon, attribute = name.partition(':')
    if not (module and colon and attribute):
        raise _Wrong(f'{name}: name it as package.module:NAME')
    try:
        found = import_module(module)
    except Exception as error:  # any error of the module's code, too
        raise _Wrong(
            f'{name}: cannot import {module}: {type(error).__name__}: {error}'
        ) from None
    if not hasattr(found, attribute):
        raise _Wrong(f'{name}: the module {module} has no {attribute}')
    return getattr(found, attribute)


def _bindings(name: str) -> list[Binding[Any, Any]]:
    """The bindings that ``name`` names; ``_Wrong`` if it names something else."""
    bind = _named(name)
    if isinstance(bind, str | bytes) or not (
        isinstance(bind, Sequence) and all(isinstance(b, Binding) for b in bind)
    ):
        raise _Wrong(
            f'{name} is a {type(bind).__name__}, not a list of bindings '
            '(ess.spec.Binding), such as package.module:BINDINGS'
        )
    return list(bind)


def _sources(name: str) -> Mapping[str, RowSource]:
    """The sources by name that ``name`` names; ``_Wrong`` if other things."""
    sources = _named(name)
    if not isinstance(sources, Mapping) or not all(
        isinstance(source, RowSource) for source in sources.values()
    ):
        raise _Wrong(
            f'{name} is a {type(sources).__name__}, not a mapping of names to '
            'row sources (ess.apps.batch.RowSource), such as package.module:SOURCES'
        )
    return sources


def _existing(text: str) -> Path:
    """The path of a file that exists; argparse refuses another."""
    path = Path(text)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f'{text}: no such file')
    return path


@dataclass(frozen=True)
class _Given:
    """
    What a command runs with: its arguments, the row sources by name, and the
    client and definitions of the folder, if the command opens it.
    """

    args: argparse.Namespace
    sources: Mapping[str, RowSource]
    folder: tuple[Client, FolderDefinitions] | None = None

    @property
    def client(self) -> Client:
        if self.folder is None:
            raise RuntimeError('the command does not open the folder')
        return self.folder[0]

    def manager(self, *, staged: bool = False) -> BatchManager:
        """The batches of the folder; their rows run through a stage if ``staged``."""
        if self.folder is None:
            raise RuntimeError('the command does not open the folder')
        client, definitions = self.folder
        return BatchManager(
            client,
            definitions=definitions,
            staged={bound.name for bound in client.specs()} if staged else (),
        )

    def batch(self, label: str) -> Batch:
        """The batch of ``label``; ``ValueError`` if the label has none."""
        manager = self.manager()
        return manager.batch(_known(manager, label))

    def intake(self) -> Intake:
        """The batches of the folder, with the row sources."""
        return Intake(self.manager(), sources=self.sources)


def _known(manager: BatchManager, label: str) -> str:
    """``label``; ``ValueError`` if no batch of ``manager`` has it."""
    if label not in manager.headers():
        raise ValueError(f'{label}: no batch has the label; batches lists them')
    return label


type _Result = pd.DataFrame | str | int
"""
What a command gives: a table or the text of a template, to write; or the exit
status of a command that said what it did as it ran.
"""


def _files(given: _Given) -> pd.DataFrame:
    """The files table of the folder (``files.csv``)."""
    args = given.args
    read = _named(args.read)
    if not callable(read):
        raise _Wrong(
            f'{args.read} is a {type(read).__name__}, not a function from the '
            'path of a file to its metadata'
        )
    if args.run_table is None and args.output is not None and args.output.exists():
        raise _Wrong(f'{args.output} exists, and may hold what a person added')
    files = pd.DataFrame(scan_folder(args.workspace, read), dtype=str).fillna('')
    if args.run_table is None:
        return files
    return described(files, read_text(args.run_table))


def _names(given: _Given, names: Iterable[str]) -> list[str] | str:
    """``names`` as a list for JSON, else as text, separated by a space."""
    return list(names) if given.args.json else ' '.join(names)


def _specs(given: _Given) -> pd.DataFrame:
    """A row per spec bound: name, version, title, and the names of its outputs."""
    specs = [
        [spec.name, spec.version, spec.title, _names(given, spec.outputs.model_fields)]
        for spec in given.client.specs()
    ]
    return pd.DataFrame(specs, columns=['name', 'version', 'title', 'outputs'])


def _template(given: _Given) -> str:
    """A template of a spec to fill in, or the settings of a batch."""
    args = given.args
    if args.label is not None:
        return settings_text(given.batch(args.label))
    try:
        spec = given.client.spec_named(args.spec)
    except KeyError as error:
        raise ValueError(f'{error.args[0]}; specs lists them') from None
    return template_to_fill(spec, given.client.specs(), given.sources)


def _row_sources(given: _Given) -> pd.DataFrame:
    """A row per row source: its name, what it fills, and its description."""
    sources = [
        [name, _names(given, source.fills), source.description]
        for name, source in given.sources.items()
    ]
    return pd.DataFrame(sources, columns=['name', 'fills', 'description'])


def _rows(given: _Given) -> pd.DataFrame:
    """The rows that the row source of the name given makes now."""
    return given.intake().rows_of(given.args.source)


def _batches(given: _Given) -> pd.DataFrame:
    """
    A row per batch: its label, title, spec, number of rows, how many have a
    completed result, the name of its row source, and whether it follows. The
    results are not compared with the rows (``Batch.state(compare=False)``),
    which would resolve every row of every batch: ``show`` tells which are
    done. A batch that cannot be read is said on stderr, and its row has no
    spec and no counts.
    """
    intake = given.intake()
    following = intake.following
    listed = []
    for label, header in intake.manager.headers().items():
        row: dict[str, Any] = {'label': label, 'title': header.title}
        try:
            batch = intake.manager.batch(label)
            states = batch.state(compare=False).values()
            row |= {
                'spec': batch.spec.name,
                'rows': len(states),
                'reduced': sum(s.status is Status.COMPLETED for s in states),
                'source': intake.source(label),
            }
        except ValueError as error:
            sys.stderr.write(f'{label}: {error}\n')
        listed.append(row | {'follows': label == following})
    columns = ['label', 'title', 'spec', 'rows', 'reduced', 'source', 'follows']
    return pd.DataFrame(listed, columns=columns).astype(
        {'rows': 'Int64', 'reduced': 'Int64'}
    )


def _table(client: Client, batch: Batch, keys: list[str] | None = None) -> pd.DataFrame:
    """
    The rows of ``keys`` of ``batch``, or all, with what came of each; why the
    settings are refused, if they are, on stderr.
    """
    states, refused = batch.shown(keys)
    if refused:
        sys.stderr.write(f'the settings are refused: {SubmitError(refused)}\n')
    frame = batch.frame()
    return frames.with_results(
        client, states, frame if keys is None else frame.loc[keys]
    )


def _show(given: _Given) -> pd.DataFrame:
    """The batch table of the label, with what came of each row."""
    return _table(given.client, given.batch(given.args.label))


def _reduce(given: _Given) -> pd.DataFrame:
    """
    Reduce the batch of the label, and return its batch table with what came
    of each row (:func:`.frames.reduce_batch`).

    For a label without a batch, the manager makes the batch
    (:meth:`~.manager.BatchManager.create`) of the template's spec, with the
    label as title; with ``--source``, the intake makes it
    (:meth:`~.intake.Intake.create`). Its batch table is the table file given
    (:func:`.table.read_table`), or the rows that the source ``--source`` has
    filled now, those that ``--first``, ``--last`` and ``--later`` pick
    (:class:`~.source.Range`), not both; without either, one row keyed by the
    label. With ``--source``, the batch keeps its source, so that ``offered``
    and its page offer the rows of the source that its batch table lacks; it
    follows only after ``follow``. ``create`` refuses a label that
    has definitions or records. If the reduction is then refused, the batch
    is removed again: nothing was submitted, and the same command line works
    once the template is right.

    For a label with a batch, the template becomes its settings and the table
    file replaces its batch table (:meth:`.reduction.Batch.put`); ``--source``
    is refused.

    A template must be of the batch's spec and its version, as the page's
    "Load settings" checks it (:func:`.templates.checked_settings`), and is
    checked before anything is written. Its blanks are not read. A value at a
    parameter column is the setting that each row with an empty cell takes,
    but at a column that the batch's source fills, where it is left out
    (:meth:`.reduction.Batch.settable`). With ``--remaining`` it reduces the
    rows that the page's "Reduce" does (:func:`.state.left_to_reduce`);
    ``--earlier-settings`` adds those reduced with earlier settings.
    """
    args, client = given.args, given.client
    label = args.label
    template = None if args.template is None else load_template(args.template)
    manager = given.manager(staged=args.staged)
    batch = manager.batch(label) if label in manager.headers() else None
    if batch is not None and args.source is not None:
        raise ValueError(f'{label}: the batch exists; --source makes a new batch')
    if batch is not None:
        spec = batch.spec
    elif template is None:
        raise ValueError(f'{label}: no batch has the label, so give a template')
    else:
        spec = manager.spec_named(template.spec.name)
    if args.table is not None:
        rows = read_table(args.table, spec.params)
    elif batch is not None:
        rows = batch.frame()
    elif args.source is None:
        rows = pd.DataFrame(index=pd.Index([label], name=KEY))
    else:
        rows = None  # Intake takes them from the source
    settings = None
    if template is not None:
        # Checked before anything is written.
        try:
            settings = checked_settings(client, spec, template)
        except ValueError as error:
            raise ValueError(f'{args.template}: {error}') from None
    made = batch is None
    if batch is not None:
        batch.put(rows)
        if settings is not None:
            batch.settings = batch.settable(settings)
    elif args.source is not None:
        intake = Intake(manager, sources=given.sources)
        batch = intake.create(
            spec.name,
            title=label,
            source=args.source,
            choose=Range(args.first, args.last, later=args.later),
            settings=settings,
            label=label,
        )
    else:
        batch = manager.create(
            spec.name, title=label, rows=rows, settings=settings, label=label
        )
    try:
        return frames.reduce_batch(
            batch, remaining=args.remaining, earlier_settings=args.earlier_settings
        )
    except (SubmitError, ValueError):
        # A refusal submits nothing, so the label is free again, unless a
        # record has the label after all.
        if made and not client.records(label=label):
            with suppress(NotKept):  # the refusal is what to say
                manager.remove(label)
        raise


def _count(number: int, noun: str) -> str:
    """``number`` and ``noun``, plural unless one."""
    return f'{number} {noun}{"" if number == 1 else "s"}'


def _reduced(result: pd.DataFrame) -> int:
    """The exit status of reduce: 3 if a row is not done, saying why if earlier."""
    # Only --remaining without --earlier-settings leaves such rows.
    if left := int((result['status'] == 'earlier').sum()):
        sys.stderr.write(
            f'left {_count(left, "row")} reduced with earlier '
            'settings: add --earlier-settings to reduce them\n'
        )
    return 0 if all(result['status'] == 'done') else 3


def _export(given: _Given) -> pd.DataFrame:
    """Write the outputs of the rows of a table, or of a batch as it stands."""
    args = given.args
    if args.label is not None:
        table = _show(given)
    else:
        table = read_text(args.table)
        if KEY not in table.columns:
            raise ValueError(
                f'{args.table}: no column {KEY}, so reduce did not write it'
            )
        table = table.set_index(KEY)
    return export_table(given.client, table, args.directory)


def _offered(given: _Given) -> pd.DataFrame:
    """The rows of the source of a batch that its batch table lacks, with ``waits``."""
    intake = given.intake()
    return intake.offered(_known(intake.manager, given.args.label))


def _take(given: _Given) -> pd.DataFrame:
    """Put rows of its source into a batch; those rows, as ``show`` writes them."""
    args = given.args
    intake = given.intake()
    intake.take(_known(intake.manager, args.label), args.keys)
    return _table(given.client, intake.manager.batch(args.label), args.keys)


def _remove(given: _Given) -> int:
    """Remove a batch, saying that its records stay; a removal cut off too."""
    label = given.args.label
    try:
        given.manager().remove(label)
    except KeyError:
        raise ValueError(
            f'{label}: no batch has the label; batches lists them'
        ) from None
    kept = len(given.client.records(label=label))
    sys.stderr.write(
        f'removed {label}; '
        + (
            f'the folder keeps its {_count(kept, "record")} under the label, so '
            'a new batch takes another label\n'
            if kept
            else 'it had no records, so the label is free\n'
        )
    )
    return 0


def _stop(given: _Given) -> int:
    """End Follow, saying on which batch."""
    intake = given.intake()
    label = intake.following
    intake.stop()
    sys.stderr.write(
        'no batch followed\n' if label is None else f'{label} follows no more\n'
    )
    return 0


def _follow(given: _Given) -> int:
    """
    Let a batch follow, and run :func:`follow` on it until Ctrl-C or SIGTERM,
    or for ``--steps``; 0 if every row of the batch is then done, else 3.
    """
    args = given.args
    intake = given.intake()
    label = _known(intake.manager, args.label)
    intake.follow(label)
    sys.stderr.write(
        f'{label} follows: a step every {args.interval:g} s'
        f'{"" if args.steps is None else f", {_count(args.steps, "step")}"}\n'
    )
    stop = threading.Event()

    def say(line: Mapping[str, Any]) -> None:
        now = datetime.now(UTC).astimezone()  # local time, with its offset
        line = {'time': now.isoformat(timespec='seconds'), **line}
        text = json.dumps(line, ensure_ascii=False) if args.json else _said(line)
        if text is not None:
            sys.stdout.write(f'{text}\n')
            sys.stdout.flush()

    with _stopped_by_signals(stop):
        follow(
            intake,
            say=say,
            pause=lambda: stop.wait(args.interval),
            stopped=stop.is_set,
            steps=args.steps,
        )
    states, _ = intake.manager.batch(label).shown()
    if left := sum(s.text != 'done' for s in states.values()):
        sys.stderr.write(
            f'{_count(left, "row")} of {label} not done: show -l {label} says why\n'
        )
        return 3
    return 0


@contextmanager
def _stopped_by_signals(stop: threading.Event) -> Iterator[None]:
    """
    While open, the first SIGINT, SIGTERM or SIGHUP sets ``stop``, and the
    second ends the process at once, with 128 plus the number of the signal,
    without waiting for the threads that run reductions.
    """

    # It writes nothing through sys.stderr, which the main thread may be
    # writing to: Python refuses a reentrant call.
    def asked(number: int, frame: FrameType | None) -> None:
        if stop.is_set():
            os.write(2, b'ended at once\n')
            os._exit(128 + number)
        stop.set()

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = {number: signal.signal(number, asked) for number in signals}
    try:
        yield
    finally:
        for number, handler in before.items():
            signal.signal(number, handler)


def follow(
    intake: Intake,
    *,
    say: Callable[[Mapping[str, Any]], None],
    pause: Callable[[], object],
    stopped: Callable[[], bool],
    steps: int | None = None,
) -> None:
    """
    Steps of Follow (:meth:`.intake.Intake.step`) on the batch that follows,
    with ``pause()`` between two, until ``stopped()`` or after ``steps``
    steps; then wait for the reductions that run, and return.

    After a step that did something, ``say`` is given what, as a mapping of
    names to keys::

        added       the rows that the step added, a list of keys
        reducing    the rows that it submitted
        ended       the status of each row whose reduction ended since
                    (RowState.text), by key; the reductions that ran at the
                    first step count too
        failure     why each of those failed or was cancelled, by key
        refused     why each row could not be submitted, by key
        waiting     the rows of the source that are not filled yet

        error       why the step raised, ``None`` if it did not

    A step did something if it added or submitted rows, if a reduction
    ended, if what is refused or waits changed since ``say`` was last called,
    or if it raised something else than the step before. After the wait,
    ``say`` is given the reductions that ended in it.

    A step may raise anything, as the code of the source may
    (:meth:`.intake.Intake.step`). It is said on stderr as the page says it
    (:func:`.intake.step_failure`), once while the message stays the same,
    and the next step tries again, as on the page; but a workspace that
    stopped (:class:`~ess.apps.errors.Stopped`) is raised, since no step can
    work on it.

    Parameters
    ----------
    intake:
        The intake, whose batch follows; ``ValueError`` if none does.
    say:
        Called with what a step did.
    pause:
        Waits between two steps, such as ``event.wait(seconds)``.
    stopped:
        Whether to end, asked before each step.
    steps:
        The number of steps, unbounded if not given.
    """
    if (label := intake.following) is None:
        raise ValueError('no batch follows')
    batch = intake.manager.batch(label)
    running = {
        key: state.record
        for key, state in batch.state(compare=False).items()
        if state.record is not None and state.status is Status.PENDING
    }
    shown: tuple[Mapping[str, str], list[str]] = ({}, [])  # refused, waiting
    failed: str | None = None  # what the step before raised, as said
    done = 0
    while steps is None or done < steps:
        if done:
            pause()
        if stopped():
            sys.stderr.write('stopping after the reductions that run\n')
            break
        done += 1
        before = set(batch.frame().index)
        error = None  # why the step raised, for the line
        told = ''  # what stderr says of it
        try:
            # None only if no batch follows, which this process alone changes.
            stepped = intake.step()
        except Stopped:
            raise
        except Exception as raised:  # code of the application, as on the page
            stepped, error, told = None, said(raised), step_failure(raised)
        if fresh := error is not None and error != failed:
            sys.stderr.write(f'{told}\n')
        failed = error
        if stepped is None:
            added: list[str] = []
            reducing: list[str] = []
            refused, waiting = shown
        else:
            added = [k for k in batch.frame().index if k not in before]
            reducing = list(stepped.records)
            refused, waiting = dict(stepped.refused), list(stepped.waiting)
            running |= stepped.records
        line = _ended(batch, running)
        if added or reducing or line['ended'] or (refused, waiting) != shown or fresh:
            say(_line(added, reducing, line, refused, waiting, error))
            shown = (refused, waiting)
    if running:
        batch.client.wait(list(running.values()))
        say(_line([], [], _ended(batch, running), *shown, None))


def _line(
    added: list[str],
    reducing: list[str],
    ended: Mapping[str, Mapping[str, str]],
    refused: Mapping[str, str],
    waiting: list[str],
    error: str | None,
) -> dict[str, Any]:
    """A line of :func:`follow`, in its order."""
    return {
        'added': added,
        'reducing': reducing,
        **ended,
        'refused': refused,
        'waiting': waiting,
        'error': error,
    }


def _ended(batch: Batch, running: dict[str, Record]) -> dict[str, dict[str, str]]:
    """
    The status of each row of ``running`` whose reduction ended, as ``ended``,
    and why it failed or was cancelled, as ``failure``; those rows leave
    ``running``.
    """
    records = list(running.values())
    statuses = batch.client.status(records)
    ended = {
        key: record
        for (key, record), status in zip(running.items(), statuses, strict=True)
        if status is not Status.PENDING
    }
    for key in ended:
        del running[key]
    if not ended:
        return {'ended': {}, 'failure': {}}
    states, _ = batch.shown(list(ended))
    why = batch.client.failure(list(ended.values()))
    return {
        'ended': {key: states[key].text for key in ended},
        'failure': {key: w for key, w in zip(ended, why, strict=True) if w},
    }


def _said(line: Mapping[str, Any]) -> str | None:
    """
    A line of :func:`follow` as a person reads it: its parts that are not
    empty, but the error, which stderr says; ``None`` if none is left.
    """

    def keys(names: Iterable[str], why: Mapping[str, str]) -> str:
        return ' '.join(
            f'{k} ({" ".join(why[k].split())})' if k in why else k for k in names
        )

    ended = line['ended']
    parts = [
        ('added', line['added'], {}),
        ('reducing', line['reducing'], {}),
        *(
            (status, [k for k, s in ended.items() if s == status], line['failure'])
            for status in dict.fromkeys(ended.values())
        ),
        ('refused', list(line['refused']), line['refused']),
        ('waiting', line['waiting'], {}),
    ]
    text = ' | '.join(f'{name} {keys(k, why)}' for name, k, why in parts if k)
    return f'{line["time"][11:19]} {text}' if text else None


def _label(command: argparse.ArgumentParser, *, required: bool, help: str) -> None:
    command.add_argument('-l', '--label', required=required, help=help)


def _files_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        'read',
        metavar='READ',
        help='package.module:NAME of a function from the path of a file to its '
        'metadata',
    )
    command.add_argument(
        '--with',
        dest='run_table',
        type=_existing,
        metavar='TABLE',
        help='a run table: a row per run, by its column run or file, which says '
        'its role, its sample, or any other column',
    )


def _template_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument('spec', nargs='?', metavar='SPEC', help='the name of a spec')
    _label(command, required=False, help='the label of a batch, in place of SPEC')


def _rows_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        'source', metavar='NAME', help='the name of a row source in -s/--sources'
    )


def _show_arguments(command: argparse.ArgumentParser) -> None:
    _label(command, required=True, help='the label of the batch')


def _reduce_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        'template',
        metavar='TEMPLATE',
        type=_existing,
        nargs='?',
        help='a template, YAML (see the command template); without it, the '
        'settings of the batch',
    )
    command.add_argument(
        'table',
        metavar='TABLE',
        type=_existing,
        nargs='?',
        help=f'the batch table: a table file of {", ".join(SUFFIXES)} with a '
        'column key and a column per parameter that differs between rows; '
        'without it, that of the batch',
    )
    _label(command, required=True, help='the label of the batch')
    command.add_argument(
        '--source',
        metavar='NAME',
        help='make a new batch of the rows that the row source NAME in '
        '-s/--sources has filled now; not with a table file',
    )
    command.add_argument(
        '--first', metavar='KEY', help='with --source, the rows from the row KEY on'
    )
    command.add_argument(
        '--last', metavar='KEY', help='with --source, the rows up to the row KEY'
    )
    command.add_argument(
        '--later',
        action='store_true',
        help='with --source, no rows now: follow adds the rows filled from now '
        'on, take any',
    )
    command.add_argument(
        '--staged',
        action='store_true',
        help='run the rows through a stage, which reduces once what they share',
    )
    command.add_argument(
        '--remaining',
        action='store_true',
        help='reduce only the rows that are not done: not those that read earlier',
    )
    command.add_argument(
        '--earlier-settings',
        action='store_true',
        help='with --remaining, reduce the rows that read earlier too',
    )


def _export_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        'table',
        metavar='TABLE',
        type=_existing,
        nargs='?',
        help='a table file that reduce or show wrote; or -l',
    )
    command.add_argument(
        'directory', metavar='DIRECTORY', type=Path, help='the folder to write to'
    )
    _label(
        command,
        required=False,
        help='the label of a batch, in place of TABLE: its rows as they stand',
    )


def _take_arguments(command: argparse.ArgumentParser) -> None:
    _label(command, required=True, help='the label of the batch')
    command.add_argument(
        'keys', metavar='KEY', nargs='+', help='the key of a row that offered lists'
    )


def _seconds(text: str) -> float:
    """A number of seconds, 0 or more; argparse refuses another."""
    with suppress(ValueError):
        if math.isfinite(seconds := float(text)) and seconds >= 0:
            return seconds
    raise argparse.ArgumentTypeError(f'{text}: give a number of seconds, 0 or more')


def _steps(text: str) -> int:
    """A number of steps, 1 or more; argparse refuses another."""
    with suppress(ValueError):
        if (steps := int(text)) >= 1:
            return steps
    raise argparse.ArgumentTypeError(f'{text}: give a number of steps, 1 or more')


def _follow_arguments(command: argparse.ArgumentParser) -> None:
    _label(command, required=True, help='the label of the batch')
    command.add_argument(
        '--interval',
        type=_seconds,
        default=5.0,
        metavar='SECONDS',
        help='the seconds between two steps (default: 5)',
    )
    command.add_argument(
        '--steps',
        type=_steps,
        metavar='N',
        help='do N steps, wait for the reductions, and exit',
    )


def _with_sources(name: str) -> Callable[[argparse.Namespace], str | None]:
    """A check that -s/--sources is given to the command ``name``."""
    return lambda args: None if args.sources else f'{name} needs -s/--sources'


def _one_of(first: str, second: str) -> Callable[[argparse.Namespace], str | None]:
    """A check that exactly one of two arguments is given, by their names."""

    def check(args: argparse.Namespace) -> str | None:
        given = [n for n in (first, second) if getattr(args, n) is not None]
        if len(given) != 1:
            return f'give {first.upper()} or -l LABEL, one of them'
        return None

    return check


def _reduce_check(args: argparse.Namespace) -> str | None:
    if args.earlier_settings and not args.remaining:
        return '--earlier-settings needs --remaining'
    if args.table is not None and args.source:
        return 'give a table file or --source, not both'
    if args.source is None and (args.first or args.last or args.later):
        return '--first, --last and --later pick rows of --source'
    if args.source is not None and not args.sources:
        return '--source needs -s/--sources'
    return None


@dataclass(frozen=True)
class _Command:
    """
    A command: the function that runs it, its text for its --help, and what
    it takes. ``output``: what it writes at its end, to -o or stdout: a table,
    to a table file or as JSON, or text; ``None`` if it writes nothing then,
    and its function gives its exit status. ``folder``: it opens the folder,
    so it needs the bindings. ``check`` says what is wrong with the
    arguments, before anything runs. ``status`` gives the exit status of what
    was written.
    """

    run: Callable[[_Given], _Result]
    description: str
    arguments: Callable[[argparse.ArgumentParser], None] = lambda _: None
    check: Callable[[argparse.Namespace], str | None] = lambda _: None
    status: Callable[[Any], int] = lambda _: 0
    output: Literal['table', 'text'] | None = 'table'
    folder: bool = True


_COMMANDS = {
    'files': _Command(
        _files,
        description='Write the files table of the folder (files.csv), '
        'a row per file that READ knows. It refuses to overwrite an -o that '
        'exists, as a person may have added to it, unless --with is given. It '
        'replaces -o at once and does not open the folder, so it runs while '
        'follow holds the folder, and follow then sees the runs it adds.',
        arguments=_files_arguments,
        folder=False,
    ),
    'specs': _Command(
        _specs,
        description='List the specs that the bindings bind, one per row: name, '
        'version, title and the names of its outputs.',
    ),
    'template': _Command(
        _template,
        description='Write a template of SPEC to fill in, with a comment on each '
        'parameter: whether it is required or its default, what it takes, and '
        'what it is. Or, with -l, the settings of the batch of LABEL, as a '
        'template for reduce.',
        arguments=_template_arguments,
        check=_one_of('spec', 'label'),
        output='text',
    ),
    'sources': _Command(
        _row_sources,
        description='List the row sources of -s/--sources by name, with the '
        'parameters that each fills for each row, and what a row is.',
        check=_with_sources('sources'),
        folder=False,
    ),
    'rows': _Command(
        _rows,
        description='Write the rows that the row source NAME makes from the '
        'files now. It reads and writes no batch.',
        arguments=_rows_arguments,
        check=_with_sources('rows'),
    ),
    'batches': _Command(
        _batches,
        description='List the batches of the folder: label, title, spec, number '
        'of rows, how many have a completed result (reduced, whether still '
        'current or not: show tells which rows are done), the row source it was '
        'made from, and whether it follows that source.',
    ),
    'show': _Command(
        _show,
        description='Write the batch table of LABEL, with the columns '
        'status, record and failure, as reduce writes it. It reduces nothing.',
        arguments=_show_arguments,
    ),
    'reduce': _Command(
        _reduce,
        description='Make the batch of LABEL if it has none, or give it new '
        'settings and a new batch table; then reduce its rows, every one that '
        'can be reduced unless --remaining, and write its batch table with the '
        'columns '
        'status, record and failure. Earlier records stay in the folder.',
        arguments=_reduce_arguments,
        check=_reduce_check,
        status=_reduced,
    ),
    'export': _Command(
        _export,
        description='Write the outputs of each done row of the table file '
        'TABLE, or of the batch of LABEL as it stands, to a folder of its key in '
        'DIRECTORY, and those rows with the path of each output.',
        arguments=_export_arguments,
        check=_one_of('table', 'label'),
    ),
    'offered': _Command(
        _offered,
        description='List the rows that the row source of the batch of LABEL '
        'makes now and its batch table lacks, with a column waits: whether a '
        'parameter that the source fills is empty. follow adds a row once it '
        'is filled, unless the batch left it out when made or a person '
        'removed it; take adds any.',
        arguments=_show_arguments,
        check=_with_sources('offered'),
    ),
    'take': _Command(
        _take,
        description='Put the rows of KEY, as the row source of the batch of '
        'LABEL makes them now, into its batch table, and write them as show '
        'does. '
        'It reduces nothing. A row that waits for a file goes in with its cell '
        'empty, and stays incomplete until a person fills it: follow changes '
        'no row of the batch table.',
        arguments=_take_arguments,
        check=_with_sources('take'),
    ),
    'follow': _Command(
        _follow,
        description='Let the batch of LABEL follow its row source, in place '
        'of the batch that followed, and do a step every SECONDS until Ctrl-C '
        'or SIGTERM, or for N steps; then wait for the reductions that run. '
        'A step adds the rows of the source that are filled and not '
        'considered, and reduces the rows that are ready and have no result. '
        'It writes a line per step that did something, an object with --json. '
        'The batch still follows when it exits: stop ends that.',
        arguments=_follow_arguments,
        check=_with_sources('follow'),
        output=None,
    ),
    'stop': _Command(
        _stop,
        description='Let no batch follow, and say which one did.',
        output=None,
    ),
    'remove': _Command(
        _remove,
        description='Remove the batch of LABEL: its batch table, settings and '
        'what it keeps of its row source. Its records stay in the folder, under '
        'the label, so a new batch takes another label.',
        arguments=_show_arguments,
        output=None,
    ),
}
"""The commands by name, in the order of a first session."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='essapps-batch',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-w',
        '--workspace',
        type=Path,
        default=Path(),
        help='the folder, with its files.csv (default: here)',
    )
    parser.add_argument(
        '-b',
        '--bind',
        metavar='MODULE:NAME',
        help='the bindings, the reductions that the folder runs; '
        'every command but files and sources needs them',
    )
    parser.add_argument(
        '-s',
        '--sources',
        metavar='MODULE:NAME',
        help='the row sources by name, for sources, rows, offered, take, '
        'follow, reduce --source and template',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='write tables as JSON; follow: an object per line',
    )
    parser.add_argument(
        '--proposal', default='local', help='the proposal of the files (default: local)'
    )
    commands = parser.add_subparsers(
        dest='command',
        required=True,
        metavar='COMMAND',
        help='a command above; COMMAND --help says more',
    )
    for name, command in _COMMANDS.items():
        sub = commands.add_parser(name, description=command.description)
        command.arguments(sub)
        if command.output is None:
            continue
        sub.add_argument(
            '-o',
            '--output',
            type=Path,
            help=f'the file to write, of {", ".join(SUFFIXES)}, or .json with '
            '--json (default: stdout)'
            if command.output == 'table'
            else 'the YAML file to write (default: stdout)',
        )
    return parser


def _cell(value: Any) -> Any:
    """A cell as JSON takes it: ``None`` if empty, a Python value."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA or value == '':
        return None
    if isinstance(value, float) and value != value:
        return None
    return value


def _write(result: pd.DataFrame | str, args: argparse.Namespace) -> None:
    """Write a template's text, or a table as a table file or as JSON."""
    if isinstance(result, pd.DataFrame) and not args.json:
        write_table(result, sys.stdout if args.output is None else args.output)
        return
    if isinstance(result, pd.DataFrame):
        if result.index.name is not None:
            result = result.reset_index()
        rows = [
            {name: _cell(value) for name, value in row.items()}
            for row in result.to_dict('records')
        ]
        result = json.dumps(rows, indent=2, ensure_ascii=False, default=str) + '\n'
    if args.output is None:
        sys.stdout.write(result)
    else:
        replace_at_once(args.output, lambda path: path.write_text(result))


def _opened(given: _Given, command: _Command) -> _Result:
    """Run ``command`` on the folder, which it opens and closes."""
    args = given.args
    if args.bind is None:
        # a workspace goes on with pending records, which need their bindings
        raise _Wrong('--bind is required: the bindings of the workspace')
    bind = _bindings(args.bind)
    if not (args.workspace / 'files.csv').is_file():
        raise _Wrong(f'{args.workspace}: no files.csv; the command files writes it')
    try:  # as read once, so that a files table in error is refused
        folder_files(args.workspace)
    except FileNotFoundError as error:
        raise ValueError(f'files.csv lists {error}, which does not exist') from None
    except ValueError as error:
        raise ValueError(f'files.csv: {error}') from None
    definitions = FolderDefinitions(args.workspace)
    # Read again when files.csv is replaced, so that follow finds the files
    # that arrive.
    files = CorrectedFiles(ArrivingFiles(args.workspace), definitions=definitions)
    try:
        client = open_folder(args.workspace, bind, files=files, proposal=args.proposal)
    except RuntimeError as error:  # another process holds the folder
        raise _Held(str(error)) from None
    with client:
        return command.run(_Given(args, given.sources, (client, definitions)))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command, and return its exit status (see the module)."""
    parser = _parser()
    args = parser.parse_args(argv)
    command = _COMMANDS[args.command]
    try:
        if command.output == 'table' and args.output is not None:
            suffixes = ('.json',) if args.json else SUFFIXES
            if args.output.suffix not in suffixes:
                raise _Wrong(f'{args.output}: write a file of {", ".join(suffixes)}')
        sources = {} if args.sources is None else _sources(args.sources)
        if (wrong := command.check(args)) is not None:
            raise _Wrong(wrong)
        # A name of the command line, so refused before the folder is opened,
        # also while another process holds it.
        source = getattr(args, 'source', None)
        if source is not None and source not in sources:
            raise _Wrong(not_registered(source, sources))
        given = _Given(args, sources)
        result = _opened(given, command) if command.folder else command.run(given)
    except _Wrong as error:
        parser.error(str(error))
    except _Held as error:
        sys.stderr.write(f'{error}\n')
        return 1
    except NotKept as error:
        sys.stderr.write(f'not kept: {error}\n')
        return 1
    except Stopped as error:
        sys.stderr.write(f'{error}\n')
        return 1
    except (SubmitError, ValueError) as error:  # also definitions of another format
        sys.stderr.write(f'{said(error)}\n')  # a ValidationError of the source too
        return 2
    if isinstance(result, int):
        return result
    _write(result, args)
    return command.status(result)


if __name__ == '__main__':
    sys.exit(main())

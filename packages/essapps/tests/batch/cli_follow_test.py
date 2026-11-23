# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The loop of ``follow`` of ``essapps-batch``, its signals and its steps."""

import signal
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ess.apps import Client, Status, Stopped, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Intake,
    RowSource,
)
from ess.apps.batch.cli import follow, main
from ess.apps.log import Event, Log
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from . import toys
from .conftest import SOURCES, arrive


@pytest.fixture
def following(intake: Intake, sample: toys.Measure) -> Batch:
    """A batch of ``intake`` that follows, made when run 1 was measured."""
    sample(1, 'water')
    label = toys.from_pairs(intake, 'plain', title='A', follow=True)
    return intake.manager.batch(label)


@pytest.mark.parametrize(
    ('error', 'said'),
    [
        (ValueError('no'), 'the source pairs: no'),
        (RuntimeError('boom'), 'RuntimeError: boom'),
        (toys.refused_metadata(), f'the source pairs: {toys.REFUSED_METADATA}'),
    ],
    ids=['refusal', 'defect', 'metadata refused'],
)
def test_a_step_that_raises_is_said_while_it_changes_and_the_steps_go_on(
    manager: BatchManager,
    sample: toys.Measure,
    error: Exception,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raising: list[Exception] = []

    def rows(files: Sequence[FileInfo]) -> pd.DataFrame:
        if raising:
            raise raising[0]
        return toys.pair(files)

    intake = Intake(manager, sources={'pairs': RowSource(rows, toys.PAIRS.fills)})
    sample(1, 'water')
    label = toys.from_pairs(intake, 'plain', title='A', follow=True)
    after = [
        lambda: raising.append(error),
        lambda: None,  # the next step raises the same
        lambda: (raising.clear(), sample(2, 'oil')),
        lambda: raising.append(error),  # said again
    ]
    lines: list[Mapping[str, Any]] = []

    follow(
        intake,
        say=lines.append,
        pause=lambda: after.pop(0)(),
        stopped=lambda: False,
        steps=5,
    )

    assert capsys.readouterr().err.count(f'Follow could not step: {said}') == 2
    assert {line['error'] for line in lines} == {None, said}
    assert list(intake.manager.batch(label).frame().index) == ['1', '2']


def test_a_stop_asked_ends_the_steps_once_the_reductions_that_run_end(
    intake: Intake,
    following: Batch,
    sample: toys.Measure,
    gate: toys.Gate,
) -> None:
    gate.close()  # the reduction of row 1 runs until the stop is asked
    asked: list[bool] = []
    lines: list[Mapping[str, Any]] = []

    def stopped() -> bool:
        if asked:
            sample(2, 'oil')  # arrives, but no step adds it
            gate.open()
        asked.append(True)
        return len(asked) > 1

    follow(intake, say=lines.append, pause=lambda: None, stopped=stopped)

    record = following.state()['1'].record
    assert following.client.status([record]) == [Status.COMPLETED]
    assert list(following.frame().index) == ['1']
    assert lines[0]['reducing'] == ['1']
    assert lines[-1]['ended'] == {'1': 'done'}


def test_a_step_that_finds_nothing_new_says_nothing(
    intake: Intake, following: Batch
) -> None:
    lines: list[Mapping[str, Any]] = []
    steps = {'pause': lambda: None, 'stopped': lambda: False, 'steps': 1}
    follow(intake, say=lambda _: None, **steps)

    follow(intake, say=lines.append, **steps)

    assert following.state()['1'].text == 'done'
    assert lines == []


def test_reductions_that_run_when_the_steps_start_are_said_once_they_end(
    intake: Intake, following: Batch, gate: toys.Gate
) -> None:
    gate.close()
    following.reduce(['1'])  # as by a process that ended meanwhile
    lines: list[Mapping[str, Any]] = []

    def stopped() -> bool:
        gate.open()
        return False

    follow(intake, say=lines.append, pause=lambda: None, stopped=stopped, steps=1)

    assert [line['reducing'] for line in lines] == [[]] * len(lines)
    assert {k: v for line in lines for k, v in line['ended'].items()} == {'1': 'done'}
    assert len(following.client.records(label=following.label)) == 1


class FullDisk(Log):
    """A log in memory whose appends fail once the disk is full."""

    full = False

    def append(self, event: Event) -> Event:
        if self.full:
            raise OSError('no space left on device')
        return super().append(event)


def test_a_workspace_that_stopped_ends_the_steps(
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
    sample: toys.Measure,
) -> None:
    log = FullDisk()
    plain = function_binding(toys.PLAIN, staged_binding.function)
    workspace = Workspace(
        proposal='p1', files=files, bind=[plain], log=log, store=FakeStore()
    )
    manager = BatchManager(Client(workspace, submitter='anna'), definitions=definitions)
    intake = Intake(manager, sources=toys.SOURCES)
    sample(1, 'water')
    toys.from_pairs(intake, 'plain', title='A', follow=True)
    log.full = True

    with pytest.raises(Stopped, match='the workspace stopped'):
        follow(
            intake,
            say=lambda _: None,
            pause=lambda: None,
            stopped=lambda: False,
            steps=3,
        )
    with suppress(Stopped):
        workspace.close()


def test_a_signal_ends_follow_once_the_reduction_that_runs_ended(
    from_3: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = signal.getsignal(signal.SIGINT)
    arrive(folder, 5, content=77)  # its reduction sends SIGINT

    assert main([*from_3, 'follow', '-l', 'x', '--interval', '0']) == 0

    said = capsys.readouterr()
    assert 'stopping after the reductions that run' in said.err
    assert 'done 5' in said.out
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.parametrize(
    'option', [['--steps', '0'], ['--interval', '-1'], ['--interval', 'inf']]
)
def test_follow_refuses_steps_and_intervals_that_cannot_be(
    command: list[str], option: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([*command, *SOURCES, 'follow', '-l', 'x', *option])

    assert f'{option[1]}: give a number of' in capsys.readouterr().err

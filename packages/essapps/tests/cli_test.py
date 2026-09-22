# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The ``essapps`` command, against a real server thread."""

from collections.abc import Mapping

import pytest
from click.testing import CliRunner

from ess.apps.cli import main
from ess.apps.spec import DatasetRef


@pytest.fixture
def env(server_url: str) -> Mapping[str, str]:
    return {
        'ESSAPPS_URL': server_url,
        'ESSAPPS_INSTRUMENT': 'dream',
        'ESSAPPS_PROPOSAL': 'p1',
        'ESSAPPS_SUBMITTER': 'simon',
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_submit_wait_output_round_trip(
    runner: CliRunner, env: Mapping[str, str], run_ref: DatasetRef
) -> None:
    result = runner.invoke(
        main, ['submit', 'load/v1', '--run', str(run_ref), '--scale', '2.0'], env=env
    )
    assert result.exit_code == 0, result.output
    record_id = result.output.strip()

    result = runner.invoke(main, ['wait', record_id], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f'{record_id} completed'

    result = runner.invoke(main, ['output', record_id, 'total'], env=env)
    assert result.exit_code == 0, result.output
    assert '72.0' in result.output


def test_submit_help_lists_the_generated_flags(
    runner: CliRunner, env: Mapping[str, str]
) -> None:
    result = runner.invoke(main, ['submit', 'load/v1', '--help'], env=env)
    assert result.exit_code == 0, result.output
    assert '--run' in result.output
    assert '--scale' in result.output


def test_submit_without_a_required_flag_exits_2(
    runner: CliRunner, env: Mapping[str, str]
) -> None:
    result = runner.invoke(main, ['submit', 'load/v1', '--scale', '2.0'], env=env)
    assert result.exit_code == 2


def test_submit_of_an_unknown_spec_is_a_clean_error(
    runner: CliRunner, env: Mapping[str, str]
) -> None:
    result = runner.invoke(main, ['submit', 'no-such-spec/v1'], env=env)
    assert result.exit_code != 0
    assert 'Traceback' not in result.output


def test_wait_on_a_failing_run_exits_1(
    runner: CliRunner, env: Mapping[str, str]
) -> None:
    result = runner.invoke(
        main, ['submit', 'load/v1', '--run', 'run:dream/999', '--scale', '1.0'], env=env
    )
    assert result.exit_code == 0, result.output
    record_id = result.output.strip()

    result = runner.invoke(main, ['wait', record_id], env=env)
    assert result.exit_code == 1

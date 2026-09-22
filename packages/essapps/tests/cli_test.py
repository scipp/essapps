# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The ``essapps`` command, against a real server thread."""

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import scipp as sc
from click.testing import CliRunner

from ess.apps.cli import main
from ess.apps.datastore import Serializers
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


@pytest.fixture
def completed(runner: CliRunner, env: Mapping[str, str], run_ref: DatasetRef) -> str:
    """The id of a completed load run, submitted and awaited through the CLI."""
    result = runner.invoke(
        main, ['submit', 'load/v1', '--run', str(run_ref), '--scale', '2.0'], env=env
    )
    assert result.exit_code == 0, result.output
    record_id = result.output.strip()
    assert runner.invoke(main, ['wait', record_id], env=env).exit_code == 0
    return record_id


def test_output_without_a_name_lists_literals_inline_and_stored_by_reference(
    runner: CliRunner, env: Mapping[str, str], completed: str
) -> None:
    result = runner.invoke(main, ['output', completed], env=env)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f'data\t{completed}.data',
        'total\t{"value": 72.0, "unit": "counts"}',
    ]


def test_output_to_a_folder_downloads_the_file(
    runner: CliRunner, env: Mapping[str, str], completed: str, tmp_path: Path
) -> None:
    result = runner.invoke(
        main, ['output', completed, 'data', '--to', str(tmp_path / 'out')], env=env
    )
    assert result.exit_code == 0, result.output
    path = Path(result.output.strip())
    assert path.parent == tmp_path / 'out'
    assert isinstance(Serializers().load(path), sc.DataArray)


def test_output_server_path_names_the_servers_copy(
    runner: CliRunner, env: Mapping[str, str], completed: str
) -> None:
    result = runner.invoke(
        main, ['output', completed, 'data', '--server-path'], env=env
    )
    assert result.exit_code == 0, result.output
    assert Path(result.output.strip()).exists()


def test_publish_refuses_an_in_process_binding_unless_allowed_then_is_idempotent(
    runner: CliRunner, env: Mapping[str, str], completed: str
) -> None:
    args = ['publish', completed, 'data', '--via', 'fake']
    refused = runner.invoke(main, args, env=env)
    assert refused.exit_code == 1
    assert 'in-process' in refused.output
    assert 'Traceback' not in refused.output
    first = runner.invoke(main, [*args, '--allow-reused'], env=env)
    assert first.exit_code == 0, first.output
    assert first.output.strip().startswith('fake/')
    assert (
        runner.invoke(main, [*args, '--allow-reused'], env=env).output == first.output
    )


def test_publish_via_an_unknown_publisher_is_a_clean_error(
    runner: CliRunner, env: Mapping[str, str], completed: str
) -> None:
    result = runner.invoke(
        main, ['publish', completed, 'data', '--via', 'nope'], env=env
    )
    assert result.exit_code == 1
    assert 'Traceback' not in result.output
    assert "Error: no publisher 'nope'; known: ['fake']" in result.output


def test_serve_rejects_a_malformed_publisher(runner: CliRunner, tmp_path: Path) -> None:
    args = [
        'serve',
        '--root',
        str(tmp_path),
        '--registry',
        'a:b',
        '--datasets',
        str(tmp_path),
    ]
    result = runner.invoke(main, [*args, '--publisher', 'fake'])
    assert result.exit_code == 2
    assert 'NAME=MODULE:FACTORY' in result.output


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


def test_specs_lists_id_title_and_description_without_a_proposal(
    runner: CliRunner, server_url: str
) -> None:
    result = runner.invoke(main, ['specs'], env={'ESSAPPS_URL': server_url})
    assert result.exit_code == 0, result.output
    assert 'load/v1\tLoad\tLoad a run from a scipp HDF5 file' in result.output


def test_specs_json_carries_the_params_schema(
    runner: CliRunner, server_url: str
) -> None:
    result = runner.invoke(main, ['specs', '--json'], env={'ESSAPPS_URL': server_url})
    assert result.exit_code == 0, result.output
    load = next(s for s in json.loads(result.output) if s['name'] == 'load')
    assert 'run' in load['params_schema']['properties']


def test_datasets_lists_reference_and_path(
    runner: CliRunner, env: Mapping[str, str], run_file: Path, run_ref: DatasetRef
) -> None:
    result = runner.invoke(main, ['datasets'], env=env)
    assert result.exit_code == 0, result.output
    assert f'{run_ref}\t{run_file}' in result.output.splitlines()


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

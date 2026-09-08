# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps.client import Client, local
from ess.apps.examples import registry, write_run
from ess.apps.spec import Ref


@pytest.fixture
def client(tmp_path: Path):
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
    )
    yield client
    client.close()


@pytest.fixture
def run_file(tmp_path: Path) -> Path:
    return write_run(tmp_path / 'run1.h5', [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])


@pytest.fixture
def run_ref(client: Client, run_file: Path) -> Ref:
    return client.file(run_file)

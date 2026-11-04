# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Files as a client sees them, and local paths reaching a workflow."""

from typing import Any

import pytest

from ess.apps import Client, SubmitError, file, local
from ess.apps.testing import FakeFiles
from ess.spec import function_binding

from .conftest import LOAD, LoadOutputs, Sample, _load, load_run


def test_a_file_reaches_the_workflow_as_its_local_path(
    files: FakeFiles,
) -> None:
    received = []

    def load(params: Sample) -> LoadOutputs:
        received.append(params.sample)
        return load_run(params)

    bind = [function_binding(LOAD, load)]
    with local(proposal='p1', files=files, bind=bind) as client:
        client.compute(_load(2))

    ref = files.resolve(file(run=2))
    assert received == [files.path(ref)]


@pytest.mark.parametrize(
    'inline',
    ['/data/run-1.json', {'path': '/data/run-1.json'}],
)
def test_a_value_given_inline_at_a_file_field_is_refused(
    client: Client, inline: Any
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(LOAD, params={'sample': inline})
    (error,) = raised.value.errors
    assert error.loc == ('sample',)
    assert error.msg.endswith(' is not a file; a file field is written as file(...)')

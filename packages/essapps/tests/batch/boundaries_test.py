# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The toolbox depends on the framework, and the framework knows nothing of it."""

import subprocess
import sys

import pytest


def modules_after(statement: str) -> set[str]:
    code = f'{statement}; import sys; print(" ".join(sys.modules))'
    done = subprocess.run(  # noqa: S603
        [sys.executable, '-c', code], capture_output=True, text=True, check=True
    )
    return set(done.stdout.split())


@pytest.mark.parametrize('statement', ['import ess.apps', 'import ess.apps.panel'])
def test_the_framework_does_not_import_the_toolbox(statement: str) -> None:
    assert not any(m.startswith('ess.apps.batch') for m in modules_after(statement))


def test_the_logic_of_the_toolbox_imports_no_widget_library() -> None:
    loaded = modules_after('import ess.apps.batch')

    assert not loaded & {'panel', 'bokeh', 'ipywidgets'}
    assert 'ess.apps.panel' not in loaded

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


def test_the_logic_of_the_toolbox_imports_nothing_of_the_page() -> None:
    loaded = modules_after('import ess.apps.batch, ess.apps.batch.auto')

    assert not any(m.startswith('ess.apps.batch.web') for m in loaded)


@pytest.mark.parametrize('module', ['server', 'models', 'pages'])
def test_what_the_page_does_and_sends_does_not_depend_on_http(module: str) -> None:
    # The package imports its API, which needs FastAPI. A package without that
    # import shows what the module itself loads.
    statement = (
        'import pathlib, sys, types, ess.apps.batch as batch;'
        "web = types.ModuleType('ess.apps.batch.web');"
        "web.__path__ = [str(pathlib.Path(batch.__file__).parent / 'web')];"
        "sys.modules['ess.apps.batch.web'] = web;"
        f'import ess.apps.batch.web.{module}'
    )

    loaded = modules_after(statement)

    assert f'ess.apps.batch.web.{module}' in loaded
    assert not loaded & {'fastapi', 'starlette'}

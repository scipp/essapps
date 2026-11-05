# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import subprocess
import sys


def test_specs_load_without_the_workflow_package() -> None:
    code = (
        'import sys, ess.instruments.loki.specs;'
        "sys.exit(any(m.startswith(('ess.sans', 'ess.loki', 'sciline'))"
        ' for m in sys.modules))'
    )
    result = subprocess.run([sys.executable, '-c', code], check=False)  # noqa: S603
    assert result.returncode == 0

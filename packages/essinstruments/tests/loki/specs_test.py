# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import subprocess
import sys

import pytest
from ess.instruments.loki.specs import BeamCentre
from pydantic import ValidationError


def test_specs_load_without_the_workflow_package() -> None:
    code = (
        'import sys, ess.instruments.loki.specs;'
        "sys.exit(any(m.startswith(('ess.sans', 'ess.loki', 'sciline'))"
        ' for m in sys.modules))'
    )
    result = subprocess.run([sys.executable, '-c', code], check=False)  # noqa: S603
    assert result.returncode == 0


@pytest.mark.parametrize('z', [0.0, -1.0])
def test_a_beam_centre_without_a_distance_from_the_sample_is_refused(z: float) -> None:
    with pytest.raises(ValidationError, match='greater than 0'):
        BeamCentre(x=0.01, y=0.01, z=z)

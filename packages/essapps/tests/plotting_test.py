# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What an application decides about plots, without a widget library."""

import subprocess
import sys

from ess.apps.plotting import slider_dims


def test_every_dimension_but_the_last_two_has_a_slider() -> None:
    assert slider_dims({'time': 50, 'layer': 20, 'y': 200, 'x': 200}) == [
        'time',
        'layer',
    ]
    assert slider_dims({'layer': 2, 'y': 3, 'x': 4}) == ['layer']
    assert slider_dims({'y': 3, 'x': 4}) == []
    assert slider_dims({'x': 4}) == []
    assert slider_dims({}) == []


def test_what_an_application_decides_about_plots_needs_no_panel() -> None:
    code = (
        'import sys; import ess.apps, ess.apps.plotting, ess.apps.listing;'
        "assert 'panel' not in sys.modules and 'plopp' not in sys.modules"
    )
    subprocess.run(  # noqa: S603
        [sys.executable, '-I', '-c', code], check=True
    )

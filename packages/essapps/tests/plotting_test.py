# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What an application decides about plots, without a widget library."""

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from ess.apps import Record, Request, SpecId
from ess.apps.plotting import Plot, read_inputs, slider_dims
from ess.spec import OutputRef


def test_every_dimension_but_the_last_two_has_a_slider() -> None:
    assert slider_dims({'time': 50, 'layer': 20, 'y': 200, 'x': 200}) == [
        'time',
        'layer',
    ]
    assert slider_dims({'layer': 2, 'y': 3, 'x': 4}) == ['layer']
    assert slider_dims({'y': 3, 'x': 4}) == []
    assert slider_dims({'x': 4}) == []
    assert slider_dims({}) == []


def test_a_plot_input_cannot_be_an_element_of_an_output() -> None:
    element = OutputRef(record='r', output='images', key='first')
    record = Record(
        id='s',
        request=Request(SpecId(name='s', version=1), params={'image': element}),
        proposal='p',
        submitter='u',
        created=datetime.now(UTC),
        outputs=(),
    )
    with pytest.raises(ValueError, match='image does not reference a whole output'):
        read_inputs(Plot(print, inputs=('image',)), None, record)  # type: ignore[arg-type]


def test_what_an_application_decides_about_plots_needs_no_panel() -> None:
    code = (
        'import sys; import ess.apps, ess.apps.plotting, ess.apps.listing;'
        "assert 'panel' not in sys.modules and 'plopp' not in sys.modules"
    )
    subprocess.run(  # noqa: S603
        [sys.executable, '-I', '-c', code], check=True
    )

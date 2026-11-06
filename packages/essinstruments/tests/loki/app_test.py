# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The application through its widgets, on the files of esssans' tutorial, in the
order a person uses it: masks, beam centre, and I(Q) of one run.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import panel as pn
import pytest
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.plots import PLOTS

from ess.apps import FolderFiles, Record, file, local
from ess.apps.panel import App

pn.extension('tabulator')


@dataclass
class Session:
    app: App
    masks: Record
    centre: Record
    iofq: Record


@pytest.fixture
def tutorial(sample: Path, mask_file: Path, direct_beam: Path) -> FolderFiles:
    def run(number: int, role: str | None = None) -> dict[str, object]:
        row = {'file': f'{number}-2022-02-28_2215.nxs', 'run': number}
        return row if role is None else row | {'role': role}

    table = [
        run(60339, 'sample'),
        run(60387, 'sample'),
        run(60386, 'transmission'),
        run(60393, 'background'),
        run(60392),  # the can's transmission run and the empty beam
        {'file': mask_file.name, 'kind': 'mask'},
        {'file': direct_beam.name, 'kind': 'calibration', 'role': 'direct_beam'},
    ]
    return FolderFiles(sample.parent, table)


@pytest.fixture
def session(
    tutorial: FolderFiles, mask_file: Path, direct_beam: Path
) -> Iterator[Session]:
    """The app with the masks, the beam centre, and I(Q) of one run computed."""
    with local(proposal='p1', files=tutorial, bind=BINDINGS) as client:
        app = App(client, plots=PLOTS)

        def submit(title: str, params: dict[str, Any]) -> Record:
            app.picker.value = title
            app.form.set(params)
            record = app.submit()
            assert record is not None
            client.wait(record)
            app.poll()
            return record

        masks = submit(
            'Pixel masks',
            {'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
        )
        centre = submit(
            'Beam centre', {'sample': file(run=60339), 'masks': masks.ref('masks')}
        )
        iofq = submit(
            'I(Q)',
            {
                'sample': file(run=60387),
                'sample_transmission': file(run=60386),
                'can': file(run=60393),
                'can_transmission': file(run=60392),
                'empty_beam': file(run=60392),
                'direct_beam': file(path=direct_beam),
                'masks': masks.ref('masks'),
                'beam_centre': centre.ref('centre'),
                'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
                'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
            },
        )
        yield Session(app, masks, centre, iofq)


def test_masks_then_centre_then_one_run_are_computed(session: Session) -> None:
    client = session.app.client

    assert client.failure(session.masks) is None
    assert client.failure(session.centre) is None
    assert client.failure(session.iofq) is None
    assert session.iofq.request.params['beam_centre'] == session.centre.ref('centre')


def test_the_plots_of_loki_draw_the_outputs_of_real_records(session: Session) -> None:
    outputs = session.app.outputs
    drawn = {}
    for record, name, view in [
        (session.masks, 'image', 'Masks on image'),
        (session.centre, 'image', 'Centre on image'),
        (session.iofq, 'iofq', 'I(Q)'),
        (session.iofq, 'transmission', 'Transmission'),
    ]:
        outputs.select(record, name)
        outputs.views.value = view
        (pane,) = outputs.view.objects
        assert isinstance(pane, pn.pane.Matplotlib), pane.object
        drawn[view] = pane.object.axes[0]

    assert any(line.get_marker() == '+' for line in drawn['Centre on image'].lines)
    assert drawn['I(Q)'].get_yscale() == 'log'
    legend = drawn['Transmission'].get_legend()
    assert [t.get_text() for t in legend.get_texts()] == ['sample', 'can']

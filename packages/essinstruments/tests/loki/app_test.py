# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The application through its widgets, on the files of esssans' tutorial, in the
order a person uses it: detector, masks, beam centre, and I(Q) of one run.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import panel as pn
import pytest
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.plots import PLOTS

from ess.apps import FolderFiles, Listing, Record, file, local
from ess.apps.panel import App

pn.extension('tabulator')


@dataclass
class Session:
    app: App
    detector: Record
    masks: Record
    centre: Record
    angles: Record
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
    """The app with every step, up to I(Q) of one run, computed."""
    with local(proposal='p1', files=tutorial, bind=BINDINGS) as client:
        app = App(client, plots=PLOTS)

        def submit(title: str, params: dict[str, Any]) -> Record:
            app.picker.value = title
            app.form.set(params, Listing(app.client))
            record = app.submit()
            assert record is not None
            client.wait(record)
            app.poll()
            return record

        detector = submit('Calibrated detector', {'run': file(run=60339)})
        image = detector.ref('detector')
        masks = submit(
            'Pixel masks',
            {'detector': image, 'mask_files': [file(path=mask_file)]},
        )
        centre = submit('Beam centre', {'detector': image, 'masks': masks.ref('masks')})
        angles = submit(
            'Create mask',
            {
                'detector': image,
                'beam_centre': centre.ref('centre'),
                'base': masks.ref('masks'),
                'shapes': [{'start': 3.0, 'stop': 180.0}],
            },
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
        yield Session(app, detector, masks, centre, angles, iofq)


def test_masks_then_centre_then_one_run_are_computed(session: Session) -> None:
    client = session.app.client

    assert client.failure(session.detector) is None
    assert client.failure(session.masks) is None
    assert client.failure(session.centre) is None
    assert client.failure(session.angles) is None
    assert client.failure(session.iofq) is None
    assert session.iofq.request.params['beam_centre'] == session.centre.ref('centre')


def test_the_plots_of_loki_draw_the_outputs_of_real_records(session: Session) -> None:
    outputs = session.app.outputs
    drawn = {}
    for record, name, view in [
        (session.detector, 'detector', 'Detector image'),
        (session.masks, 'masks', 'Masks on image'),
        (session.angles, 'masks', 'Masks on image'),
        (session.centre, 'centre', 'Centre on image'),
        (session.iofq, 'iofq', 'I(Q)'),
        (session.iofq, 'transmission', 'Transmission'),
    ]:
        outputs.select(record, name)
        outputs.views.value = view
        (pane,) = outputs.view.objects
        assert isinstance(pane, pn.pane.Matplotlib), pane.object
        drawn[record.id, view] = pane.object.axes[0]

    def of(record: Record, view: str) -> Any:
        return drawn[record.id, view]

    file_masks, angle_masks = (
        np.nansum(of(record, 'Masks on image').images[0].get_array())
        for record in (session.masks, session.angles)
    )
    assert 0 < angle_masks < file_masks
    all_pixels = of(session.detector, 'Detector image').images[0].get_array()
    assert file_masks < np.nansum(all_pixels)
    centre = of(session.centre, 'Centre on image')
    assert np.nansum(centre.images[0].get_array()) == file_masks
    (cross,) = (line for line in centre.lines if line.get_marker() == '+')
    found = session.app.client.output(session.centre, 'centre')
    assert (cross.get_xdata()[0], cross.get_ydata()[0]) == (found.x, found.y)
    assert of(session.iofq, 'I(Q)').get_yscale() == 'log'
    legend = of(session.iofq, 'Transmission').get_legend()
    assert [t.get_text() for t in legend.get_texts()] == ['sample', 'can']

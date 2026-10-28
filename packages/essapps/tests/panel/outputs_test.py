# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
``CURVE`` outputs a group with a 1-D and a 0-D array, a 2-D array with a
description, the settings it used, a group without an array, and an
optional array it never returns. It waits for ``gate``,
so a test holds its record pending, and a scale of 0 fails it. ``SCALE``
reads the curve.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import panel as pn
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client, Record, local
from ess.apps.panel import App, Listing, Outputs
from ess.apps.testing import FakeFiles, read_file
from ess.spec import FileModel, FileRef, WorkflowSpec, function_binding


class Run(FileModel):
    """A raw run."""


class Settings(BaseModel):
    scale: float = 1.0


class Curve(BaseModel):
    """
    Counts per bin, and their total.

    The total is that of every bin.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    counts: sc.DataArray
    total: sc.Variable
    bins: int


class CurveParams(BaseModel):
    run: Run
    settings: Settings = Field(default_factory=Settings)


class CurveOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    curve: Curve
    image: sc.Variable = Field(description='Counts per pixel.')
    used: Settings
    extra: sc.Variable | None = None


class ScaleParams(BaseModel):
    curve: Curve


class ScaleOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    total: sc.Variable


CURVE = WorkflowSpec(
    name='curve',
    version=1,
    title='Curve',
    description='The counts of a run.',
    params=CurveParams,
    outputs=CurveOutputs,
)
SCALE = WorkflowSpec(
    name='scale',
    version=1,
    title='Scale',
    description='The total of a curve.',
    params=ScaleParams,
    outputs=ScaleOutputs,
)


@pytest.fixture
def gate() -> threading.Event:
    opened = threading.Event()
    opened.set()
    return opened


@pytest.fixture
def run(tmp_path: Path) -> tuple[FakeFiles, FileRef]:
    files = FakeFiles('p1', tmp_path)
    return files, files.measure(101, {'counts': [1.0, 2.0, 3.0]})


@pytest.fixture
def client(run: tuple[FakeFiles, FileRef], gate: threading.Event) -> Iterator[Client]:
    def curve(params: CurveParams) -> CurveOutputs:
        gate.wait()
        counts = sc.array(dims=['x'], values=read_file(params.run.path)['counts'])
        counts *= 1 / params.settings.scale
        return CurveOutputs(
            curve=Curve(
                counts=sc.DataArray(counts, coords={'x': sc.arange('x', 3.0)}),
                total=counts.sum(),
                bins=3,
            ),
            image=sc.ones(dims=['y', 'x'], shape=[2, 3], unit='counts'),
            used=params.settings,
        )

    def scale(params: ScaleParams) -> ScaleOutputs:
        return ScaleOutputs(total=params.curve.total)

    bind = [function_binding(CURVE, curve), function_binding(SCALE, scale)]
    with local(proposal='p1', files=run[0], bind=bind) as client:
        yield client


@pytest.fixture
def file(run: tuple[FakeFiles, FileRef]) -> FileRef:
    return run[1]


def outputs_of(client: Client, *records: Record) -> Outputs:
    client.wait(list(records))
    return Outputs(client, Listing(client))


def rows(outputs: Outputs) -> list[tuple[str, str, str]]:
    table = outputs.table.value
    return list(zip(table['Output'], table['Type'], table['Status'], strict=True))


def test_a_row_per_output_newest_record_first(client: Client, file: FileRef) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    outputs = outputs_of(client, curve, scale)

    assert rows(outputs) == [
        ('total', 'Variable', 'completed'),
        ('curve', 'Curve', 'completed'),
        ('image', 'Variable', 'completed'),
        ('used', 'Settings', 'completed'),
        ('extra', 'Variable | NoneType', 'not readable'),
    ]
    assert outputs.table.value['Record'].tolist()[0].startswith(scale.id[:6])
    outputs.panel()  # builds, with the warnings of Panel as errors


def test_the_output_selected_shows_what_it_is_how_it_was_made_and_its_value(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    outputs = outputs_of(client, curve, scale)

    outputs.select(curve, 'curve')
    detail = outputs.detail.object
    assert 'Counts per bin, and their total.' in detail
    assert 'The total is' not in detail  # only the first paragraph
    assert '**Spec** Curve (`curve/v1`): The counts of a run.' in detail
    assert '**Files** run 101' in detail
    assert '"scale": 1.0' in detail
    assert '- counts: array (x: 3) [dimensionless], coordinates x' in detail
    assert '- total: 6.0 [dimensionless]' in detail
    assert '- bins: 3' in detail
    assert not outputs.button.disabled

    outputs.select(scale, 'total')
    assert '**Reads** ' + Listing(client).labels[curve.id] in outputs.detail.object

    outputs.select(curve, 'image')
    assert 'Counts per pixel.' in outputs.detail.object


def test_a_plot_has_a_figure_per_array_of_one_or_two_dimensions(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)

    outputs.select(curve, 'curve')
    outputs.plot()
    figures = outputs.plots.objects
    assert isinstance(figures[0], pn.pane.Matplotlib)
    assert figures[1].object == 'curve.total: 0-D, not plotted'

    outputs.select(curve, 'image')
    outputs.plot()
    assert isinstance(outputs.plots.objects[0], pn.pane.Matplotlib)


def test_an_output_without_an_array_is_shown_but_not_plotted(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file, 'settings': {'scale': 2.0}})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'used')

    assert '- scale: 2.0' in outputs.detail.object
    assert outputs.button.disabled


def test_an_output_not_readable_shows_no_value(client: Client, file: FileRef) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'extra')

    assert '**Value**' not in outputs.detail.object
    assert outputs.button.disabled


def test_a_failed_record_shows_why_and_no_value(client: Client, file: FileRef) -> None:
    curve = client.submit(CURVE, params={'run': file, 'settings': {'scale': 0.0}})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'curve')

    assert [s for _, _, s in rows(outputs)] == ['failed'] * 4
    assert '**Failure** ZeroDivisionError' in outputs.detail.object
    assert outputs.button.disabled


def test_the_app_lists_a_record_pending_until_a_poll_after_it_finished(
    client: Client, file: FileRef, gate: threading.Event
) -> None:
    gate.clear()
    app = App(client)
    app.form.set({'run': file})

    record = app.submit()
    assert record is not None
    label = app.refresh().labels[record.id]
    assert app.message.object == f'Submitted {label}.'
    assert app.outputs.pending == [record]
    app.outputs.select(record, 'curve')
    assert '**Value**' not in app.outputs.detail.object

    gate.set()
    client.wait(record)
    assert app.outputs.pending == [record]  # until polled
    app.poll()
    assert app.outputs.pending == []
    assert app.message.object == f'Computed {label}.'
    assert app.outputs.selected == (record, 'curve')
    assert '**Value**' in app.outputs.detail.object


def test_a_refresh_keeps_the_plot_of_an_output_whose_status_is_unchanged(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'image')
    outputs.plot()

    outputs.refresh(Listing(client))
    assert outputs.selected == (curve, 'image')
    assert len(outputs.plots.objects) == 1

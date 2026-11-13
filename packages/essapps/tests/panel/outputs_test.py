# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
``CURVE`` outputs a group with a 1-D and a 0-D array, a 2-D array in a group
with a description, the settings it used, and an optional group it never
returns. It waits for ``gate``, so a test holds its record pending, and a
scale of 0 fails it. ``SCALE`` reads the curve. ``CUBE`` outputs a group with
a 4-D array, whose value tells its position, and a 1-D array, and a 3-D array.
"""

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import panel as pn
import plopp as pp
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client, Listing, Record, SpecId, Status, local
from ess.apps.folder import open_folder
from ess.apps.panel import App, Outputs, Records
from ess.apps.panel.records import describe
from ess.apps.plotting import Plot
from ess.apps.testing import FakeFiles, RecordingClient, read_file
from ess.spec import (
    FileRef,
    Referenced,
    RunFile,
    WorkflowSpec,
    function_binding,
)


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


class Image(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    counts: sc.Variable


class Total(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: sc.Variable


class CurveParams(BaseModel):
    run: RunFile
    settings: Settings = Field(default_factory=Settings)


class CurveOutputs(BaseModel):
    curve: Curve
    image: Image = Field(description='Counts per pixel.')
    used: Settings
    extra: Image | None = None


class Cube(BaseModel):
    """Counts by time, layer, y, and x, and by x only."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    counts: sc.DataArray
    profile: sc.DataArray


class CubeParams(BaseModel):
    settings: Settings = Field(default_factory=Settings)


class CubeOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    cube: Cube
    volume: sc.DataArray


class ScaleParams(BaseModel):
    curve: Referenced[Curve]


class ScaleOutputs(BaseModel):
    total: Total


class FrameParams(BaseModel):
    picture: Referenced[Image]


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


FRAME = WorkflowSpec(
    name='frame',
    version=1,
    title='Frame',
    description='The total of an image.',
    params=FrameParams,
    outputs=ScaleOutputs,
)


CUBE = WorkflowSpec(
    name='cube',
    version=1,
    title='Cube',
    description='A cube of counts that tells its position.',
    params=CubeParams,
    outputs=CubeOutputs,
)

CUBE_SIZES = {'time': 4, 'layer': 3, 'y': 5, 'x': 6}


def position(**steps: int) -> float:
    """The value of the cube at the given indices, 0 for a dimension left out."""
    place = {'time': 1000, 'layer': 100, 'y': 10, 'x': 1}
    return float(sum(place[dim] * i for dim, i in steps.items()))


def cube(params: CubeParams) -> CubeOutputs:
    grid = np.indices(list(CUBE_SIZES.values()))
    values = sum(g * position(**{d: 1}) for g, d in zip(grid, CUBE_SIZES, strict=True))
    counts = sc.DataArray(
        sc.array(dims=list(CUBE_SIZES), values=values, unit='counts'),
        coords={d: sc.arange(d, float(n)) for d, n in CUBE_SIZES.items()},
    )
    return CubeOutputs(
        cube=Cube(counts=counts, profile=counts['time', 0]['layer', 0]['y', 0].copy()),
        volume=counts['time', 0].copy(),
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
        counts = sc.array(dims=['x'], values=read_file(params.run)['counts'])
        counts *= 1 / params.settings.scale
        return CurveOutputs(
            curve=Curve(
                counts=sc.DataArray(counts, coords={'x': sc.arange('x', 3.0)}),
                total=counts.sum(),
                bins=3,
            ),
            image=Image(counts=sc.ones(dims=['y', 'x'], shape=[2, 3], unit='counts')),
            used=params.settings,
        )

    def scale(params: ScaleParams) -> ScaleOutputs:
        return ScaleOutputs(total=Total(value=params.curve.total))

    def frame(params: FrameParams) -> ScaleOutputs:
        return ScaleOutputs(total=Total(value=params.picture.counts.sum()))

    bind = [
        function_binding(CURVE, curve),
        function_binding(SCALE, scale),
        function_binding(FRAME, frame),
        function_binding(CUBE, cube),
    ]
    with local(proposal='p1', files=run[0], bind=bind) as client:
        yield client


@pytest.fixture
def file(run: tuple[FakeFiles, FileRef]) -> FileRef:
    return run[1]


def outputs_of(client: Client, *records: Record) -> Outputs:
    client.wait(list(records))
    return Outputs(client, Listing(client))


def rows(outputs: Outputs) -> list[tuple[str, str, str]]:
    table = outputs.table.widget.value
    return list(zip(table['Type'], table['Output'], table['Status'], strict=True))


def at(record: Record) -> str:
    """The time of a label: the local time the record was created."""
    return f'{record.created.astimezone():%H:%M}'


def test_an_output_is_listed_by_its_type_and_time_newest_first(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    outputs = outputs_of(client, curve, scale)

    assert rows(outputs) == [
        ('Total', f'Total ({at(scale)})', 'completed'),
        ('Curve', f'Curve ({at(curve)})', 'completed'),
        ('Image', f'Image ({at(curve)}, image)', 'completed'),
        ('Settings', f'Settings ({at(curve)})', 'completed'),
    ]  # not extra, an Image too, which the workflow did not return
    assert outputs.table.widget.groupby == ['Type']
    outputs.panel()  # builds, with the warnings of Panel as errors


def test_outputs_of_one_type_and_time_are_told_apart_by_their_record(
    client: Client, file: FileRef
) -> None:
    first = client.submit(CURVE, params={'run': file})
    second = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, first, second)

    curves = [label for tp, label, _ in rows(outputs) if tp == 'Curve']
    if at(first) == at(second):
        assert curves == [
            f'Curve ({at(second)}, curve, {second.id[:6]})',
            f'Curve ({at(first)}, curve, {first.id[:6]})',
        ]
    assert len(set(curves)) == 2


def test_the_output_selected_shows_what_it_is_and_its_value(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)

    outputs.select(curve, 'curve')
    detail = outputs.detail.object
    assert detail.startswith(f'### Curve ({at(curve)}): completed')
    assert 'Counts per bin, and their total.' in detail
    assert 'The total is' not in detail  # only the first paragraph
    assert f'**Record** Curve ({at(curve)})' in detail
    assert '- counts: array (x: 3) [dimensionless], coordinates x' in detail
    assert '- total: 6.0 [dimensionless]' in detail
    assert '- bins: 3' in detail

    outputs.select(curve, 'image')
    assert 'Counts per pixel.' in outputs.detail.object


def test_the_plot_has_a_figure_per_array_of_one_or_two_dimensions(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)

    outputs.select(curve, 'curve')
    figures = outputs.view.objects
    assert len(figures) == 1  # not the 0-D total
    assert isinstance(figures[0], pn.pane.Matplotlib)

    outputs.select(curve, 'image')
    assert isinstance(outputs.view.objects[0], pn.pane.Matplotlib)


def test_an_output_without_an_array_is_shown_but_not_plotted(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file, 'settings': {'scale': 2.0}})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'used')

    assert '- scale: 2.0' in outputs.detail.object
    assert outputs.view.objects == []


def test_a_failed_record_lists_its_outputs_as_failed(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file, 'settings': {'scale': 0.0}})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'curve')

    assert [s for _, _, s in rows(outputs)] == ['failed'] * 4
    assert '**Failure** ZeroDivisionError' in outputs.detail.object
    assert '**Value**' not in outputs.detail.object
    assert outputs.view.objects == []


def test_the_provenance_graph_shows_each_record_read_and_their_files(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    outputs = outputs_of(client, curve, scale)
    outputs.select(scale, 'total')
    outputs.views.value = 'Provenance'

    graph = outputs.graph
    assert graph.nodes == [file, curve, scale]
    assert graph.edges == [(curve, scale, 'curve'), (file, curve, 'run')]
    assert outputs.view.objects  # the graph, with the node picked below it
    listing = Listing(client)
    assert graph.detail.object == describe(client, listing, scale)
    graph.pick(curve)
    assert graph.detail.object == describe(client, listing, curve)
    graph.pick(file)
    assert graph.detail.object.startswith('### run 101')
    outputs.panel()  # builds, with the warnings of Panel as errors


def test_a_refresh_draws_the_graph_again_and_keeps_the_node_picked(
    client: Client, file: FileRef, gate: threading.Event
) -> None:
    gate.clear()
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    outputs = Outputs(client, Listing(client))
    outputs.views.value = 'Provenance'
    outputs.select(scale, 'total')
    outputs.graph.pick(curve)
    assert ': pending' in outputs.graph.detail.object

    gate.set()
    client.wait([curve, scale])
    outputs.refresh(Listing(client))
    assert outputs.graph.detail.object.startswith(f'### Curve ({at(curve)}): completed')


def test_removing_an_output_releases_its_record_and_stops_listing_it(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'image')
    outputs.remove()

    assert rows(outputs) == []
    assert outputs.selected is None
    assert client.readable(curve) == ()
    assert client.records() == [curve]  # the record stays


def test_removing_a_pending_output_cancels_its_record(
    client: Client, file: FileRef, gate: threading.Event
) -> None:
    gate.clear()
    curve = client.submit(CURVE, params={'run': file})
    outputs = Outputs(client, Listing(client))
    outputs.select(curve, 'curve')
    outputs.remove()
    gate.set()

    assert client.wait(curve) is Status.CANCELLED
    assert rows(outputs) == []


def test_removing_a_failed_output_stops_listing_it(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file, 'settings': {'scale': 0.0}})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'curve')
    outputs.remove()

    assert rows(outputs) == []


def test_a_refresh_keeps_the_plot_while_new_records_come_before_it(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)
    outputs.select(curve, 'image')
    plot = outputs.view.objects[0]

    newer = client.submit(SCALE, params={'curve': curve.ref('curve')})
    client.wait(newer)
    outputs.refresh(Listing(client))
    assert outputs.selected == (curve, 'image')
    assert outputs.view.objects == [plot]


def test_the_app_lists_a_record_pending_until_a_poll_after_it_finished(
    client: Client, file: FileRef, gate: threading.Event
) -> None:
    gate.clear()
    app = App(client)
    app.form.set({'run': file}, Listing(app.client))

    record = app.submit()
    assert record is not None
    label = app.refresh().labels[record.id]
    assert app.message.object == f'Submitted {label}.'
    assert app.refresh().pending == [record]
    app.outputs.select(record, 'curve')
    assert ': pending' in app.outputs.detail.object

    gate.set()
    client.wait(record)
    app.poll()
    assert app.message.object == f'Computed {label}.'
    assert app.outputs.selected == (record, 'curve')
    assert '**Value**' in app.outputs.detail.object


def test_the_records_list_each_record_with_what_its_request_names(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    client.wait([curve, scale])
    listing = Listing(client)
    records = Records(client, listing)

    table = records.table.widget.value
    assert table['Record'].tolist() == [f'Scale ({at(scale)})', f'Curve ({at(curve)})']
    assert table['Reads'].tolist() == [f'Curve ({at(curve)})', 'run 101']
    records.select(curve)
    detail = records.detail.object
    assert detail == describe(client, listing, curve)
    assert '**Spec** Curve (`curve/v1`): The counts of a run.' in detail
    assert '**Reads** run 101' in detail
    assert '"scale": 1.0' in detail
    records.panel()  # builds, with the warnings of Panel as errors


def test_the_summary_of_an_output_reads_no_value(client: Client, file: FileRef) -> None:
    curve = client.submit(CURVE, params={'run': file})
    client.wait(curve)
    recording = RecordingClient(client)
    outputs = Outputs(recording, Listing(client))  # type: ignore[arg-type]

    outputs.select(curve, 'used')  # nothing to plot
    assert recording.outputs == []
    outputs.select(curve, 'curve')

    assert recording.described == [(curve.id, 'used'), (curve.id, 'curve')]
    assert '- counts: array (x: 3)' in outputs.detail.object
    assert recording.outputs == [(curve.id, 'curve', None)]  # the plot only


def test_an_output_released_since_it_was_listed_is_shown_as_not_readable(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    outputs = outputs_of(client, curve)
    client.release(curve)

    outputs.select(curve, 'curve')

    assert '**Value** not readable' in outputs.detail.object
    assert outputs.view.objects == []
    outputs.views.value = 'Provenance'
    assert outputs.view.objects  # the graph still shows


def sliders(outputs: Outputs) -> list[pn.widgets.IntSlider]:
    columns = [v for v in outputs.view.objects if isinstance(v, pn.Column)]
    return [
        o for c in columns for o in c.objects if isinstance(o, pn.widgets.IntSlider)
    ]


def cut(outputs: Outputs, n: int = 0) -> Any:
    """The figure drawn in the nth view with sliders: its title and colour range."""
    column = [v for v in outputs.view.objects if isinstance(v, pn.Column)][n]
    fig = column.objects[-1].object
    return fig.axes[0].get_title(), fig.axes[1].get_ylim()


def drag(slider: pn.widgets.IntSlider, index: int) -> None:
    """
    Drag a slider to ``index`` without releasing it. The event takes the path
    of one from the browser: assigning ``value`` in Python also sets
    ``value_throttled``, as a release does.
    """
    slider._process_events({'value': index})


def move(slider: pn.widgets.IntSlider, index: int) -> None:
    """Drag a slider to ``index`` and release it."""
    drag(slider, index)
    slider._process_events({'value_throttled': index})


def test_an_array_of_more_than_two_dimensions_has_a_slider_per_other_dimension(
    client: Client,
) -> None:
    record = client.compute(CUBE)
    recording = RecordingClient(client)
    outputs = Outputs(recording, Listing(client))  # type: ignore[arg-type]

    outputs.select(record, 'cube')

    assert [s.name for s in sliders(outputs)] == ['time', 'layer']
    assert [s.end for s in sliders(outputs)] == [3, 2]
    assert 'array (time: 4, layer: 3, y: 5, x: 6) [counts]' in outputs.detail.object
    title, (low, high) = cut(outputs, 0)
    assert title.endswith('counts (time=0, layer=0)')
    assert (low, high) == (position(), position(y=4, x=5))


def test_the_slider_reads_only_the_cut_it_draws(client: Client) -> None:
    record = client.compute(CUBE)
    recording = RecordingClient(client)
    outputs = Outputs(recording, Listing(client))  # type: ignore[arg-type]
    outputs.select(record, 'cube')
    time, layer = sliders(outputs)

    drag(time, 3)
    assert len(recording.outputs) == 1  # not until released
    move(time, 3)
    move(layer, 2)

    assert recording.outputs[1:] == [
        (record.id, 'cube', {'time': 3, 'layer': 0}),
        (record.id, 'cube', {'time': 3, 'layer': 2}),
    ]
    assert all(select for _, _, select in recording.outputs)
    title, (low, high) = cut(outputs, 0)
    assert title.endswith('counts (time=3, layer=2)')
    assert (low, high) == (
        position(time=3, layer=2),
        position(time=3, layer=2, y=4, x=5),
    )


def test_a_field_of_one_or_two_dimensions_is_drawn_from_the_first_cut(
    client: Client,
) -> None:
    record = client.compute(CUBE)
    recording = RecordingClient(client)
    outputs = Outputs(recording, Listing(client))  # type: ignore[arg-type]

    outputs.select(record, 'cube')

    plain, with_sliders = outputs.view.objects
    assert isinstance(plain, pn.pane.Matplotlib)
    assert isinstance(with_sliders, pn.Column)
    assert len(recording.outputs) == 1
    assert recording.outputs[0][2] is not None


def test_an_array_output_has_a_slider_for_each_dimension_but_the_last_two(
    client: Client,
) -> None:
    record = client.compute(CUBE)
    recording = RecordingClient(client)
    outputs = Outputs(recording, Listing(client))  # type: ignore[arg-type]

    outputs.select(record, 'volume')

    assert [s.name for s in sliders(outputs)] == ['layer']
    move(sliders(outputs)[0], 2)
    assert recording.outputs == [
        (record.id, 'volume', {'layer': 0}),
        (record.id, 'volume', {'layer': 2}),
    ]
    _, (low, _) = cut(outputs, 0)
    assert low == position(layer=2)


def test_a_slider_over_a_released_value_shows_why_it_cannot_draw(
    client: Client,
) -> None:
    record = client.compute(CUBE)
    outputs = outputs_of(client, record)
    outputs.select(record, 'volume')
    client.release(record)

    move(sliders(outputs)[0], 1)

    assert 'not readable' in outputs.view.objects[0].objects[-1].object


class Seen:
    """The outputs a configured plot was called with, and what it draws."""

    def __init__(self) -> None:
        self.outputs: list[CurveOutputs] = []

    def counts(self, out: CurveOutputs) -> Any:
        self.outputs.append(out)
        return pp.plot(out.curve.counts, title='counts')


def explode(out: CurveOutputs) -> Any:
    raise RuntimeError('no plot today')


def test_plots_configured_for_a_spec_are_views_of_the_outputs_of_its_records(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    client.wait([curve, scale])
    seen = Seen()
    plots = {SpecId.of(CURVE): {'Counts': Plot(seen.counts)}}
    outputs = Outputs(client, Listing(client), plots=plots)
    assert outputs.views.options == ['Plot', 'Provenance']

    outputs.select(curve, 'image')
    assert outputs.views.options == ['Plot', 'Counts', 'Provenance']
    outputs.select(scale, 'total')
    assert outputs.views.options == ['Plot', 'Provenance']
    outputs.select(curve, 'used')
    outputs.views.value = 'Counts'

    (out,) = seen.outputs
    assert isinstance(out, CurveOutputs)
    assert out.curve.counts.sizes == {'x': 3}
    assert out.used == Settings()
    assert out.extra is None  # an optional output that was not returned
    (pane,) = outputs.view.objects
    assert isinstance(pane, pn.pane.Matplotlib)
    assert pane.object.axes[0].get_title() == 'counts'


def test_a_view_the_record_does_not_offer_falls_back_to_the_plot(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    scale = client.submit(SCALE, params={'curve': curve.ref('curve')})
    client.wait([curve, scale])
    plots = {SpecId.of(CURVE): {'Counts': Plot(Seen().counts)}}
    outputs = Outputs(client, Listing(client), plots=plots)
    outputs.select(curve, 'curve')
    outputs.views.value = 'Counts'

    outputs.select(scale, 'total')

    assert outputs.views.value == 'Plot'
    outputs.select(curve, 'curve')  # the pick stays where it is offered
    outputs.views.value = 'Provenance'
    outputs.select(curve, 'image')
    assert outputs.views.value == 'Provenance'


def test_a_configured_plot_that_fails_shows_why_as_text(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    client.wait(curve)
    outputs = Outputs(
        client, Listing(client), plots={SpecId.of(CURVE): {'Bad': Plot(explode)}}
    )
    outputs.select(curve, 'curve')

    outputs.views.value = 'Bad'

    (text,) = outputs.view.objects
    assert 'Bad failed' in text.object
    assert 'RuntimeError: no plot today' in text.object


def draw_picture(out: ScaleOutputs, *, picture: Image) -> Any:
    return pp.plot(picture.counts, title=str(picture.counts.sum().value))


@pytest.fixture
def frame_view(client: Client, file: FileRef) -> tuple[Outputs, Record, Record]:
    """The outputs with a view of a frame that draws the image it referenced."""
    curve = client.submit(CURVE, params={'run': file})
    frame = client.submit(FRAME, params={'picture': curve.ref('image')})
    client.wait([curve, frame])
    plots = {SpecId.of(FRAME): {'Picture': Plot(draw_picture, inputs=('picture',))}}
    return Outputs(client, Listing(client), plots=plots), curve, frame


def test_a_configured_plot_takes_the_value_its_record_referenced(
    client: Client, frame_view: tuple[Outputs, Record, Record]
) -> None:
    outputs, curve, frame = frame_view
    outputs.select(frame, 'total')

    outputs.views.value = 'Picture'

    (pane,) = outputs.view.objects
    assert isinstance(pane, pn.pane.Matplotlib), pane.object
    total = client.output(curve, 'image').counts.sum().value
    assert pane.object.axes[0].get_title() == str(total)


def test_a_configured_plot_whose_input_is_released_says_what_to_do(
    client: Client, frame_view: tuple[Outputs, Record, Record]
) -> None:
    outputs, curve, frame = frame_view
    client.release(curve)
    outputs.select(frame, 'total')

    outputs.views.value = 'Picture'

    (text,) = outputs.view.objects
    assert 'Picture failed' in text.object
    assert 'NotReadable: picture is no longer available' in text.object
    assert 'Compute it again' in text.object


def test_a_configured_plot_input_must_reference_an_output(
    client: Client, file: FileRef
) -> None:
    curve = client.submit(CURVE, params={'run': file})
    client.wait(curve)
    plots = {SpecId.of(CURVE): {'Counts': Plot(Seen().counts, inputs=('run',))}}
    outputs = Outputs(client, Listing(client), plots=plots)
    outputs.select(curve, 'curve')

    outputs.views.value = 'Counts'

    (text,) = outputs.view.objects
    assert 'run does not reference a whole output' in text.object


def test_a_configured_plot_cannot_take_the_name_of_a_view_of_every_record() -> None:
    plots = {SpecId.of(CURVE): {'Plot': Plot(explode)}}
    with pytest.raises(ValueError, match="curve/v1: \\['Plot'\\]"):
        Outputs(None, None, plots=plots)  # type: ignore[arg-type]


class Block(BaseModel):
    """``CubeOutputs`` after ``volume`` was renamed, under the same version."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    cube: Cube
    block: sc.DataArray


def block(params: CubeParams) -> Block:
    outputs = cube(params)
    return Block(cube=outputs.cube, block=outputs.volume)


@pytest.fixture
def stored_cube(tmp_path: Path) -> Iterator[tuple[Client, Record]]:
    """
    A client of a folder with a persisted record of ``CUBE``, opened again with
    other outputs bound under the same version: a record of another spec.
    """
    (tmp_path / 'files.csv').write_text('file,run\n')
    with open_folder(tmp_path, [function_binding(CUBE, cube)]) as client:
        record = client.compute(CUBE, persist=True)
    renamed = WorkflowSpec(**{**dict(CUBE), 'outputs': Block})
    with open_folder(tmp_path, [function_binding(renamed, block)]) as client:
        yield client, record


def test_a_record_of_another_spec_lists_its_outputs_by_name(
    stored_cube: tuple[Client, Record],
) -> None:
    client, record = stored_cube
    plots = {SpecId.of(CUBE): {'Counts': Plot(explode)}}

    listing = Listing(client)
    outputs = Outputs(client, listing, plots=plots)

    spec = '**Spec** `cube/v1`, another spec than the one bound'
    assert spec in describe(client, listing, record)
    assert rows(outputs) == [
        ('cube', f'cube ({at(record)})', 'completed'),
        ('volume', f'volume ({at(record)})', 'completed'),
    ]
    outputs.select(record, 'cube')
    assert outputs.views.options == ['Plot', 'Provenance']  # no configured plot
    outputs.panel()  # builds, with the warnings of Panel as errors


def test_a_group_of_a_record_of_another_spec_is_drawn_from_the_stored_data(
    stored_cube: tuple[Client, Record],
) -> None:
    client, record = stored_cube
    outputs = Outputs(client, Listing(client))

    outputs.select(record, 'cube')

    assert outputs.detail.object.startswith(f'### cube ({at(record)}): completed')
    assert 'array (time: 4, layer: 3, y: 5, x: 6) [counts]' in outputs.detail.object
    plain, with_sliders = outputs.view.objects
    assert isinstance(plain, pn.pane.Matplotlib)  # the profile
    assert isinstance(with_sliders, pn.Column)  # the counts
    move(sliders(outputs)[0], 3)
    title, (low, _) = cut(outputs, 0)
    assert title.endswith('counts (time=3, layer=0)')
    assert low == position(time=3)


def test_an_array_of_a_record_of_another_spec_is_drawn_from_the_stored_data(
    stored_cube: tuple[Client, Record],
) -> None:
    client, record = stored_cube
    outputs = Outputs(client, Listing(client))

    outputs.select(record, 'volume')

    assert '- volume: array (layer: 3, y: 5, x: 6) [counts]' in outputs.detail.object
    move(sliders(outputs)[0], 2)
    _, (low, _) = cut(outputs, 0)
    assert low == position(layer=2)


def test_the_app_passes_its_plots_to_the_outputs(client: Client, file: FileRef) -> None:
    curve = client.submit(CURVE, params={'run': file})
    client.wait(curve)
    seen = Seen()
    app = App(client, plots={SpecId.of(CURVE): {'Counts': Plot(seen.counts)}})
    app.poll()

    app.outputs.select(curve, 'curve')
    app.outputs.views.value = 'Counts'

    assert len(seen.outputs) == 1

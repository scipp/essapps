# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
``CURVE`` outputs a group with a 1-D and a 0-D array, a 2-D array in a group
with a description, the settings it used, and an optional group it never
returns. It waits for ``gate``, so a test holds its record pending, and a
scale of 0 fails it. ``SCALE`` reads the curve.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import panel as pn
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client, Record, Status, local
from ess.apps.panel import App, Listing, Outputs, Records
from ess.apps.panel.records import describe
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


class Image(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    counts: sc.Variable


class Total(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: sc.Variable


class CurveParams(BaseModel):
    run: Run
    settings: Settings = Field(default_factory=Settings)


class CurveOutputs(BaseModel):
    curve: Curve
    image: Image = Field(description='Counts per pixel.')
    used: Settings
    extra: Image | None = None


class ScaleParams(BaseModel):
    curve: Curve


class ScaleOutputs(BaseModel):
    total: Total


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
            image=Image(counts=sc.ones(dims=['y', 'x'], shape=[2, 3], unit='counts')),
            used=params.settings,
        )

    def scale(params: ScaleParams) -> ScaleOutputs:
        return ScaleOutputs(total=Total(value=params.curve.total))

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
    assert isinstance(figures[0], pn.pane.Matplotlib)
    assert figures[1].object == f'Curve ({at(curve)}) total: 0-D, not plotted'

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
    app.form.set({'run': file})

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

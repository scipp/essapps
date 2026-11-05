# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
``LINES`` outputs a group with three arrays along x, two of counts and one of
metres, one along t, and an image. It waits for ``gate``.
"""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict

from ess.apps import Backend, Client, Listing, Record
from ess.apps.panel import App, Overlay
from ess.apps.testing import FakeFiles
from ess.spec import WorkflowSpec, function_binding


class Lines(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    first: sc.DataArray
    second: sc.DataArray
    metres: sc.DataArray
    over_time: sc.DataArray
    image: sc.DataArray


class Params(BaseModel):
    scale: float = 1.0


class LinesOutputs(BaseModel):
    lines: Lines


LINES = WorkflowSpec(
    name='lines',
    version=1,
    title='Lines',
    description='Arrays of several shapes.',
    params=Params,
    outputs=LinesOutputs,
)


def line(dim: str, scale: float = 1.0, unit: str = 'counts') -> sc.DataArray:
    return sc.DataArray(
        sc.arange(dim, 4.0, unit=unit) * scale,
        coords={dim: sc.arange(dim, 4.0, unit='m')},
    )


@pytest.fixture
def gate() -> threading.Event:
    opened = threading.Event()
    opened.set()
    return opened


@pytest.fixture
def client(tmp_path: Path, gate: threading.Event) -> Iterator[Client]:
    def lines(params: Params) -> LinesOutputs:
        gate.wait()
        return LinesOutputs(
            lines=Lines(
                first=line('x', params.scale),
                second=line('x', 2 * params.scale),
                metres=line('x', unit='m'),
                over_time=line('t'),
                image=sc.DataArray(sc.ones(dims=['y', 'x'], shape=[2, 4])),
            )
        )

    backend = Backend(FakeFiles('p1', tmp_path), [function_binding(LINES, lines)])
    yield Client(backend, proposal='p1', submitter='anna')
    gate.set()  # a test that failed may leave a record pending
    backend.close()


def key(record: Record, field: str) -> tuple[str, str, str]:
    """What the choice holds for a field of the output of a record."""
    return (record.id, 'lines', field)


def drawn(overlay: Overlay) -> dict[str, list[float]]:
    """The lines of the figure shown, by name."""
    (pane,) = overlay.view.objects
    (axes,) = pane.object.axes
    return {
        ln.get_label(): ln.get_ydata().tolist() for ln in axes.lines if ln.get_label()
    }


def test_the_1d_arrays_of_completed_outputs_are_offered(
    client: Client, gate: threading.Event
) -> None:
    done = client.compute(LINES)
    gate.clear()
    pending = client.submit(LINES)
    overlay = Overlay(client, Listing(client))

    assert list(overlay.choice.options.values()) == [
        key(done, f) for f in ('first', 'second', 'metres', 'over_time')
    ]  # not the image, nor anything of the pending record
    assert overlay.view.objects == []

    gate.set()
    client.wait(pending)
    overlay.refresh(Listing(client))
    assert len(overlay.choice.options) == 8


def test_one_line_is_drawn_for_each_choice(client: Client) -> None:
    record = client.compute(LINES, params={'scale': 3.0})
    overlay = Overlay(client, Listing(client))
    labels = {k: label for label, k in overlay.choice.options.items()}

    overlay.choice.value = [key(record, 'first')]
    assert drawn(overlay) == {labels[key(record, 'first')]: [0.0, 3.0, 6.0, 9.0]}

    both = [key(record, 'first'), key(record, 'second')]
    overlay.choice.value = both
    assert list(drawn(overlay)) == [labels[k] for k in both]


def test_once_one_is_chosen_only_arrays_along_its_dimension_are_offered(
    client: Client,
) -> None:
    record = client.compute(LINES)
    overlay = Overlay(client, Listing(client))

    overlay.choice.value = [key(record, 'over_time')]
    assert list(overlay.choice.options.values()) == [key(record, 'over_time')]

    overlay.choice.value = []
    assert len(overlay.choice.options) == 4


def test_the_log_checkbox_sets_the_scale_of_the_axis(client: Client) -> None:
    record = client.compute(LINES)
    overlay = Overlay(client, Listing(client))
    overlay.choice.value = [key(record, 'first')]
    assert overlay.view.objects[0].object.axes[0].get_yscale() == 'linear'

    overlay.log.value = True

    assert overlay.view.objects[0].object.axes[0].get_yscale() == 'log'


def test_a_refresh_keeps_the_choice_that_is_still_offered(client: Client) -> None:
    first = client.compute(LINES)
    overlay = Overlay(client, Listing(client))
    overlay.choice.value = [key(first, 'first')]
    figure = overlay.view.objects[0]

    overlay.refresh(Listing(client))  # nothing new
    assert overlay.view.objects == [figure]

    second = client.compute(LINES)
    overlay.refresh(Listing(client))

    assert overlay.choice.value == [key(first, 'first')]
    assert key(second, 'first') in overlay.choice.options.values()
    labels = {k: label for label, k in overlay.choice.options.items()}
    assert list(drawn(overlay)) == [labels[key(first, 'first')]]  # its current label


def test_a_refresh_drops_a_choice_that_is_no_longer_offered(client: Client) -> None:
    kept = client.compute(LINES)
    gone = client.compute(LINES)
    overlay = Overlay(client, Listing(client))
    choice = [key(kept, 'first'), key(gone, 'second')]
    overlay.choice.value = choice
    assert len(drawn(overlay)) == 2

    client.release(gone)
    overlay.refresh(Listing(client))

    assert overlay.choice.value == choice[:1]
    assert len(drawn(overlay)) == 1


def test_choices_that_cannot_share_a_plot_show_why(client: Client) -> None:
    record = client.compute(LINES)
    overlay = Overlay(client, Listing(client))

    overlay.choice.value = [key(record, 'first'), key(record, 'metres')]

    (text,) = overlay.view.objects
    assert text.object.startswith('Cannot draw these:')


def test_an_output_released_by_another_client_shows_why_it_cannot_be_read(
    client: Client,
) -> None:
    record = client.compute(LINES)
    overlay = Overlay(client, Listing(client))
    client.release(record)  # the listing does not know

    overlay.choice.value = [key(record, 'first')]

    (text,) = overlay.view.objects
    assert 'Cannot draw these:' in text.object


def test_the_app_has_a_tab_that_compares_outputs(client: Client) -> None:
    record = client.compute(LINES)
    app = App(client)
    app.poll()

    assert key(record, 'first') in app.overlay.choice.options.values()
    tabs = app.panel().objects[-1]
    assert tabs._names == ['Outputs', 'Compare', 'Records']

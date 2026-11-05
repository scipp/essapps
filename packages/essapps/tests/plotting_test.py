# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What an application decides about plots, without a widget library."""

import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Backend, Client, Listing
from ess.apps.plotting import Lines, overlay, same_dim, slider_dims
from ess.apps.testing import FakeFiles
from ess.spec import WorkflowSpec, function_binding


class Settings(BaseModel):
    scale: float = 1.0


class Spectra(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    first: sc.DataArray  # along x
    second: sc.DataArray  # along x
    over_time: sc.DataArray
    image: sc.DataArray
    peak: sc.Variable
    note: str


class Params(BaseModel):
    settings: Settings = Field(default_factory=Settings)


class Outputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    spectra: Spectra
    profile: sc.DataArray
    centre: sc.Variable
    extra: Spectra | None = None


SPECTRA = WorkflowSpec(
    name='spectra',
    version=1,
    title='Spectra',
    description='Arrays of several shapes.',
    params=Params,
    outputs=Outputs,
)


def line(dim: str, n: int = 4, scale: float = 1.0) -> sc.DataArray:
    return sc.DataArray(
        sc.arange(dim, float(n), unit='counts') * scale,
        coords={dim: sc.arange(dim, float(n), unit='m')},
    )


@pytest.fixture
def gate() -> threading.Event:
    opened = threading.Event()
    opened.set()
    return opened


@pytest.fixture
def backend(tmp_path: Path, gate: threading.Event) -> Iterator[Backend]:
    def spectra(params: Params) -> Outputs:
        gate.wait()
        if params.settings.scale == 0:
            raise ZeroDivisionError('scale 0')
        scale = params.settings.scale
        image = sc.DataArray(sc.ones(dims=['y', 'x'], shape=[2, 4], unit='counts'))
        return Outputs(
            spectra=Spectra(
                first=line('x', scale=scale),
                second=line('x', scale=2 * scale),
                over_time=line('t', 3),
                image=image,
                peak=sc.scalar(3.0),
                note='a',
            ),
            profile=line('x', scale=3 * scale),
            centre=sc.scalar(1.0, unit='m'),
        )

    backend = Backend(FakeFiles('p1', tmp_path), [function_binding(SPECTRA, spectra)])
    yield backend
    gate.set()  # a test that failed may leave a record pending
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def test_every_dimension_but_the_last_two_has_a_slider() -> None:
    assert slider_dims({'time': 50, 'layer': 20, 'y': 200, 'x': 200}) == [
        'time',
        'layer',
    ]
    assert slider_dims({'layer': 2, 'y': 3, 'x': 4}) == ['layer']
    assert slider_dims({'y': 3, 'x': 4}) == []
    assert slider_dims({'x': 4}) == []
    assert slider_dims({}) == []


def test_the_arrays_offered_are_the_1d_fields_of_completed_outputs(
    client: Client,
) -> None:
    record = client.compute(SPECTRA)
    label = Listing(client).output_label(record.ref('spectra'))
    profile = Listing(client).output_label(record.ref('profile'))

    offered = Lines(client).of(Listing(client))

    assert {ln.label: (ln.output, ln.field, ln.dim) for ln in offered.values()} == {
        f'{label} first': ('spectra', 'first', 'x'),
        f'{label} second': ('spectra', 'second', 'x'),
        f'{label} over_time': ('spectra', 'over_time', 't'),
        profile: ('profile', None, 'x'),
    }  # not the image, peak, centre, or extra, which was not returned
    assert all(key == ln.key for key, ln in offered.items())


def test_a_line_reads_its_array(client: Client) -> None:
    record = client.compute(SPECTRA, params={'settings': {'scale': 2.0}})
    lines = Lines(client)
    offered = lines.of(Listing(client))

    first = offered[(record.id, 'spectra', 'first')]
    profile = offered[(record.id, 'profile', None)]
    assert sc.identical(lines.read(first), line('x', scale=2.0))
    assert sc.identical(lines.read(profile), line('x', scale=6.0))


def test_arrays_of_pending_and_failed_records_are_not_offered(
    client: Client, gate: threading.Event
) -> None:
    gate.clear()
    pending = client.submit(SPECTRA)
    failed = client.submit(SPECTRA, params={'settings': {'scale': 0.0}})
    lines = Lines(client)
    assert lines.of(Listing(client)) == {}

    gate.set()
    client.wait([pending, failed])
    offered = lines.of(Listing(client))

    assert {ln.record.id for ln in offered.values()} == {pending.id}


def test_an_output_released_since_it_was_described_is_not_offered(
    client: Client,
) -> None:
    kept = client.compute(SPECTRA)
    released = client.compute(SPECTRA)
    listing = Listing(client)
    lines = Lines(client)
    assert {ln.record.id for ln in lines.of(listing).values()} == {
        kept.id,
        released.id,
    }

    client.release(released)

    assert {ln.record.id for ln in Lines(client).of(listing).values()} == {kept.id}


def test_once_one_is_chosen_only_arrays_along_its_dimension_are_offered(
    client: Client,
) -> None:
    client.compute(SPECTRA)
    offered = Lines(client).of(Listing(client))
    along_t = next(key for key, ln in offered.items() if ln.dim == 't')
    along_x = [key for key, ln in offered.items() if ln.dim == 'x']

    assert same_dim(offered, []) == offered
    assert list(same_dim(offered, [along_t])) == [along_t]
    assert list(same_dim(offered, along_x[:1])) == along_x
    assert (
        same_dim(offered, [('gone', 'x', None)]) == offered
    )  # a choice no longer offered


def test_the_overlay_has_a_line_for_each_array() -> None:
    arrays = {'one': line('x'), 'two': line('x', scale=3.0)}

    axes = overlay(arrays).fig.axes[0]

    lines = {ln.get_label(): ln.get_ydata() for ln in axes.lines if ln.get_label()}
    assert list(lines) == ['one', 'two']
    assert lines['two'].tolist() == [0.0, 3.0, 6.0, 9.0]
    assert axes.get_yscale() == 'linear'
    assert overlay(arrays, log=True).fig.axes[0].get_yscale() == 'log'


def test_a_missing_plopp_is_named_with_the_extra_that_provides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, 'plopp', None)  # as if not installed

    with pytest.raises(ImportError, match=r"essapps\[panel\]"):
        overlay({'one': line('x')})


def test_what_an_application_decides_about_plots_needs_no_panel() -> None:
    code = (
        'import sys; import ess.apps, ess.apps.plotting, ess.apps.listing;'
        "assert 'panel' not in sys.modules and 'plopp' not in sys.modules"
    )
    subprocess.run(  # noqa: S603
        [sys.executable, '-I', '-c', code], check=True
    )

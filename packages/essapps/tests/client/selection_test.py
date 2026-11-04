# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Selections of outputs."""

from collections.abc import Iterator

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, Field

from ess.apps import Accumulator, Client, Record, Template, file, local
from ess.apps.testing import FakeFiles, read_file
from ess.spec import Referenced, function_binding

from . import toys


class Axis(toys.Data):
    """The bin edges of a grid along x: a group in a group."""

    edges: sc.Variable


class Grid(toys.Data):
    """Counts on an x-y grid, their sum over y, its x axis, and a label."""

    counts: sc.Variable
    profile: sc.Variable
    axis: Axis
    label: str


class Label(BaseModel):
    text: str


class GridOutputs(toys.Data):
    grid: Grid
    background: sc.Variable  # an array output, which no request references
    label: Label
    peak: float


class GridParts(toys.Data):
    parts: list[Referenced[Grid]] = Field(min_length=1)


class GridTotal(toys.Data):
    grid: Grid


GRID = toys.make_spec('grid', params=toys.Sample, outputs=GridOutputs)


GRID_TOTAL = toys.make_spec(
    'grid-total', params=GridParts, outputs=GridTotal, tables=('parts',)
)


def grid(params: toys.Sample) -> GridOutputs:
    values = np.arange(6.0).reshape(3, 2) * read_file(params.sample)
    counts = sc.array(dims=['x', 'y'], values=values)
    axis = Axis(edges=sc.array(dims=['x'], values=[0.0, 1.0, 2.0, 3.0]))
    return GridOutputs(
        grid=Grid(counts=counts, profile=counts.sum('y'), axis=axis, label='a'),
        background=sc.array(dims=['x', 'y'], values=np.arange(6.0).reshape(3, 2) + 10),
        label=Label(text='a'),
        peak=counts.max().value,
    )


def _add_grids(total: Grid, part: Grid) -> Grid:
    return Grid(
        counts=total.counts + part.counts,
        profile=total.profile + part.profile,
        axis=total.axis,
        label=total.label,
    )


@pytest.fixture
def grids(files: FakeFiles) -> Iterator[Client]:
    """A client of a backend with GRID, a 3x2 grid per run, and GRID_TOTAL."""
    bind = [function_binding(GRID, grid), toys.folding(GRID_TOTAL, _add_grids)]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


def _grid(client: Client, n: int) -> Record:
    return client.compute(GRID, params={'sample': file(run=n)})


def _grid_total(client: Client) -> Accumulator:
    return client.accumulator(Template(GRID_TOTAL, blanks=('parts',)))


def test_a_selection_copies_part_of_an_output_and_makes_no_record(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)
    row = client.output(record, 'background', select={'x': 1})
    corner = client.output(record, 'background', select={'x': slice(0, 2), 'y': 1})
    row.values[0] = 99.0

    assert row.dims == ('y',)
    assert corner.values.tolist() == [11.0, 13.0]
    assert client.output(record, 'background')['x', 1].values.tolist() == [12.0, 13.0]
    assert client.records() == [record]


def test_a_selection_of_a_group_slices_its_arrays_and_copies_its_other_fields(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)
    row = client.output(record, 'grid', select={'x': 1})
    column = client.output(record, 'grid', select={'y': 1})
    selected = (
        row.counts.values.tolist(),
        row.profile.value,
        row.axis.edges.values.tolist(),  # has x, yet not sliced: a group in it
        row.label,
    )
    row.counts.values[0] = 99.0
    row.axis.edges.values[0] = 99.0

    assert selected == ([2.0, 3.0], 5.0, [0.0, 1.0, 2.0, 3.0], 'a')
    assert column.counts.values.tolist() == [1.0, 3.0, 5.0]
    assert column.profile.values.tolist() == [1.0, 5.0, 9.0]  # has no y: whole
    again = client.output(record, 'grid', select={'x': 1})
    assert again.counts.values.tolist() == [2.0, 3.0]
    assert again.axis.edges.values.tolist() == [0.0, 1.0, 2.0, 3.0]


def test_a_selection_of_an_accumulator_copies_part_of_its_current_state(
    grids: Client,
) -> None:
    client = grids
    runs = [_grid(client, n) for n in (1, 2)]
    total = _grid_total(client)
    cuts = []
    for run in runs:
        total.push(parts=[run.ref('grid')])
        cuts.append(client.output(total, 'grid', select={'y': 1}))

    assert [c.counts.values.tolist() for c in cuts] == [
        [1.0, 3.0, 5.0],
        [3.0, 9.0, 15.0],
    ]
    assert client.records() == runs


def test_a_selection_of_a_group_by_a_dimension_no_array_has_is_refused(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)
    total = _grid_total(client)
    total.push(parts=[record.ref('grid')])

    for reader in (record, total):
        with pytest.raises(
            ValueError, match=r"^Grid has no array field with dimension 'z'$"
        ):
            client.output(reader, 'grid', select={'z': 0})


def test_a_selection_is_refused_for_an_output_that_declares_no_array(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)

    for name in ('label', 'peak'):  # a group without an array, and a number
        with pytest.raises(
            ValueError, match='needs an array, or a group with an array'
        ):
            client.output(record, name, select={'x': 0})
    with pytest.raises(ValueError, match='name of an output'):
        client.output(record, select={'x': 0})


def test_a_selection_the_output_cannot_take_raises_what_the_slice_raises(
    grids: Client,
) -> None:
    client = grids
    record, other = _grid(client, 1), _grid(client, 2)
    total = _grid_total(client)
    total.push(parts=[record.ref('grid')])

    with pytest.raises(IndexError):
        client.output(record, 'grid', select={'x': 3})
    with pytest.raises(IndexError):
        client.output(total, 'grid', select={'x': 3})
    with pytest.raises(sc.DimensionError):
        client.output(record, 'background', select={'z': 0})
    total.push(parts=[other.ref('grid')])  # the reads let go of the state
    selected = client.output(total, 'grid', select={'x': 2})
    assert selected.counts.values.tolist() == [12.0, 15.0]

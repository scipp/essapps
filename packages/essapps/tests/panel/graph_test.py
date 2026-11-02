# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from itertools import combinations, pairwise

from ess.apps.panel.graph import Point, layout


def crossings(paths: list[list[Point]]) -> int:
    """The number of pairs of segments, between the same columns, that cross."""
    segments = [s for path in paths for s in pairwise(path)]
    return sum(
        (a[0][1] - b[0][1]) * (a[1][1] - b[1][1]) < 0
        for a, b in combinations(segments, 2)
        if a[0][0] == b[0][0]
    )


def test_an_iofq_from_records_has_no_crossings_nor_edges_through_nodes() -> None:
    # As the graph of an I(Q) lists its nodes: its files, then its records,
    # furthest first; a straight line from run 101 to I(Q) would pass
    # through Transmission, and run 120 to Transmission would cross run 101
    # to Beam centre.
    column = {'101': 0, '110': 0, '120': 0, 'trans': 1, 'centre': 1, 'iofq': 2}
    edges = [
        ('120', 'trans'),
        ('101', 'centre'),
        ('101', 'iofq'),
        ('110', 'iofq'),
        ('centre', 'iofq'),
        ('trans', 'iofq'),
    ]
    at, paths = layout(column, edges)

    assert crossings(paths) == 0
    bends = [p for path in paths for p in path[1:-1]]
    assert len(bends) == 2  # 101 and 110 to I(Q), past column 1
    assert not set(bends) & set(at.values())
    assert [path[0] for path in paths] == [at[a] for a, _ in edges]
    assert [path[-1] for path in paths] == [at[b] for _, b in edges]


def test_a_column_is_centred() -> None:
    at, paths = layout({'a': 0, 'b': 0, 'c': 1}, [('a', 'c'), ('b', 'c')])

    assert sorted(y for _, y in (at['a'], at['b'])) == [-0.5, 0.5]
    assert at['c'] == (1.0, 0.0)
    assert len(paths) == 2

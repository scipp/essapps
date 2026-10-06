# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import operator

import pytest

from ess.spec import combine


def test_a_plain_request_combines_the_rows_of_its_table() -> None:
    function = combine(operator.add).stage({}, ())
    assert function(parts=[{'a': 1}, {'a': 2}, {'a': 3}]) == {'a': 6}


def test_the_held_state_combines_a_copy_of_the_first_row() -> None:
    first = {'a': [1]}
    held = combine(operator.iadd).held_state({})
    held.push({'parts': first})
    held.push({'parts': {'a': [2]}})

    assert held.outputs() == {'a': [1, 2]}
    assert first == {'a': [1]}


def test_combine_takes_only_one_table() -> None:
    with pytest.raises(TypeError, match=r"not also \['scale'\]"):
        combine(operator.add).held_state({'scale': 2.0})
    held = combine(operator.add).held_state({})
    held.push({'parts': {'value': 1.0}})
    with pytest.raises(ValueError, match='one table, not parts and others'):
        held.push({'others': {'value': 1.0}})

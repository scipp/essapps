# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import pytest

from ess.apps.binding import Registry
from ess.apps.examples import LOAD, load_workflow
from ess.apps.spec import SpecId


def test_registry_knows_specs_and_hands_out_factories_on_request() -> None:
    calls = []

    def factory():
        calls.append(1)
        return load_workflow()

    registry = Registry()
    registry.bind(LOAD, factory)
    assert LOAD.id in registry
    assert registry.spec(LOAD.id) is LOAD
    assert calls == []
    binding = registry.binding(LOAD.id)
    assert binding.how == 'in_process'
    assert binding.factory is factory
    with pytest.raises(KeyError, match='No spec'):
        registry.spec(SpecId(name='nope', version=1))

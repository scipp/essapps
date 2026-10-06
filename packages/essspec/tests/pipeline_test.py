# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from typing import NewType

import pytest
import sciline

from ess.spec.pipeline import PipelineBinding

Loaded = NewType('Loaded', float)
Offset = NewType('Offset', float)
Shifted = NewType('Shifted', float)


def shift(loaded: Loaded, offset: Offset) -> Shifted:
    return Shifted(loaded + offset)


@pytest.fixture
def binding() -> PipelineBinding:
    return PipelineBinding(
        sciline.Pipeline([shift]),
        params={'loaded': Loaded, 'offset': Offset},
        outputs={'value': Shifted},
    )


def test_a_plain_request_returns_the_outputs_by_field_name(
    binding: PipelineBinding,
) -> None:
    assert binding.stage({'loaded': 10.0, 'offset': 0.5}, ())() == {'value': 10.5}


def test_a_stage_with_a_blank_gives_the_plain_requests_outputs(
    binding: PipelineBinding,
) -> None:
    plain = binding.stage({'loaded': 10.0, 'offset': 0.5}, ())()
    staged = binding.stage({'loaded': 10.0}, ('offset',))(offset=0.5)
    assert staged == plain


def test_an_unknown_parameter_is_refused(binding: PipelineBinding) -> None:
    with pytest.raises(ValueError, match='no sciline key'):
        binding.stage({'speed': 2.0}, ())

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


Settings = NewType('Settings', dict)


@pytest.fixture
def binding() -> PipelineBinding:
    return PipelineBinding(
        sciline.Pipeline([shift]),
        params={'run.loaded': Loaded, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )


def test_a_plain_request_returns_the_outputs_by_field_name(
    binding: PipelineBinding,
) -> None:
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.stage(values, ())() == {'value': 10.5}


def test_a_stage_with_a_blank_group_gives_the_plain_requests_outputs(
    binding: PipelineBinding,
) -> None:
    plain = binding.stage({'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}, ())()
    staged = binding.stage({'run': {'loaded': 10.0}}, ('shift',))
    assert staged(shift={'offset': 0.5}) == plain


def test_a_group_mapped_as_a_whole_sets_its_values() -> None:
    def from_settings(settings: Settings) -> Offset:
        return Offset(settings['offset'])

    binding = PipelineBinding(
        sciline.Pipeline([shift, from_settings]),
        params={'run.loaded': Loaded, 'shift': Settings},
        outputs={'value': Shifted},
    )
    values = {'run': {'loaded': 10.0}, 'shift': {'offset': 0.5}}
    assert binding.stage(values, ())() == {'value': 10.5}


@pytest.mark.parametrize(
    ('fixed', 'blanks'),
    [
        ({'speed': 2.0}, ()),
        ({'run': {'loaded': 10.0, 'speed': 2.0}}, ()),
        ({'run': {'loaded': 10.0}}, ('speed',)),
    ],
)
def test_an_unknown_parameter_is_refused(
    binding: PipelineBinding, fixed: dict, blanks: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match='no sciline key'):
        binding.stage(fixed, blanks)

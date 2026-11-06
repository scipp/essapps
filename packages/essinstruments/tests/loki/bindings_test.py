# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The bindings on the runs of the LoKI detector test, against plain esssans.
"""

from pathlib import Path

import numpy as np
import pytest
import scipp as sc
from ess.instruments.loki import bindings
from ess.instruments.loki.specs import (
    BeamCentre,
    BeamCentreParams,
    IofQParams,
    MasksOutputs,
    MasksParams,
    PixelMasks,
)
from ess.sans.types import BackgroundRun, NormalizedQ, Numerator

from ess.spec.testing import check_caching


@pytest.fixture(scope='module')
def computed_masks(sample: Path, mask_file: Path) -> MasksOutputs:
    return bindings.masks_binding.compute(
        MasksParams(run=sample, mask_files=[mask_file])
    )


def test_masks_are_named_by_file_and_shown_on_the_image(
    computed_masks: MasksOutputs, mask_file: Path
) -> None:
    masks = computed_masks.masks.masks
    assert list(masks) == [mask_file.name]
    assert masks[mask_file.name].sum().value == 249022
    image = computed_masks.image.counts
    assert sc.identical(image.masks[mask_file.name], masks[mask_file.name])
    assert 'position' in image.coords
    assert image.sizes == masks[mask_file.name].sizes


def test_without_mask_files_no_pixel_is_masked(sample: Path) -> None:
    outputs = bindings.masks_binding.compute(MasksParams(run=sample, mask_files=[]))
    assert outputs.masks.masks == {}
    assert not outputs.image.counts.masks
    assert outputs.image.counts.sum().value > 0


def test_a_mask_marks_pixels_on_the_image_and_keeps_their_counts(
    computed_masks: MasksOutputs,
) -> None:
    image = computed_masks.image.counts
    assert image.drop_masks(list(image.masks)).sum().value > image.sum().value


def test_beam_centre_is_that_of_the_tutorial(
    sample: Path, computed_masks: MasksOutputs, tutorial_centre: BeamCentre
) -> None:
    outputs = bindings.beam_centre_binding.compute(
        BeamCentreParams(sample=sample, masks=computed_masks.masks)
    )
    assert outputs.centre.x == pytest.approx(tutorial_centre.x)
    assert outputs.centre.y == pytest.approx(tutorial_centre.y)
    # esssans gives the tutorial's distance to five decimals.
    assert outputs.centre.z == pytest.approx(tutorial_centre.z, abs=1e-5)


def test_beam_centre_image_shares_no_memory_with_the_masks_given(
    sample: Path, computed_masks: MasksOutputs
) -> None:
    given = computed_masks.masks
    outputs = bindings.beam_centre_binding.compute(
        BeamCentreParams(sample=sample, masks=given)
    )
    for name, mask in given.masks.items():
        assert not np.shares_memory(
            outputs.image.counts.masks[name].values, mask.values
        )


def test_iofq_is_that_of_plain_esssans(
    iofq_values: dict[str, object],
    computed_masks: MasksOutputs,
    direct_beam: Path,
    plain_esssans_iofq: sc.DataArray,
) -> None:
    outputs = bindings.iofq_binding.compute(
        IofQParams(**iofq_values, masks=computed_masks.masks, direct_beam=direct_beam)
    )
    assert sc.identical(outputs.iofq.intensity, plain_esssans_iofq)
    assert outputs.iofq.title == 'My experiment'
    assert outputs.iofq.run is None  # the files of this test name no run
    assert outputs.transmission.sample.sizes == {'wavelength': 200}
    assert outputs.transmission.can.sizes == {'wavelength': 200}


def test_iofq_without_direct_beam_differs_from_that_with_one(
    iofq_values: dict[str, object],
    computed_masks: MasksOutputs,
    plain_esssans_iofq: sc.DataArray,
) -> None:
    outputs = bindings.iofq_binding.compute(
        IofQParams(**iofq_values, masks=computed_masks.masks, direct_beam=None)
    )
    assert outputs.iofq.intensity.sizes == plain_esssans_iofq.sizes
    assert not sc.identical(outputs.iofq.intensity, plain_esssans_iofq)


def test_cans_branch_of_iofq_does_not_depend_on_the_sample(
    iofq_values: dict[str, object], computed_masks: MasksOutputs, direct_beam: Path
) -> None:
    fixed = {
        k: v
        for k, v in iofq_values.items()
        if k not in ('sample', 'sample_transmission')
    }
    pipeline = bindings.iofq_binding.pipeline(
        {**fixed, 'masks': computed_masks.masks, 'direct_beam': direct_beam}
    )
    # With no sample run set, this would raise if the can's branch needed one.
    assert pipeline.compute(NormalizedQ[BackgroundRun, Numerator]).sum().value > 0


def test_stage_with_the_sample_blank_gives_the_plain_results(
    iofq_values: dict[str, object],
    computed_masks: MasksOutputs,
    direct_beam: Path,
    sample: Path,
    other_sample: Path,
) -> None:
    fixed = {k: v for k, v in iofq_values.items() if k != 'sample'}
    check_caching(
        bindings.iofq_binding,
        {**fixed, 'masks': computed_masks.masks, 'direct_beam': direct_beam},
        [{'sample': s} for s in (sample, other_sample, sample)],
        rtol=1e-5,
    )


def test_masks_of_another_shape_are_refused(
    iofq_values: dict[str, object], direct_beam: Path
) -> None:
    masks = PixelMasks(masks={'wrong': sc.array(dims=['pixel'], values=[True, False])})
    with pytest.raises(sc.DimensionError):
        bindings.iofq_binding.compute(
            IofQParams(**iofq_values, masks=masks, direct_beam=direct_beam)
        )

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
    AngleRange,
    BeamCentre,
    BeamCentreParams,
    CalibratedDetectorParams,
    CreateMaskParams,
    DetectorImage,
    IofQParams,
    MasksOutputs,
    MasksParams,
    PixelMasks,
)
from ess.sans.types import BackgroundRun, NormalizedQ, Numerator

from ess.spec.testing import check_caching


@pytest.fixture(scope='module')
def detector(sample: Path) -> DetectorImage:
    return bindings.detector_binding.compute(
        CalibratedDetectorParams(run=sample)
    ).detector


@pytest.fixture(scope='module')
def computed_masks(detector: DetectorImage, mask_file: Path) -> MasksOutputs:
    return bindings.masks_binding.compute(
        MasksParams(detector=detector, mask_files=[mask_file])
    )


def test_the_detector_holds_counts_and_what_the_specs_that_read_it_need(
    detector: DetectorImage,
) -> None:
    counts = detector.counts
    assert counts.sum().value > 0
    assert not counts.masks
    assert {
        'position',
        'detector_number',
        'sample_position',
        'source_position',
    } <= set(counts.coords)


def test_masks_are_named_by_file_and_fit_the_detector(
    computed_masks: MasksOutputs, detector: DetectorImage, mask_file: Path
) -> None:
    masks = computed_masks.masks.masks
    assert list(masks) == [mask_file.name]
    assert masks[mask_file.name].sum().value == 249022
    assert detector.counts.sizes == masks[mask_file.name].sizes


def test_without_mask_files_no_pixel_is_masked(detector: DetectorImage) -> None:
    outputs = bindings.masks_binding.compute(
        MasksParams(detector=detector, mask_files=[])
    )
    assert outputs.masks.masks == {}


def test_beam_centre_is_that_of_the_tutorial(
    detector: DetectorImage,
    computed_masks: MasksOutputs,
    tutorial_centre: BeamCentre,
) -> None:
    outputs = bindings.beam_centre_binding.compute(
        BeamCentreParams(detector=detector, masks=computed_masks.masks)
    )
    assert outputs.centre.x == pytest.approx(tutorial_centre.x)
    assert outputs.centre.y == pytest.approx(tutorial_centre.y)
    # esssans gives the tutorial's distance to five decimals.
    assert outputs.centre.z == pytest.approx(tutorial_centre.z, abs=1e-5)


def angle_mask(
    detector: DetectorImage,
    centre: BeamCentre,
    ranges: list[AngleRange],
    base: PixelMasks | None = None,
) -> PixelMasks:
    return bindings.create_mask_binding.compute(
        CreateMaskParams(
            detector=detector, beam_centre=centre, base=base, shapes=ranges
        )
    ).masks


def test_an_angle_range_masks_the_pixels_at_those_angles(
    detector: DetectorImage,
) -> None:
    ranges = [
        AngleRange(start=1.0, stop=2.0),
        AngleRange(start=3.0, stop=4.0),
    ]
    masks = angle_mask(detector, BeamCentre(x=0.0, y=0.0, z=1.0), ranges)
    (mask,) = masks.masks.values()
    coords = detector.counts.coords
    to_pixel = coords['position'] - coords['sample_position']
    angle = np.degrees(
        np.arctan2(
            np.hypot(to_pixel.fields.x.values, to_pixel.fields.y.values),
            to_pixel.fields.z.values,
        )
    )
    assert 0 < mask.values.sum() < mask.values.size
    assert mask.sizes == detector.counts.sizes
    assert np.array_equal(
        mask.values, ((1 <= angle) & (angle < 2)) | ((3 <= angle) & (angle < 4))
    )


def test_an_angle_is_measured_from_the_beam_centre(
    detector: DetectorImage, tutorial_centre: BeamCentre
) -> None:
    masks = angle_mask(detector, tutorial_centre, [AngleRange(start=0.0, stop=1.0)])
    (mask,) = masks.masks.values()
    position = detector.counts.coords['position']
    assert position.fields.x.values[mask.values].mean() == pytest.approx(
        tutorial_centre.x, abs=2e-3
    )
    assert position.fields.y.values[mask.values].mean() == pytest.approx(
        tutorial_centre.y, abs=2e-3
    )


def test_an_angle_mask_adds_to_the_masks_given_and_shares_no_memory_with_them(
    detector: DetectorImage,
    computed_masks: MasksOutputs,
    tutorial_centre: BeamCentre,
    mask_file: Path,
) -> None:
    given = computed_masks.masks
    masks = angle_mask(
        detector, tutorial_centre, [AngleRange(start=0.0, stop=1.0)], base=given
    )
    assert len(masks.masks) == 2
    assert sc.identical(masks.masks[mask_file.name], given.masks[mask_file.name])
    assert not np.shares_memory(
        masks.masks[mask_file.name].values, given.masks[mask_file.name].values
    )


def test_a_second_angle_mask_keeps_the_ranges_of_the_first(
    detector: DetectorImage, tutorial_centre: BeamCentre
) -> None:
    first = AngleRange(start=0.0, stop=1.0)
    second = AngleRange(start=2.0, stop=3.0)
    both = angle_mask(detector, tutorial_centre, [first, second])
    chained = angle_mask(
        detector,
        tutorial_centre,
        [second],
        base=angle_mask(detector, tutorial_centre, [first]),
    )
    assert chained.masks.keys() == both.masks.keys()
    (name,) = both.masks
    assert sc.identical(chained.masks[name], both.masks[name])


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

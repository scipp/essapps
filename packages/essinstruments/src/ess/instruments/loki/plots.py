# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The plots of LoKI's specs: each a function of the outputs of one record, and of
what its request referenced, that returns a plopp figure, as
``ess.apps.panel.App(plots=)`` takes them. They need plopp, not Panel, so a notebook
can draw them as well.
"""

import plopp as pp
import scipp as sc
from plopp.core.typing import FigureLike as Figure

from ess.apps import SpecId
from ess.apps.plotting import Plot

from .specs import (
    BEAM_CENTRE,
    CREATE_MASK,
    IOFQ,
    MASKS,
    BeamCentreOutputs,
    DetectorImage,
    IofQOutputs,
    MasksOutputs,
    PixelMasks,
)


def on_detector(image: sc.DataArray) -> sc.DataArray:
    """The counts by the position of each pixel on the detector plane."""
    pixels = image.flatten(to='id')
    # hist needs contiguous coordinates, and a field is a view with strides.
    pixels.coords['x'] = pixels.coords['position'].fields.x.copy()
    pixels.coords['y'] = pixels.coords['position'].fields.y.copy()
    others = [c for c in pixels.coords if c not in ('x', 'y')]
    return pixels.drop_coords(others).hist(y=128, x=128)


def masks_on_image(outputs: MasksOutputs, detector: DetectorImage) -> Figure:
    """The detector image without the masked pixels, which are left blank."""
    return pp.plot(
        on_detector(detector.counts.assign_masks(outputs.masks.masks)),
        norm='log',
        title='Without the masked pixels',
    )


def centre_on_image(
    outputs: BeamCentreOutputs, detector: DetectorImage, masks: PixelMasks
) -> Figure:
    """The detector image, with a red cross at the beam centre."""
    centre = outputs.centre
    figure = pp.plot(
        on_detector(detector.counts.assign_masks(masks.masks)),
        norm='log',
        title=f'Beam centre: x = {centre.x:.4f} m, y = {centre.y:.4f} m',
    )
    figure.ax.plot(centre.x, centre.y, marker='+', color='red', markersize=20, mew=2)
    return figure


def iofq_curve(outputs: IofQOutputs) -> Figure:
    """I(Q), on a logarithmic axis."""
    return pp.plot(outputs.iofq.intensity, norm='log', title='I(Q)')


def transmission_curves(outputs: IofQOutputs) -> Figure:
    """The transmission of the sample and of the can, by wavelength."""
    curves = outputs.transmission
    return pp.plot({'sample': curves.sample, 'can': curves.can}, title='Transmission')


_MASKS_ON_IMAGE = Plot(masks_on_image, inputs=('detector',))

PLOTS = {
    SpecId.of(MASKS): {'Masks on image': _MASKS_ON_IMAGE},
    SpecId.of(CREATE_MASK): {'Masks on image': _MASKS_ON_IMAGE},
    SpecId.of(BEAM_CENTRE): {
        'Centre on image': Plot(centre_on_image, inputs=('detector', 'masks'))
    },
    SpecId.of(IOFQ): {
        'I(Q)': Plot(iofq_curve),
        'Transmission': Plot(transmission_curves),
    },
}

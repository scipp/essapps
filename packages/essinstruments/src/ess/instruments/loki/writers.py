# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The writers of the outputs of LoKI's reductions.

:data:`NXCANSAS` writes I(Q) as an NXcanSAS file with esssans. The module needs the
``loki`` extra of ``essinstruments``, which brings esssans.

An application gives :data:`WRITERS`, the writers by the name of a spec, for example
``essbatch -W ess.instruments.loki.writers:WRITERS``.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ess.sans.io import save_background_subtracted_iofq
from ess.sans.types import Measurement, SampleRun

from ess.apps.writers import DatasetMetadata, Writer

from .specs import IOFQ


def _nxcansas(
    outputs: Mapping[str, Any], metadata: DatasetMetadata, directory: Path
) -> None:
    iofq = outputs['iofq']
    # NXcanSAS requires a run; a file without one, such as those of the LoKI
    # tutorial, gets an empty one. scippneutron's run_number_maybe_int raises
    # TypeError on None (scipp/scippneutron#747), so None is not passed on.
    run = '' if iofq.run is None else iofq.run
    save_background_subtracted_iofq(
        iofq=iofq.intensity,
        out_filename=directory / 'iofq.h5',
        measurement=Measurement[SampleRun](title=iofq.title, run_number=run),
    )


NXCANSAS = Writer(
    name='nxcansas',
    title='NXcanSAS',
    description='I(Q) as iofq.h5, an NXcanSAS file with the title and run of the '
    'sample.',
    outputs=('iofq',),
    write=_nxcansas,
)

WRITERS = {IOFQ.name: [NXCANSAS]}

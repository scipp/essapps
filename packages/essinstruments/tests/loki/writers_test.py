# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The NXcanSAS writer of LoKI, on a synthetic I(Q)."""

from pathlib import Path

import numpy as np
import pytest
import scipp as sc
import scippnexus as snx
from ess.instruments.loki.specs import IofQ
from ess.instruments.loki.writers import NXCANSAS

from ess.apps.writers import About

ABOUT = About(name='sample')


def _iofq(run: str | None) -> IofQ:
    q = sc.linspace('Q', 0.01, 0.5, 6, unit='1/angstrom')
    intensity = sc.DataArray(
        sc.array(
            dims=['Q'],
            values=[5.0, 4.0, 3.0, 2.0, 1.0],
            variances=[0.5, 0.4, 0.3, 0.2, 0.1],
            unit='1/cm',
        ),
        coords={'Q': q},
    )
    return IofQ(intensity=intensity, title='My sample', run=run)


def test_nxcansas_writes_intensity_title_and_run(tmp_path: Path) -> None:
    NXCANSAS.write({'iofq': _iofq('60339')}, ABOUT, tmp_path)
    with snx.File(tmp_path / 'iofq.h5') as f:
        entry = f['sasentry']
        assert entry['title'][()] == 'My sample'
        assert str(entry['run'][()]) == '60339'
        data = entry['sasdata'][()]
    np.testing.assert_allclose(data.values, [5.0, 4.0, 3.0, 2.0, 1.0])
    np.testing.assert_allclose(data.variances, [0.5, 0.4, 0.3, 0.2, 0.1])


def test_nxcansas_without_run_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='run number'):
        NXCANSAS.write({'iofq': _iofq(None)}, ABOUT, tmp_path)

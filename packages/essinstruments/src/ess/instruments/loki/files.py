# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a file of LoKI says of itself, for the table of a workspace in a folder
(see :func:`ess.apps.folder.scan_folder`). It needs the extra ``[loki-app]``.

No file says what a run is for, so ``role`` is left for a person to fill in, and
with it the ``role`` of a calibration file, such as ``direct_beam``.
"""

import re
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from ess.apps.files import catalogue_identifier

_LEADING_RUN = re.compile(r'\d+')


def _text(entry: h5py.Group, name: str) -> str | None:
    """The text of a field of ``entry``, a scalar or an array of one element."""
    if name not in entry:
        return None
    value = np.ravel(entry[name][()])[0]
    return value.decode() if isinstance(value, bytes) else str(value)


def read_metadata(path: Path) -> dict[str, Any] | None:
    """
    The metadata of a file, or ``None`` if it is not one of LoKI's::

        *.xml                {'kind': 'mask'}
        HDF5 with an NXentry {'kind': 'raw', 'run': ..., 'pid': ..., 'sample': ...}
        HDF5 written by scipp {'kind': 'calibration'}

    The run of a raw file is its ``entry_identifier``, and its ``pid`` the
    catalogue identifier formed from its ``entry_identifier_uuid`` (see
    :func:`ess.apps.files.catalogue_identifier`). The files of the detector test
    at Larmor have neither, and are named by their run, ``60339-....nxs``.
    """
    if path.suffix == '.xml':
        return {'kind': 'mask'}
    if not h5py.is_hdf5(path):
        return None
    with h5py.File(path) as file:
        entry = file.get('entry')
        if entry is None or entry.attrs.get('NX_class') != 'NXentry':
            return {'kind': 'calibration'} if 'scipp-type' in file.attrs else None
        named = _LEADING_RUN.match(path.name)
        uuid = _text(entry, 'entry_identifier_uuid')
        found = {
            'run': _text(entry, 'entry_identifier') or (named and named.group()),
            'pid': uuid and catalogue_identifier(uuid),
            'sample': _text(entry, 'sample/name'),
            'title': _text(entry, 'title'),
        }
    return {'kind': 'raw', **{k: v for k, v in found.items() if v}}

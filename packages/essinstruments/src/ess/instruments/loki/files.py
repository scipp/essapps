# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Reads the metadata of LoKI's files for the files table of a workspace folder
(:func:`ess.apps.folder.scan_folder`). It needs the extra ``[loki-app]``.

No file states what it is for. A person fills in the ``role`` of each run and
calibration file, such as ``direct_beam``.
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
    Returns the metadata of a LoKI file, or ``None`` if it is not one::

        *.xml                  {'kind': 'mask'}
        HDF5 with an NXentry   {'kind': 'raw', 'run', 'pid', 'sample', 'title'}
        HDF5 written by scipp  {'kind': 'calibration'}

    ``run`` is the ``entry_identifier``, or the number the file name starts
    with, as in the Larmor files ``60339-....nxs``. ``pid`` is formed from the
    ``entry_identifier_uuid`` (:func:`ess.apps.files.catalogue_identifier`). A
    raw file lacking a field lacks its key.
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

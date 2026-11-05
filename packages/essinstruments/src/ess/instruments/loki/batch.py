# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch of I(Q) reductions, and the result each row has now.

A sample run is a row. The ``sample`` key of the metadata of its file names the
sample, and pairs the run with the transmission run of the same sample::

    {'file': 'run-1.nxs', 'run': 1, 'role': 'sample', 'sample': 'water'}
    {'file': 'run-2.nxs', 'run': 2, 'role': 'transmission', 'sample': 'water'}

The result of a row is read from the records alone, so that a record made
by hand for one run takes the place of the row's result.

This module needs neither Panel nor plopp.
"""

from collections import Counter
from dataclasses import dataclass

from ess.apps import Files, Listing, Record, Selector, SpecId
from ess.spec import FileRef

from .specs import IOFQ


@dataclass(frozen=True)
class Row:
    """
    A sample run to reduce. ``name`` is the ``sample`` of its file's metadata, with
    the run number if the sample was measured more than once, or the run number of
    a file without one, so that it tells the rows apart. ``transmission`` is the
    sample's transmission run; ``None`` if there is none, and the row cannot be
    reduced.
    """

    name: str
    sample: FileRef
    transmission: FileRef | None


def rows(files: Files) -> list[Row]:
    """
    A row for each sample run, in the order measured. Where several transmission
    runs have the sample's name, the last measured is the row's. A sample run
    without a name has no transmission run.
    """
    transmissions: dict[object, FileRef] = {}
    for ref in files.list(Selector(role='transmission')):
        if (name := files.metadata(ref).get('sample')) is not None:
            transmissions[name] = ref
    samples = [(r, files.metadata(r)) for r in files.list(Selector(role='sample'))]
    measured = Counter(metadata.get('sample') for _, metadata in samples)
    result = []
    for ref, metadata in samples:
        run = str(metadata.get('run', ref))
        name = metadata.get('sample')
        if name is None:
            result.append(Row(run, ref, None))
        else:
            shown = str(name) if measured[name] == 1 else f'{name} ({run})'
            result.append(Row(shown, ref, transmissions.get(name)))
    return result


def latest_results(listing: Listing) -> dict[FileRef, Record]:
    """
    For each sample run that an I(Q) record names, the newest such record,
    whatever its status.
    """
    iofq = SpecId.of(IOFQ)
    return {
        record.request.params['sample']: record
        for record in listing.records
        if record.spec == iofq
    }

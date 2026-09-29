# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch: one template filled for each of many datasets."""

from __future__ import annotations

from collections.abc import Iterable

from ess.reduce.spec import DatasetRef

from .datasets import DatasetSource
from .records import Request, Template


def apply(
    template: Template,
    datasets: Iterable[DatasetRef],
    source: DatasetSource,
    *,
    member_field: str,
) -> dict[str, Request]:
    """
    Fill the template's one blank with each dataset.

    The requests are keyed by member: the value of the metadata field
    ``member_field`` of each dataset, read from ``source``. Submitting them
    under a label makes the keys the members of their records.
    """
    if len(template.blanks) != 1:
        raise ValueError(f'a batch fills one blank; the template has {template.blanks}')
    (blank,) = template.blanks
    requests: dict[str, Request] = {}
    for ref in datasets:
        member = str(source.metadata(ref)[member_field])
        if member in requests:
            raise ValueError(f'two datasets have {member_field} {member}')
        requests[member] = template.fill({blank: ref})
    return requests

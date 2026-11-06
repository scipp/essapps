# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch: one template filled for each of many files."""

from __future__ import annotations

from collections.abc import Iterable

from ess.spec import FileRef

from .client import Files
from .records import Request, Template


def apply(
    template: Template,
    files: Iterable[FileRef],
    source: Files,
    *,
    at: str,
    member_field: str = 'run',
) -> dict[str, Request]:
    """
    Fill the template for each file.

    ``source`` answers metadata queries, such as ``client.files``. Each file
    fills ``at``, the template's one blank, a file parameter::

        template = Template(IOFQ, blanks=('sample',))
        apply(template, runs, client.files, at='sample')
        # {'101': Request(IOFQ, params={'sample': file(run=101)}), ...}

    The requests are keyed by member: the value of the metadata field
    ``member_field`` of each file. Submitting them under a label makes the
    keys the members of their records.

    Raises
    ------
    ValueError
        If ``at`` is not the one blank, or two files have the same member.
    LookupError
        If the metadata of a file lacks ``member_field``.
    """
    if len(template.blanks) != 1:
        raise ValueError(f'a file fills one blank; {template.blanks} leave more')
    if at != template.blanks[0]:
        raise ValueError(f'a file fills the blank {template.blanks[0]!r}, not {at!r}')
    requests: dict[str, Request] = {}
    for ref in files:
        member = str(source.metadata(ref)[member_field])
        if member in requests:
            raise ValueError(f'two files have {member_field} {member}')
        requests[member] = template.fill(params={at: ref})
    return requests

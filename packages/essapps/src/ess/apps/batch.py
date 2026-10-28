# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch: one template filled for each of many files.

A lookup fills further blanks per file, such as the can measured most
recently before a sample. A file fills the one blank the lookup leaves, a
parameter of a file model, such as ``'sample'``. Files are ordered by
their run number.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ess.spec import FileRef

from .client import Files
from .files import Selector
from .records import Request, Template


def run_number(ref: FileRef, source: Files) -> int:
    return int(source.metadata(ref)['run'])


@dataclass(frozen=True)
class LastBefore:
    """The matching file with the highest run number below the filled one's."""

    selector: Selector

    def find(self, ref: FileRef, source: Files) -> FileRef:
        run = run_number(ref, source)
        earlier = [d for d in source.list(self.selector) if run_number(d, source) < run]
        if not earlier:
            raise LookupError(f'no file matching {self.selector} before run {run}')
        return max(earlier, key=lambda d: run_number(d, source))


class Lookup:
    """
    Rules that fill blanks per file, each keyed by its blank, a parameter
    of a file model::

        Lookup({'can': LastBefore(Selector(role='can'))})
    """

    def __init__(self, rules: Mapping[str, LastBefore]) -> None:
        self.rules = dict(rules)

    @property
    def blanks(self) -> set[str]:
        """The blanks the lookup fills."""
        return set(self.rules)

    def fill(self, ref: FileRef, source: Files) -> dict[str, FileRef]:
        """Each blank the lookup fills, with the file found for it."""
        return {blank: rule.find(ref, source) for blank, rule in self.rules.items()}


def file_place(template: Template, lookup: Lookup | None, at: str) -> str:
    """The place a file fills, ``at``: the one blank the lookup leaves."""
    left = [b for b in template.blanks if lookup is None or b not in lookup.blanks]
    if len(left) != 1:
        raise ValueError(f'a file fills one blank; {template.blanks} leave {left}')
    if at != left[0]:
        raise ValueError(f'a file fills the blank {left[0]!r}, not {at!r}')
    return at


def apply(
    template: Template,
    files: Iterable[FileRef],
    source: Files,
    *,
    at: str,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> dict[str, Request]:
    """
    Fill the template for each file.

    ``source`` answers metadata queries, such as ``client.files``. Each
    file fills the blank ``at``, the one the lookup leaves, a parameter
    of a file model::

        template = Template(IOFQ, blanks=('sample',))
        apply(template, runs, client.files, at='sample')
        # {'101': Request(IOFQ, params={'sample': file(run=101)}), ...}

    The requests are keyed by member: the value of the metadata field
    ``member_field`` of each file. Submitting them under a label makes the
    keys the members of their records. Raises ``LookupError`` if the request
    of a file cannot be made.
    """
    requests, failed = try_apply(
        template,
        files,
        source,
        at=at,
        member_field=member_field,
        lookup=lookup,
    )
    if failed:
        raise next(iter(failed.values()))
    return requests


def try_apply(
    template: Template,
    files: Iterable[FileRef],
    source: Files,
    *,
    at: str,
    member_field: str = 'run',
    lookup: Lookup | None = None,
) -> tuple[dict[str, Request], dict[FileRef, LookupError]]:
    """
    As :func:`apply`, but leave out each file whose request cannot be made.

    A request cannot be made if the lookup finds nothing for the file, or
    its metadata lacks ``member_field``. Returns the requests, and the error of
    each file left out.
    """
    place = file_place(template, lookup, at)
    requests: dict[str, Request] = {}
    failed: dict[FileRef, LookupError] = {}
    for ref in files:
        try:
            member = str(source.metadata(ref)[member_field])
            found = lookup.fill(ref, source) if lookup else {}
        except LookupError as error:
            failed[ref] = error
            continue
        if member in requests:
            raise ValueError(f'two files have {member_field} {member}')
        requests[member] = template.fill(params={place: ref, **found})
    return requests, failed

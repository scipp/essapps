# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a client lists now, looked up once for a refresh of every view."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ess.spec import File, FileRef, OutputRef, field_types
from ess.spec.refs import iter_refs

from .client import Client
from .files import Selector
from .records import Record, Status


class Listing:
    """
    What a request of ``client`` may name now, and the state of the client's
    records, looked up once for a refresh of any number of views: the files
    to offer for each file field and the client's records, each by a label, and the
    outputs a request may reference. ``cursor`` is the position in the client's
    changes (:meth:`~ess.apps.Client.changes`) that the listing is up to date
    with. Given the ``previous`` listing, it reads only the records that changed
    since, in the records, their status, and what is readable, and takes the
    rest from ``previous``, which stays as it is. The records are oldest
    first. Labels and the outputs offered are worked out from all the records
    in memory, so a label that depends on the other records needs no read.
    ``specs`` holds the spec of each record by its id, ``status`` its status,
    and ``readable`` the names of its outputs that a request may reference.

    It stands in for two queries the client lacks (scipp/essapps#95). The
    client lists no records by the type of their outputs (gap 1), so
    :meth:`fitting` checks the type of each readable output. A record has no
    short name (gap 4), so its label is its spec's title and the local time
    it was created, ``Find centre (11:45)``, and an output's is its type's
    name instead of the spec's title, ``BeamCentre (11:45)``. Where labels
    would be the same, a record's adds the start of its id, ``Find centre
    (11:45, 2f9c01)``, and an output's adds its name and then, if still the
    same, the start of its record's id: ``BeamCentre (11:45, centre,
    2f9c01)``.
    """

    def __init__(self, client: Client, previous: Listing | None = None) -> None:
        self._client = client
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._listed: dict[str, list[FileRef]] = {}
        self._files: dict[File, dict[str, FileRef]] = {}
        changes = client.changes(after=0 if previous is None else previous.cursor)
        self.cursor = changes.cursor
        changed = client.records(ids=changes.records)
        old = [] if previous is None else previous.records
        known = {r.id for r in old}
        self.records = old + [r for r in changed if r.id not in known]
        specs = {} if previous is None else {r.spec: previous.specs[r.id] for r in old}
        for spec in {r.spec for r in changed} - specs.keys():
            specs[spec] = client.spec(spec)
        self.specs = {r.id: specs[r.spec] for r in self.records}
        self.status = {} if previous is None else dict(previous.status)
        self.readable = {} if previous is None else dict(previous.readable)
        statuses = client.status(changed)
        readable = client.readable(changed)
        self.status |= {r.id: s for r, s in zip(changed, statuses, strict=True)}
        self.readable |= {r.id: n for r, n in zip(changed, readable, strict=True)}
        self.labels = _labels(
            {r.id: (self.specs[r.id].title, r, (r.id[:6],)) for r in self.records}
        )
        self._output_labels = _labels(
            {
                r.ref(name): (self.type_name(r, name), r, (name, r.id[:6]))
                for r in self.records
                for name in r.outputs
            }
        )
        self._outputs = [
            (
                self.output_label(record.ref(name)),
                record.ref(name),
                self.specs[record.id].outputs.model_fields[name].annotation,
            )
            for record in reversed(self.records)
            for name in self.readable[record.id]
        ]

    @property
    def pending(self) -> list[Record]:
        """The records listed as pending."""
        return [r for r in self.records if self.status[r.id] is Status.PENDING]

    def type_name(self, record: Record, name: str) -> str:
        """The name of the type of an output, without ``| None``."""
        info = self.specs[record.id].outputs.model_fields[name]
        return field_types.type_name(field_types.value_type(info.annotation))

    def is_group(self, record: Record, name: str) -> bool:
        """Whether an output is a group, which ``describe`` gives by field."""
        info = self.specs[record.id].outputs.model_fields[name]
        return field_types.is_group(field_types.value_type(info.annotation))

    def reads(self, record: Record) -> list[str]:
        """The label of each file and output that the record's request names."""
        return [
            self.file_label(ref) if isinstance(ref, FileRef) else self.output_label(ref)
            for _, ref in iter_refs(record.request.params)
        ]

    def files(self, marker: File) -> dict[str, FileRef]:
        """
        The files to offer for a file field with ``marker``, each by a label, in
        the order measured: those that may fill it (:meth:`Selector.of`), but not
        a run with another role than the field's. A run without a role is offered
        for every role.
        """
        if marker not in self._files:
            wanted = marker.metadata.get('role')
            self._files[marker] = {}
            for ref in self._list(Selector.of(marker)):
                fields = self._known(ref)
                if wanted is None or fields.get('role', wanted) == wanted:
                    role = f' ({fields["role"]})' if 'role' in fields else ''
                    self._files[marker][_file_label(ref, fields) + role] = ref
        return self._files[marker]

    def _list(self, selector: Selector) -> list[FileRef]:
        """The files of ``selector``, looked up once, and their metadata in one call."""
        if (key := repr(selector)) not in self._listed:
            listed = self._client.files.list(selector)
            unknown = [ref for ref in listed if ref not in self._metadata]
            found = self._client.files.metadata(unknown)
            self._metadata |= dict(zip(unknown, found, strict=True))
            self._listed[key] = listed
        return self._listed[key]

    def _known(self, ref: FileRef) -> dict[str, Any]:
        """The metadata of a file, looked up once."""
        if ref not in self._metadata:
            self._metadata[ref] = self._client.files.metadata(ref)
        return self._metadata[ref]

    def file_label(self, ref: FileRef) -> str:
        """The label of a file, as :meth:`files` gives it, without its role."""
        return _file_label(ref, self._known(ref))

    def output_label(self, ref: OutputRef) -> str:
        """The label of an output; the reference if its record is not listed."""
        return self._output_labels.get(ref, str(ref))

    def fitting(self, tp: Any) -> dict[str, OutputRef]:
        """The outputs a request may reference that fit ``tp``, newest first."""
        return {
            label: ref for label, ref, out in self._outputs if field_types.fits(out, tp)
        }


def _labels[K](named: Mapping[K, tuple[str, Record, tuple[str, ...]]]) -> dict[K, str]:
    """
    ``name (time)`` of each key, from its name, record, and tags; with as many
    of its tags as it takes to tell it from each other key.
    """
    labels = {
        k: f'{name} ({record.created.astimezone():%H:%M}'
        for k, (name, record, _) in named.items()
    }
    for i in range(max((len(tags) for *_, tags in named.values()), default=0)):
        counts = Counter(labels.values())
        for k, label in labels.items():
            if counts[label] > 1:
                labels[k] = f'{label}, {named[k][2][i]}'
    return {k: f'{label})' for k, label in labels.items()}


def _file_label(ref: FileRef, metadata: Mapping[str, Any]) -> str:
    """
    ``run 101`` where the metadata has a run, else the file's identity:
    the contract of a file source names no field but ``proposal``.
    """
    run = metadata.get('run')
    return str(ref) if run is None else f'run {run}'

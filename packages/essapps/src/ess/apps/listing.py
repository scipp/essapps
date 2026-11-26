# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a client lists now, looked up once for a refresh of all views."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from pydantic_core import to_json

from ess.spec import File, FileRef, OutputRef, field_types
from ess.spec.refs import as_ref, iter_refs

from .client import Client
from .files import Selector
from .records import Record, SpecId, Status


class Listing:
    """
    The records of ``client``, and the files and outputs that a request may
    refer to now, each with a label, looked up once for a refresh of all views.

    Given the ``previous`` listing, it reads only the records that changed
    since, and takes the rest from ``previous``, which stays unchanged.
    ``cursor`` is the position in :meth:`~ess.apps.Client.changes` that the
    listing is up to date with. ``records`` are oldest first. ``status`` holds
    the status of each record, and ``readable`` the names of its outputs that
    may be read. ``specs`` holds the spec of each record of a bound spec, by
    record ID. A record of an unbound spec is labelled by its spec's name and
    version, its outputs by their names, and none of its outputs is offered.

    Labels are computed from all records in memory, so a label that depends on
    other records needs no read::

        Find centre (11:45)                 a record: its spec's title, local time
        BeamCentre (11:45)                  an output: its type's name
        Find centre (11:45, 2f9c01)         a record whose label would repeat
        BeamCentre (11:45, centre)          an output whose label would repeat
        BeamCentre (11:45, centre, 2f9c01)  ... and would still repeat
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
        specs = {SpecId.of(spec): spec for spec in client.specs()}
        self.specs = {} if previous is None else dict(previous.specs)
        self.status = {} if previous is None else dict(previous.status)
        self.readable = {} if previous is None else dict(previous.readable)
        statuses = client.status(changed)
        readable = client.readable(changed)
        bound = client.bound(changed)
        self.specs |= {
            r.id: specs[r.spec] for r, b in zip(changed, bound, strict=True) if b
        }
        self.status |= {r.id: s for r, s in zip(changed, statuses, strict=True)}
        self.readable |= {r.id: n for r, n in zip(changed, readable, strict=True)}
        self.labels = _labels(
            {r.id: (self.title(r), r, (r.id[:6],)) for r in self.records}
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
            if record.id in self.specs
            for name in self.readable[record.id]
        ]

    @property
    def pending(self) -> list[Record]:
        """The records listed as pending."""
        return [r for r in self.records if self.status[r.id] is Status.PENDING]

    def title(self, record: Record) -> str:
        """
        The title of a record's spec, or its name and version, ``centre/v1``, if
        the record is of an unbound spec.
        """
        spec = self.specs.get(record.id)
        return str(record.spec) if spec is None else spec.title

    def type_name(self, record: Record, name: str) -> str:
        """
        The name of the type of an output, without ``| None``, or the output's
        name if its record is of an unbound spec.
        """
        if (spec := self.specs.get(record.id)) is None:
            return name
        info = spec.outputs.model_fields[name]
        return field_types.type_name(field_types.value_type(info.annotation))

    def is_group(self, record: Record, name: str) -> bool:
        """
        Whether an output of a record of the bound spec is a group.

        Raises ``KeyError`` for a record of an unbound spec, since only its
        stored value shows whether it is a ``DataGroup``.
        """
        info = self.specs[record.id].outputs.model_fields[name]
        return field_types.is_group(field_types.value_type(info.annotation))

    def reads(self, record: Record) -> list[str]:
        """The label of each file and output that the record's request refers to."""
        return [
            self.file_label(ref) if isinstance(ref, FileRef) else self.output_label(ref)
            for _, ref in iter_refs(record.request.params)
        ]

    def files(self, marker: File) -> dict[str, FileRef]:
        """
        The files to offer for a file field with ``marker``, by label, in the
        order measured.

        These are the files that may fill the field (:meth:`Selector.of`),
        except a run with another role than the field's. A run without a role is
        offered for every role.
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

    def listed(self, selector: Selector) -> dict[FileRef, dict[str, Any]]:
        """The files of ``selector``, with their metadata, looked up once."""
        return {ref: self._metadata[ref] for ref in self._list(selector)}

    def _list(self, selector: Selector) -> list[FileRef]:
        """The files of ``selector``, with their metadata read in one call."""
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
        """
        The label of a file, as in :meth:`files` without its role.

        The label is the reference itself if it is a name, such as
        ``file(run=1)``, or if the file source no longer has the file.
        """
        try:
            return _file_label(ref, self._known(ref))
        except KeyError:
            return str(ref)

    def output_label(self, ref: OutputRef) -> str:
        """The label of an output, or the reference if its record is not listed."""
        return self._output_labels.get(ref, str(ref))

    def text(self, value: Any) -> str:
        """
        The text that a table shows for a value.

        A file or an output is shown by its label, a list as its items' texts
        joined by commas, a dict as JSON, and ``None`` as ``none``.
        """
        ref = as_ref(value)
        if isinstance(ref, OutputRef):
            return self.output_label(ref)
        if isinstance(ref, FileRef):
            return self.file_label(ref)
        if isinstance(value, list):
            return ', '.join(self.text(item) for item in value)
        if isinstance(value, dict):
            return to_json(value).decode()
        return 'none' if value is None else str(value)

    def fitting(self, tp: Any) -> dict[str, OutputRef]:
        """The outputs a request may reference that fit ``tp``, newest first."""
        return {
            label: ref for label, ref, out in self._outputs if field_types.fits(out, tp)
        }


def _labels[K](named: Mapping[K, tuple[str, Record, tuple[str, ...]]]) -> dict[K, str]:
    """
    ``name (time)`` of each key, with as many of its tags as it takes to tell
    it from each other key.
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
    ``run 101`` where the metadata has a run, else the file's identity.

    The contract of a file source names no field except ``proposal``.
    """
    run = metadata.get('run')
    return str(ref) if run is None else f'run {run}'

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The record store: SQLite, one writer, schema-versioned.

Holds run records, the reference edges between them, and the registry of disk
copies (the part of the data store that knows where bytes are), keyed by
reference in either form. Records are never deleted one at a time.

See docs/developer/records.md.
"""

from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import IO, Self

from .records import RunRecord, Status
from .spec import Ref, SpecId

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    spec_name TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    proposal TEXT NOT NULL,
    instrument TEXT NOT NULL,
    label TEXT,
    member_key TEXT,
    supersedes TEXT,
    created TEXT NOT NULL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_proposal ON records (proposal, created);
CREATE INDEX IF NOT EXISTS records_label ON records (proposal, label, member_key);
CREATE INDEX IF NOT EXISTS records_supersedes ON records (supersedes);
CREATE TABLE IF NOT EXISTS refs (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    output TEXT NOT NULL,
    key TEXT
);
CREATE INDEX IF NOT EXISTS refs_to ON refs (to_id, output);
CREATE INDEX IF NOT EXISTS refs_from ON refs (from_id);
CREATE TABLE IF NOT EXISTS registry (
    ref TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    store_owned INTEGER NOT NULL
);
"""


class StoreLockedError(RuntimeError):
    """Another backend holds this store."""


class RecordStore:
    """Single-writer store of records; open it in exactly one backend process."""

    _HEAD = (
        'NOT EXISTS (SELECT 1 FROM records WHERE proposal=r.proposal'
        ' AND label=r.label AND member_key IS r.member_key AND supersedes=r.id)'
    )
    """The record under (proposal, label, member_key) that nothing supersedes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock: IO[str] = open(self.path.with_suffix('.lock'), 'w')
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            self._lock.close()
            raise StoreLockedError(f'{self.path} is held by another backend') from e
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.executescript(_SCHEMA)
        row = self._db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None:
            self._db.execute(
                "INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
            )
        elif int(row[0]) != SCHEMA_VERSION:
            raise RuntimeError(
                f'{self.path} has schema {row[0]}, this code needs {SCHEMA_VERSION}'
            )

    def close(self) -> None:
        self._db.close()
        fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # Records

    def add(self, *records: RunRecord) -> None:
        """Insert records in one transaction: all of them or none."""
        with self._db:
            self._db.execute('BEGIN')
            for record in records:
                req = record.request
                self._db.execute(
                    'INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        record.id,
                        req.spec.name,
                        req.spec.version,
                        record.status.value,
                        req.proposal,
                        req.instrument,
                        req.label,
                        req.member_key,
                        record.supersedes,
                        record.created.isoformat(),
                        record.model_dump_json(),
                    ),
                )
                self._db.executemany(
                    'INSERT INTO refs VALUES (?,?,?,?)',
                    [(record.id, r.record, r.output, r.key) for r in req.refs()],
                )

    def update(self, record: RunRecord) -> None:
        with self._db:
            cur = self._db.execute(
                'UPDATE records SET status=?, doc=? WHERE id=?',
                (record.status.value, record.model_dump_json(), record.id),
            )
        if cur.rowcount != 1:
            raise KeyError(record.id)

    def get(self, record_id: str) -> RunRecord:
        row = self._db.execute(
            'SELECT doc FROM records WHERE id=?', (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return RunRecord.model_validate_json(row[0])

    def __contains__(self, record_id: str) -> bool:
        return (
            self._db.execute(
                'SELECT 1 FROM records WHERE id=?', (record_id,)
            ).fetchone()
            is not None
        )

    def list(
        self,
        *,
        proposal: str | None = None,
        spec: SpecId | None = None,
        status: Status | None = None,
        label: str | None = None,
        member_key: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[RunRecord]:
        """Records matching every given filter, oldest first."""
        clauses, args = [], []
        for column, value in (
            ('proposal', proposal),
            ('status', status.value if status else None),
            ('label', label),
            ('member_key', member_key),
        ):
            if value is not None:
                clauses.append(f'{column}=?')
                args.append(value)
        if spec is not None:
            clauses.append('spec_name=? AND spec_version=?')
            args += [spec.name, spec.version]
        if since is not None:
            clauses.append('created>=?')
            args.append(since.isoformat())
        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        tail = f'LIMIT {int(limit)}' if limit else ''
        rows = self._db.execute(
            f'SELECT doc FROM records {where} ORDER BY created, rowid {tail}',  # noqa: S608
            args,
        )
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def latest(
        self, label: str, proposal: str, *, member_key: str | None = None
    ) -> RunRecord | None:
        """
        The head of this label and member key's chain, whatever its status.

        The head is the record under (proposal, label, member_key) that no other
        record supersedes; it does not depend on a clock, which matters once
        several writers, a rule, a retry, and a person's correction submit under
        one label from different hosts. Without ``member_key`` this is the chain
        whose member key is NULL, the slot form.
        """
        row = self._db.execute(
            'SELECT doc FROM records AS r '  # noqa: S608
            f'WHERE proposal=? AND label=? AND member_key IS ? AND {self._HEAD} '
            'ORDER BY rowid DESC LIMIT 1',
            (proposal, label, member_key),
        ).fetchone()
        return None if row is None else RunRecord.model_validate_json(row[0])

    def batch(self, label: str, proposal: str) -> list[RunRecord]:
        """The records under this label: the head per member key, by member key."""
        rows = self._db.execute(
            'SELECT doc FROM records AS r '  # noqa: S608
            f'WHERE proposal=? AND label=? AND {self._HEAD} '
            'ORDER BY member_key, rowid',
            (proposal, label),
        )
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def members_without_completed_record(
        self, label: str, proposal: str
    ) -> list[RunRecord]:
        """The records of :meth:`batch` whose member key never completed."""
        rows = self._db.execute(
            'SELECT doc FROM records AS r '  # noqa: S608
            f'WHERE proposal=? AND label=? AND {self._HEAD} '
            'AND NOT EXISTS (SELECT 1 FROM records WHERE proposal=r.proposal'
            ' AND label=r.label AND member_key IS r.member_key AND status=?)'
            ' ORDER BY member_key, rowid',
            (proposal, label, Status.COMPLETED.value),
        )
        return [RunRecord.model_validate_json(r[0]) for r in rows]

    def referencing(self, record_id: str, output: str | None = None) -> list[str]:
        """IDs of records that reference an output of ``record_id``."""
        if output is None:
            rows = self._db.execute(
                'SELECT DISTINCT from_id FROM refs WHERE to_id=?', (record_id,)
            )
        else:
            rows = self._db.execute(
                'SELECT DISTINCT from_id FROM refs WHERE to_id=? AND output=?',
                (record_id, output),
            )
        return [r[0] for r in rows]

    def by_status(self, *statuses: Status) -> Iterator[RunRecord]:
        marks = ','.join('?' * len(statuses))
        rows = self._db.execute(
            f'SELECT doc FROM records WHERE status IN ({marks}) '  # noqa: S608
            'ORDER BY created, rowid',
            [s.value for s in statuses],
        )
        return (RunRecord.model_validate_json(r[0]) for r in rows)

    # Registry of disk copies

    def register(self, ref: Ref, path: Path, *, store_owned: bool) -> None:
        with self._db:
            self._db.execute(
                'INSERT OR REPLACE INTO registry VALUES (?,?,?)',
                (str(ref), str(path), int(store_owned)),
            )

    def location(self, ref: Ref) -> tuple[Path, bool] | None:
        """Registered path of a copy and whether the store wrote it."""
        row = self._db.execute(
            'SELECT path, store_owned FROM registry WHERE ref=?', (str(ref),)
        ).fetchone()
        return None if row is None else (Path(row[0]), bool(row[1]))

    def unregister(self, ref: Ref) -> None:
        with self._db:
            self._db.execute('DELETE FROM registry WHERE ref=?', (str(ref),))

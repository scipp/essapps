# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The data store: a registry of disk copies, a disk tier, and a private cache.

The registry (in the record store) knows disk copies only. Every process that
holds data has a private in-memory cache that nothing else can see. A workflow
asks for a path or an object and gets it from whichever tier has it; only the
session shape serves objects from memory, and nothing in a spec or a binding
can tell.

See docs/developer/records.md.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Protocol

import scipp as sc

from .spec import OutputRef, Ref
from .store import RecordStore


class Serializer(Protocol):
    suffix: str

    def save(self, value: Any, path: Path) -> None: ...

    def load(self, path: Path) -> Any: ...


class ScippHDF5:
    suffix = '.h5'

    def save(self, value: Any, path: Path) -> None:
        value.save_hdf5(path)

    def load(self, path: Path) -> Any:
        return sc.io.load_hdf5(path)


class RawBytes:
    """An opaque file returned by a workflow as bytes."""

    suffix = '.bin'

    def save(self, value: Any, path: Path) -> None:
        path.write_bytes(value)

    def load(self, path: Path) -> Any:
        return path.read_bytes()


class Serializers:
    """Serializers by value type; scipp objects and bytes are built in."""

    def __init__(self) -> None:
        self._by_type: list[tuple[type, Serializer]] = [
            (sc.DataArray, ScippHDF5()),
            (sc.Variable, ScippHDF5()),
            (sc.Dataset, ScippHDF5()),
            (sc.DataGroup, ScippHDF5()),
            (bytes, RawBytes()),
        ]

    def add(self, type_: type, serializer: Serializer) -> None:
        self._by_type.insert(0, (type_, serializer))

    def save(self, value: Any, path_without_suffix: Path) -> Path:
        """Write ``value`` at ``path_without_suffix`` plus the serializer's suffix."""
        for type_, serializer in self._by_type:
            if isinstance(value, type_):
                path = path_without_suffix.with_name(
                    path_without_suffix.name + serializer.suffix
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                serializer.save(value, path)
                return path
        raise TypeError(f'No serializer for {type(value).__name__}; declare one')

    def load(self, path: Path) -> Any:
        for _, serializer in self._by_type:
            if path.suffix == serializer.suffix:
                return serializer.load(path)
        raise TypeError(f'Cannot load {path}')


class MissingCopyError(LookupError):
    """No copy of this output exists; recompute is explicit, never implicit."""


class DataStore:
    """
    Owned by the backend; ``root`` is its disk tier.

    Values are addressed by a reference in either form, though only outputs of
    records are ever written here: the copies the store makes. ``put`` always
    fills the cache and writes to disk only when asked; ``write_out`` moves a
    cached value to disk later, which is what publication and chaining out of a
    session do.
    """

    def __init__(self, records: RecordStore, root: Path) -> None:
        self._records = records
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[Ref, Any] = {}
        self.serializers = Serializers()

    def path_for(self, ref: OutputRef, suffix: str = '') -> Path:
        """Where the disk tier keeps this output; also used by throwaway runners."""
        name = ref.output if ref.key is None else f'{ref.output}/{ref.key}'
        return self.root / ref.record / f'{name}{suffix}'

    def put(self, ref: OutputRef, value: Any, *, to_disk: bool) -> None:
        self._cache[ref] = value
        if to_disk:
            self.write_out(ref)

    def write_out(self, ref: OutputRef) -> Path:
        """Write the cached value to the disk tier and register it."""
        if self.has_copy(ref):
            return self._records.location(ref)[0]  # type: ignore[index]
        path = self.serializers.save(self._cache[ref], self.path_for(ref))
        self._records.register(ref, path, store_owned=True)
        return path

    def adopt(self, ref: Ref, path: Path, *, store_owned: bool) -> None:
        """Register a copy this process did not write, such as a runner's output."""
        self._records.register(ref, path, store_owned=store_owned)

    def has_copy(self, ref: Ref) -> bool:
        loc = self._records.location(ref)
        return loc is not None and loc[0].exists()

    def in_cache(self, ref: Ref) -> bool:
        return ref in self._cache

    def array(self, ref: Ref) -> Any:
        """The scipp object: from the cache, else loaded from disk and cached."""
        if ref in self._cache:
            return self._cache[ref]
        value = self.serializers.load(self._disk_path(ref))
        self._cache[ref] = value
        return value

    def path(self, ref: Ref) -> Path:
        """A disk copy, written out first when only the cache holds the value."""
        if ref in self._cache and not self.has_copy(ref):
            return self.write_out(ref)
        return self._disk_path(ref)

    def available(self, ref: Ref) -> bool:
        return ref in self._cache or self.has_copy(ref)

    def _disk_path(self, ref: Ref) -> Path:
        loc = self._records.location(ref)
        if loc is None or not loc[0].exists():
            raise MissingCopyError(f'No copy of {ref}')
        return loc[0]

    def drop(self, ref: Ref) -> None:
        """Drop the bytes of a store-owned copy; the record stays."""
        loc = self._records.location(ref)
        if loc is None:
            return
        path, store_owned = loc
        if not store_owned:
            raise PermissionError(f'{ref} is not a store copy; drop it where it lives')
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        self._records.unregister(ref)
        self._cache.pop(ref, None)

    def evict(self, ref: Ref) -> None:
        """Forget a cached value; disk copies are untouched."""
        self._cache.pop(ref, None)

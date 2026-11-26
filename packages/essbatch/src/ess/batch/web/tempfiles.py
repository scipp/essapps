# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A file that a person sends or gets, in a folder of its own."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import request_error_text


@contextmanager
def named_file(name: str, content: bytes | None = None) -> Iterator[Path]:
    """A path named ``name`` in a folder of its own, holding ``content`` if given."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / Path(name).name
        if content is not None:
            path.write_bytes(content)
        yield path


def read_file[T](name: str, content: bytes, read: Callable[[Path], T]) -> T:
    """
    What ``read`` returns for the file ``name`` with ``content``, which a
    person uploaded. Raises ``ValueError`` for an empty file, and for an
    ``OSError``, ``ValueError`` or ``KeyError`` of ``read``, with ``name`` in
    place of the temporary path.
    """
    if not content:
        raise ValueError('the file is empty')
    with named_file(name, content) as path:
        try:
            return read(path)
        except (OSError, ValueError, KeyError) as error:
            said = (
                request_error_text(error)
                .replace(f'{path}: ', '')
                .replace(str(path), name)
            )
            raise ValueError(said) from None

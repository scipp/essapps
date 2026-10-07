# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The store: where persisted outputs are written, and read back.

The backend is given a store; each deployment configures its own, and tests
use a fake (``testing.FakeStore``). A value is found by the record's ID and
the output's name, so history names no file. A store reads a value back as
the workflow returned it. The format of each output type and the layout of
the store are its own. A store may hold values that no history names, such
as those of a record cancelled while it was written; nothing reads them, and
they go with the proposal's area.
"""

from __future__ import annotations

from typing import Any, Protocol


class Store(Protocol):
    def write(self, record: str, output: str, value: Any) -> None:
        """
        Write the value of an output; raises if it cannot. A write replaces
        a value written before under the same names, as a record run again
        after a restart writes its outputs again.
        """

    def read(self, record: str, output: str) -> Any:
        """The value of an output that was written."""

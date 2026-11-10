# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Files as a client sees them."""

import threading
import time
from itertools import islice

import pytest

from ess.apps import Client, Files, Selector, Workspace, file
from ess.apps.testing import FakeFiles
from ess.spec import FileRef


def test_a_client_sees_only_the_files_of_its_workspace_s_proposal(
    workspace: Workspace, files: FakeFiles
) -> None:
    mine = Client(workspace, submitter='anna')
    p2 = Workspace(proposal='p2', files=files, bind=[])
    theirs = Client(p2, submitter='eve', on_close=p2.close)
    other = files.measure(4, 4.0, proposal='p2')
    existing = [files.resolve(file(run=n)) for n in (1, 2)]

    assert mine.files.list(Selector()) == existing
    assert theirs.files.list(Selector()) == [other]
    watch = mine.files.watch(Selector())
    assert list(islice(watch, 2)) == existing
    files.measure(6, 6.0, proposal='p2')
    later = files.measure(5, 5.0)
    assert next(watch) == later
    assert theirs.files.metadata(other)['run'] == 4
    with pytest.raises(KeyError, match='in proposal p1'):
        mine.files.metadata(other)
    theirs.close()


def test_watch_lists_again_until_a_file_is_new(
    workspace: Workspace, files: FakeFiles
) -> None:
    client = Client(workspace, submitter='anna')
    watch = client.files.watch(Selector(), interval=0.01)
    list(islice(watch, 2))
    arrives = threading.Timer(0.05, files.measure, args=(5, 5.0))

    arrives.start()
    found = next(watch)
    arrives.join()

    assert found == files.resolve(file(run=5))


def test_metadata_takes_one_or_a_list_of_files(
    workspace: Workspace, files: FakeFiles
) -> None:
    client = Client(workspace, submitter='anna')
    refs = [files.resolve(file(run=n)) for n in (1, 2)]

    assert client.files.metadata(refs[0])['run'] == 1
    assert [m['run'] for m in client.files.metadata(refs)] == [1, 2]


class FilesLater:
    """Answers ``files`` with no file for its first ``empty`` calls."""

    def __init__(self, ref: FileRef, empty: int) -> None:
        self._ref = ref
        self._empty = empty

    def files(self, selector: Selector) -> list[FileRef]:
        self._empty -= 1
        return [] if self._empty >= 0 else [self._ref]


def test_watch_waits_for_the_interval_between_listings() -> None:
    ref = FileRef(file='uuid:run-1')
    watch = Files(FilesLater(ref, empty=3)).watch(Selector(), interval=0.01)

    start = time.monotonic()
    assert next(watch) == ref
    assert time.monotonic() - start >= 3 * 0.01

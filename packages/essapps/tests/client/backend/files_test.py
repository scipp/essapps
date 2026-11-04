# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Files as a client sees them, and local paths reaching a workflow."""

from itertools import islice

import pytest

from ess.apps import Backend, Client, Selector, file
from ess.apps.testing import FakeFiles


def test_a_client_sees_only_the_files_of_its_proposal(
    backend: Backend, files: FakeFiles
) -> None:
    mine = Client(backend, proposal='p1', submitter='anna')
    theirs = Client(backend, proposal='p2', submitter='eve')
    other = files.measure(4, 4.0, proposal='p2')
    existing = [files.resolve(file(run=n)) for n in (1, 2, 3)]

    assert mine.files.list(Selector()) == existing
    assert theirs.files.list(Selector()) == [other]
    watch = mine.files.watch(Selector())
    assert list(islice(watch, 3)) == existing
    files.measure(6, 6.0, proposal='p2')
    later = files.measure(5, 5.0)
    assert next(watch) == later
    assert theirs.files.metadata(other)['run'] == 4
    with pytest.raises(KeyError, match='in proposal p1'):
        mine.files.metadata(other)

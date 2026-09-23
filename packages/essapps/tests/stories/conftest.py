# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A small facility for the user stories: runs measured into a catalogue, a
notebook's client, and a client shaped like the shared service.

The toy workflows of :mod:`ess.apps.examples` stand in for the instrument
workflows the stories name.
"""

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from ess.apps.client import Client, local
from ess.apps.examples import registry, write_run
from ess.apps.sources import Dataset
from ess.apps.spec import DatasetRef
from ess.apps.testing import FakeDatasetSource, FakePublisher

Measure = Callable[..., DatasetRef]


@pytest.fixture
def catalogue() -> FakeDatasetSource:
    """The datasets of the proposal as they arrive; stands in for SciCat."""
    return FakeDatasetSource()


@pytest.fixture
def measure(tmp_path: Path, catalogue: FakeDatasetSource) -> Measure:
    """
    Measure a run: its file lands and the catalogue lists it with its metadata.

    Returns the run's identity, which is how a request names it.
    """
    folder = tmp_path / 'raw'
    folder.mkdir()

    def measure(
        run: int, counts: Sequence[float] = (1.0, 2.0, 3.0, 4.0), **metadata: Any
    ) -> DatasetRef:
        path = write_run(folder / f'dream_{run}.h5', list(counts))
        dataset = Dataset(path=path, instrument='dream', run=run, metadata=metadata)
        catalogue.add(dataset)
        return dataset.ref

    return measure


@pytest.fixture
def scicat() -> FakePublisher:
    """Where published results go."""
    return FakePublisher()


def _client(root: Path, registry: Any, **kwargs: Any) -> Client:
    return local(
        root,
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry,
        **kwargs,
    )


@pytest.fixture
def client(
    tmp_path: Path, catalogue: FakeDatasetSource, scicat: FakePublisher
) -> Iterator[Client]:
    """A notebook: client, backend, and a session in this process."""
    client = _client(
        tmp_path / 'notebook',
        registry(),
        sources=[catalogue],
        publishers={'scicat': scicat},
    )
    yield client
    client.close()


@pytest.fixture
def service(
    tmp_path: Path, catalogue: FakeDatasetSource, scicat: FakePublisher
) -> Iterator[Client]:
    """A client of the shared service's shape: every run in a throwaway process."""
    client = _client(
        tmp_path / 'service',
        'ess.apps.examples:registry',
        throwaway=True,
        sources=[catalogue],
        publishers={'scicat': scicat},
    )
    yield client
    client.close()

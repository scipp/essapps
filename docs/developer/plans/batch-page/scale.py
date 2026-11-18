# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The cost of the batch page's server at N rows, without HTTP: a toy workflow,
three columns, an in-memory workspace. Prints seconds as JSON, each as
``[best, mean]`` of three calls, for two regimes: ``A`` before any row is
reduced, ``B`` after all are.

Usage: scale.py N
"""

import cProfile
import importlib.util
import io
import itertools
import json
import pstats
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ess.apps import Client, local
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import Server
from ess.apps.batch.web.models import CellEdit, Reduce
from ess.apps.testing import FakeFiles, StagedFunction, read_file

TOYS = Path(__file__).resolve().parents[4] / 'packages/essapps/tests/batch/toys.py'
AT = frozenset({'sample', 'transmission'})


def _toys() -> Any:
    """The toy workflow of the tests of ``ess.apps.batch``."""
    spec = importlib.util.spec_from_file_location('toys', TOYS)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


toys = _toys()


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


def reduce(params: Any) -> Any:
    return toys.Outputs(total=read_file(params.sample) / params.thickness)


def build(n: int, tmp: Path) -> tuple[FakeFiles, Client, Batch, Server]:
    fake = FakeFiles('p1', tmp)
    for i in range(1, n + 1):
        fake.measure(i, float(i), role='sample', sample=f's{i}')
        fake.measure(i + 10**6, 1.0, role='transmission', sample=f's{i}')
    files = CorrectedFiles(fake)
    client = local(
        proposal='p1', files=files, bind=[StagedFunction(toys.STAGED, reduce)]
    )
    batch = Batch(
        client,
        toys.STAGED,
        columns=toys.COLUMNS,
        label='scan',
        source=toys.pair,
        staged=True,
    )
    batch.scan()
    server = Server(batch, name='name', files=files, marks=Marks, at=AT)
    return fake, client, batch, server


def timeit(f: Callable[[], Any], k: int = 3) -> tuple[float, float, Any]:
    """The best and the mean seconds of ``k`` calls, and the last result."""
    seconds = []
    result = None
    for _ in range(k):
        start = time.perf_counter()
        result = f()
        seconds.append(time.perf_counter() - start)
    return min(seconds), sum(seconds) / k, result


def drain(server: Server, limit: float = 3600) -> float:
    """Wait until no row runs; the seconds it took."""
    start = time.perf_counter()
    while server.state().action.running:
        time.sleep(0.2)
        if time.perf_counter() - start > limit:
            raise TimeoutError(f'rows still running after {limit} s')
    return time.perf_counter() - start


def measure(
    server: Server, batch: Batch, fake: FakeFiles, regime: str, runs: Iterator[int]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    state = server.state()
    key = state.rows[0].key
    out['state'] = timeit(server.state)[:2]
    best, mean, text = timeit(state.model_dump_json)
    out['json'] = best, mean
    out['json_MB'] = len(text) / 1e6
    server.poll(0, '')
    version = server.version
    best, mean, polled = timeit(lambda: server.poll(version, server.start))
    out['poll'] = best, mean
    out['poll_none'] = polled is None
    values = iter([2.0, 3.0, 4.0])
    out['edit'] = timeit(
        lambda: server.edit(
            CellEdit(keys=[key], column='thickness', value=next(values))
        )
    )[:2]
    out['runs'] = timeit(server.runs)[:2]
    out['batch_state'] = timeit(batch.state)[:2]
    if regime == 'A':
        start = time.perf_counter()
        server.step()
        out['step_first'] = time.perf_counter() - start
        drain(server)
    out['step_idle'] = timeit(server.step)[:2]
    if regime == 'B':

        def arrive() -> float:
            run = next(runs)
            fake.measure(run, float(run), role='sample', sample=f's{run}')
            fake.measure(run + 10**6, 1.0, role='transmission', sample=f's{run}')
            start = time.perf_counter()
            server.step()
            return time.perf_counter() - start

        seconds = []
        for _ in range(3):
            seconds.append(arrive())
            drain(server)
        out['step_new'] = min(seconds), sum(seconds) / 3
    return out


def main(n: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as directory:
        start = time.perf_counter()
        fake, client, batch, server = build(n, Path(directory))
        result['setup'] = time.perf_counter() - start
        runs = itertools.count(n + 1)
        result['A'] = measure(server, batch, fake, 'A', runs)
        start = time.perf_counter()
        server.reduce(Reduce(which='all'))
        result['reduce_submit'] = time.perf_counter() - start
        result['reduce_drain'] = drain(server)
        rows = server.state().rows
        result['statuses'] = {
            s: sum(r.status == s for r in rows) for s in {r.status for r in rows}
        }
        result['B'] = measure(server, batch, fake, 'B', runs)
        if n <= 1000:
            profile = cProfile.Profile()
            profile.enable()
            server.state()
            profile.disable()
            text = io.StringIO()
            stats = pstats.Stats(profile, stream=text)
            stats.sort_stats('cumulative').print_stats(25)
            result['profile of server.state()'] = text.getvalue().splitlines()
        client.close()
    return result


if __name__ == '__main__':
    sys.stdout.write(json.dumps(main(int(sys.argv[1])), indent=1) + '\n')

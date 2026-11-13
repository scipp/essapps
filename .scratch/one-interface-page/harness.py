"""
The batch web page (``ess.apps.batch.web``, unchanged) on a batch that an
``Automatic`` steps, as ``one-interface.md`` proposes: a thread steps the loop
every second under the server's lock, with the settings of the page's form.

Runs on the checkout (branch ``184-automatic-as-batch``), with the toy
reduction of ``packages/essapps/tests/batch/toys.py``: a row is a sample run
and the transmission run of the same sample name. A sample file holding a
negative number fails.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import uvicorn

from ess.apps import Client, Workspace
from ess.apps.batch import Batch
from ess.apps.batch.auto import Automatic
from ess.apps.batch.web import create_app
from ess.apps.testing import FakeFiles, FakeStore, read_file
from ess.spec import function_binding

ROOT = Path('/workspace/essapps/packages/essapps/tests/batch/toys.py')
_spec = importlib.util.spec_from_file_location('batch_toys', ROOT)
toys = importlib.util.module_from_spec(_spec)
sys.modules['batch_toys'] = toys
_spec.loader.exec_module(toys)


DELAY = 0.0
"""Seconds each reduction takes."""


def _reduce(params: Any) -> Any:
    time.sleep(DELAY)
    value = read_file(params.sample)
    if value < 0:
        raise ValueError('the sample holds a negative number')
    offset = 0.0 if params.offset is None else params.offset.value
    return toys.Outputs(total=value / params.thickness + offset)


def draw(shown: Any) -> dict[str, Any]:
    return {'data': [{'y': [1, 2], 'name': s.name} for s in shown], 'layout': {}}


class Setup:
    """Files, a workspace, a batch, the page's server over HTTP, and the loop."""

    def __init__(self, interval: float = 1.0) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix='one-interface-page-'))
        self.files = FakeFiles('p1', self.directory)
        self.workspace = Workspace(
            proposal='p1',
            files=self.files,
            bind=[function_binding(toys.PLAIN, _reduce)],
            store=FakeStore(),
        )
        self.client = Client(self.workspace, submitter='loop')
        self.batch = Batch(
            self.client,
            toys.PLAIN,
            columns=toys.COLUMNS,
            label='auto',
            source=toys.pair,
            persist=True,
        )
        self.app = create_app(self.batch, name='name', draw=draw)
        self.server = self.app.state.server
        self.auto = Automatic(
            self.batch, at='sample', settings=lambda: self.server.settings.params
        )
        self.interval = interval
        self.steps: list[tuple[float, float, int]] = []  # (start, ms, submitted)
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self._http: uvicorn.Server | None = None
        self._http_thread: threading.Thread | None = None
        self.url = ''

    def sample(self, run: int, name: str, *, partner: bool = True, value: float | None = None) -> None:
        self.files.measure(run, float(run) if value is None else value, role='sample', sample=name)
        if partner:
            self.transmission(run + 1_000_000, name)

    def transmission(self, run: int, name: str) -> None:
        self.files.measure(run, 1.0, role='transmission', sample=name)

    def step(self) -> int:
        t0 = time.perf_counter()
        with self.server.lock:
            done = self.auto.step()
        self.steps.append((t0, (time.perf_counter() - t0) * 1e3, len(done)))
        return len(done)

    def fill(self, n: int) -> None:
        """``n`` rows, reduced, before the page opens."""
        for i in range(n):
            self.sample(i, f's{i}')
        self.step()
        self.wait_all()

    def wait_all(self) -> None:
        records = [s.record for s in self.batch.state().values() if s.record]
        self.client.wait(records)

    def start_loop(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception:
                    self.errors.append(traceback.format_exc())
                self._stop.wait(self.interval)

        self._loop = threading.Thread(target=loop, daemon=True)
        self._loop.start()

    def start_http(self) -> str:
        with socket.socket() as s:
            s.bind(('localhost', 0))
            port = s.getsockname()[1]
        self._http = uvicorn.Server(
            uvicorn.Config(self.app, host='localhost', port=port, log_level='warning')
        )
        self._http_thread = threading.Thread(target=self._http.run, daemon=True)
        self._http_thread.start()
        while not self._http.started:
            time.sleep(0.01)
        self.url = f'http://localhost:{port}/'
        return self.url

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            self._loop.join()
        if self._http is not None:
            self._http.should_exit = True
            self._http_thread.join()
        self.batch.close()
        self.workspace.close()

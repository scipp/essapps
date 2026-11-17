# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Serve the demo page of the batch, with N fake samples, to probes in a browser."""

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from playwright import sync_api

from ess.apps.batch.web import demo


def show(*parts: object) -> None:
    """Write what a probe found to standard output, as ``print`` does."""
    sys.stdout.write(' '.join(map(str, parts)) + '\n')
    sys.stdout.flush()


def runs(n: int) -> list[Any]:
    """The runs of ``n`` samples, each with its transmission, and one background."""
    out = [(demo.FIRST_RUN, {'level': 0.02}, 'background', 'empty cell')]
    run = demo.FIRST_RUN
    for i in range(n):
        name, radius, scale = demo.SAMPLES[i % len(demo.SAMPLES)]
        name = f'{name} #{i}'
        values = {
            'radius': radius,
            'scale': scale,
            'incoherent': 0.03,
            'events': 2_000_000,
            'seed': i,
        }
        out.append((run + 1, values, 'sample', name))
        out.append((run + 2, {'transmission': 0.9}, 'transmission', name))
        run += 2
    return out


def arrive(served: dict[str, Any], run: int) -> None:
    """Make a background run arrive in the demo's folder."""
    runs = [r for group in demo.measurements() for r in group]
    runs.append((run, {'level': 0.03}, 'background', 'empty cell 2'))
    demo.measure(served['directory'], runs)


def post(url: str, body: Any) -> Any:
    """POST ``body`` as JSON, and the JSON answered."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310  a URL of serve_demo
        url, data, {'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(request) as answer:  # noqa: S310
        return json.load(answer)


@contextmanager
def serve_demo(n: int | None = None, follow: float | None = None) -> Iterator[dict]:
    """
    Serve the demo on a free port, in a folder of its own: its runs if ``n`` is
    ``None``, else those of :func:`runs`.
    """
    directory = Path(tempfile.mkdtemp(prefix='batch-probe-'))
    try:
        if n is not None:
            demo.measure(directory, runs(n))
        client, files, definitions = demo.open_demo(directory)
        with client:
            start = time.perf_counter()
            app = demo.build(
                client, files, definitions, folder=directory.name, follow=follow
            )
            built = time.perf_counter() - start
            with socket.socket() as s:
                s.bind(('localhost', 0))
                port = s.getsockname()[1]
            config = uvicorn.Config(
                app, host='localhost', port=port, log_level='warning'
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            while not server.started:
                time.sleep(0.01)
            try:
                yield {
                    'url': f'http://localhost:{port}/',
                    'app': app,
                    'server': app.state.server,
                    'client': client,
                    'directory': directory,
                    'built': built,
                }
            finally:
                server.should_exit = True
                thread.join()
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def browser() -> Iterator[Any]:
    """Chromium from ``playwright install chromium``, or ``chromium`` on the PATH."""
    with sync_api.sync_playwright() as p:
        bundled = p.chromium.executable_path
        exe = bundled if Path(bundled).exists() else shutil.which('chromium')
        b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


def new_page(
    b: Any, url: str, errors: list[str] | None = None, timeout: float = 30_000
) -> Any:
    """A page of ``url`` once it shows rows; console errors go to ``errors``."""
    page = b.new_page(viewport={'width': 1440, 'height': 900})
    page.set_default_timeout(timeout)
    if errors is not None:
        page.on(
            'console',
            lambda m: (
                errors.append(f'console: {m.text}') if m.type == 'error' else None
            ),
        )
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    page.goto(url)
    page.wait_for_selector('#rows .tabulator-row')
    return page

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
F8: "select all" at N rows asks ``/api/plot`` with every key in the URL. Above
some length the server answers 400, and the page says nothing.

Usage: probe_select_all.py N [N ...]
"""

import sys
import time

from harness import browser, new_page, serve_demo, show


def select_all(n: int) -> None:
    with serve_demo(n) as served, browser() as b:
        errors: list[str] = []
        page = new_page(b, served['url'], errors, timeout=600_000)  # slow at 10 000
        plots: list[tuple[int, int]] = []
        page.on(
            'response',
            lambda r: (
                plots.append((r.status, len(r.url))) if '/api/plot' in r.url else None
            ),
        )
        start = time.perf_counter()
        page.click('#rows .tabulator-header .tabulator-row-header input')
        page.wait_for_function(
            f'document.getElementById("selection").textContent === "{n} selected"',
            timeout=600_000,
        )
        selected = time.perf_counter() - start
        page.wait_for_timeout(2000)
        message = page.inner_text('#message') if page.is_visible('#message') else ''
        show(f'N={n}: "{n} selected" after {selected * 1e3:.0f} ms')
        show('  /api/plot (status, length of URL):', plots)
        show('  message shown:', repr(message))
        show('  errors:', errors[:3])


for n in map(int, sys.argv[1:]):
    select_all(n)

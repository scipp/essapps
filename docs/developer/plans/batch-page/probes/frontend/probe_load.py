# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
F7: where the first load of the page at N rows spends its time.

Usage: probe_load.py N [N ...]
"""

import sys
import time

from harness import browser, serve_demo, show

LONG_TASKS = """(() => {
  window.__long = [];
  new PerformanceObserver((l) => l.getEntries().forEach(
    (e) => window.__long.push([Math.round(e.startTime), Math.round(e.duration)])
  )).observe({type: 'longtask', buffered: true});
})();"""
API = ('state', 'runs', 'plot')


def load(n: int) -> None:
    """Load the page at ``n`` rows and say when its rows show."""
    with serve_demo(n) as served, browser() as b:
        context = b.new_context(viewport={'width': 1440, 'height': 900})
        context.add_init_script(LONG_TASKS)
        page = context.new_page()
        answers: list[tuple[int, str, int]] = []
        start = time.perf_counter()
        page.on(
            'response',
            lambda r: answers.append(
                (
                    round((time.perf_counter() - start) * 1e3),
                    r.url.split('/')[-1][:25],
                    r.status,
                )
            ),
        )
        page.goto(served['url'])
        page.wait_for_selector('#rows .tabulator-row', timeout=300_000)
        shown = time.perf_counter() - start
        page.wait_for_timeout(1000)
        api = [a for a in answers if a[1].startswith(API)]
        show(f'N={n}: rows shown at {shown * 1e3:.0f} ms')
        show('  answers [ms, url, status]:', api[:6])
        show('  long tasks [start, ms]:', page.evaluate('window.__long')[:8])


for n in map(int, sys.argv[1:]):
    load(n)

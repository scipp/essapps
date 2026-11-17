# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The cost of the page at N rows: the size of the state, and the time the server
takes for an action and the page to apply the state it answers.

Usage: probe_perf.py N [N ...]
"""

import gzip
import json
import sys
import time
import urllib.request
from typing import Any

from harness import browser, serve_demo, show

# Times the parsing of each JSON answer, the task that follows it, and the next
# frame; collects long tasks.
TIMING = """
(() => {
  window.__t = [];
  const json0 = Response.prototype.json;
  Response.prototype.json = async function () {
    const t0 = performance.now();
    const v = await json0.call(this);
    const rec = {
      url: this.url.replace(location.origin, '').slice(0, 30),
      parse: performance.now() - t0,
      t1: performance.now(),
    };
    window.__t.push(rec);
    setTimeout(() => {
      rec.task = performance.now() - rec.t1;
      requestAnimationFrame(
        () => setTimeout(() => (rec.frame = performance.now() - rec.t1), 0)
      );
    }, 0);
    return v;
  };
  window.__long = [];
  new PerformanceObserver(
    (l) => l.getEntries().forEach((e) => window.__long.push([e.startTime, e.duration]))
  ).observe({type: 'longtask', buffered: true});
})();
"""
STATES = """window.__t.filter((r) => ['/api/state', '/api/cells', '/api/scan'].some(
  (u) => r.url.startsWith(u)
))"""


def http(url: str, body: Any = None) -> tuple[bytes, float]:
    """The answer to a GET, or to a POST of ``body``, and the seconds it took."""
    start = time.perf_counter()
    if body is None:
        request = urllib.request.Request(url)  # noqa: S310  a URL of serve_demo
    else:
        data = json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310
            url, data, {'Content-Type': 'application/json'}
        )
    with urllib.request.urlopen(request) as answer:  # noqa: S310
        raw = answer.read()
    return raw, time.perf_counter() - start


def last_state_timing(page: Any, after_ms: int) -> Any:
    page.wait_for_timeout(after_ms)
    timings = page.evaluate(STATES)
    return timings[-1] if timings else None


def run(n: int) -> None:
    show(f'=== N = {n}')
    with serve_demo(n) as served, browser() as b:
        api = served['url'] + 'api/'
        show(f'build (scan) {served["built"]:.2f} s')
        raw, dt = http(api + 'state')
        state = json.loads(raw)
        show(
            f'GET /api/state: {len(raw) / 1e3:.0f} kB,'
            f' gzip {len(gzip.compress(raw)) / 1e3:.0f} kB,'
            f' server {dt * 1e3:.0f} ms, rows {len(state["rows"])}'
        )
        context = b.new_context(viewport={'width': 1440, 'height': 900})
        context.add_init_script(TIMING)
        page = context.new_page()
        errors: list[str] = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        start = time.perf_counter()
        page.goto(served['url'])
        page.wait_for_selector('#rows .tabulator-row', timeout=300_000)
        page.wait_for_function(
            'document.querySelector("#legend") && window.__t.length > 0'
        )
        load = time.perf_counter() - start
        first = last_state_timing(page, 500)
        show(
            f'first load to rows shown {load * 1e3:.0f} ms;'
            f' first state: parse {first["parse"]:.0f} ms,'
            f' task {first["task"]:.0f} ms, to frame {first.get("frame", -1):.0f} ms'
        )
        keys = [r['key'] for r in state['rows']]

        def after(label: str, body: Any, path: str) -> None:
            page.evaluate('window.__long = []')
            _, dt = http(api + path, body)
            rec = last_state_timing(page, 2500 if n < 5000 else 8000)
            long = [round(d) for _, d in page.evaluate('window.__long')]
            show(
                f'{label}: server {dt * 1e3:.0f} ms;'
                f' page parse {rec["parse"]:.0f} ms, task {rec["task"]:.0f} ms,'
                f' to next frame {rec.get("frame", -1):.0f} ms; long tasks {long}'
            )

        after('rescan, no row changes', {}, 'scan')
        one = {'keys': keys[:1], 'column': 'thickness', 'value': 2.0}
        after('edit one row', one, 'cells')
        every = {'keys': keys, 'column': 'thickness', 'value': 3.0}
        after('edit every row', every, 'cells')
        show('errors:', errors[:3])
        context.close()


if __name__ == '__main__':
    for n in map(int, sys.argv[1:]):
        run(n)

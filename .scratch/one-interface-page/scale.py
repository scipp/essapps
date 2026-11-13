"""
The page at N rows with the loop running: what one new row costs the server,
the wire, and the browser.

Run: /workspace/essapps/.scratch/venv-container/bin/python scale.py N
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright import sync_api

from harness import Setup


def patch_cell() -> None:
    """Measurement only: forms.cell without its search over every option."""
    from ess.apps.batch.web import forms
    from ess.apps.fields import UNSET

    cache: dict[int, tuple[Any, dict[str, str]]] = {}
    original = forms.cell

    def cell(field: Any, value: Any) -> Any:
        inner, _ = forms._inner(field)
        options = getattr(inner, 'options', None)
        if value is UNSET or value is None or not options:
            return original(field, value)
        held = cache.get(id(options))
        if held is None or held[0] is not options:
            reverse: dict[str, str] = {}
            for k, v in options.items():
                reverse.setdefault(repr(v), k)
            held = cache[id(options)] = (options, reverse)
        return held[1].get(repr(value))

    forms.cell = cell

N = int(sys.argv[1])
JS_ROWS = "() => document.querySelectorAll('#rows .tabulator-row').length"
LONG = """() => { window.longtasks = [];
  new PerformanceObserver(l => window.longtasks.push(...l.getEntries().map(e => [e.startTime, e.duration])))
    .observe({type: 'longtask', buffered: true}); }"""


def timed(fn: Any) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def say(what: str, *rest: Any) -> None:
    print(f'N={N:<7} {what:<40}', *rest, flush=True)


if os.environ.get('PATCH'):
    patch_cell()
    print('forms.cell patched: a dict from value to option in place of a linear search')
s = Setup()
t_fill, _ = timed(lambda: s.fill(N))
say('fill (scan, submit, wait)', f'{t_fill:.1f} s')
srv = s.server
srv._relist()  # as each poll that sees a new record does: the pickers get every file
st = srv.state()
cols = sum(len(c.model_dump_json()) for c in st.columns)
say('  of the JSON: columns (pickers of files)', f'{cols / 1e6:.2f} MB; rows {sum(len(r.model_dump_json()) for r in st.rows) / 1e6:.2f} MB')
t_state = min(timed(srv.state)[0] for _ in range(3))
size = len(srv.state().model_dump_json())
say('Server.state()', f'{t_state * 1e3:.0f} ms, JSON {size / 1e6:.2f} MB')
t_bstate = min(timed(lambda: s.batch.state(srv.settings.params))[0] for _ in range(3))
say('  of it Batch.state(settings)', f'{t_bstate * 1e3:.0f} ms')
t_plot = min(timed(lambda: srv.plot([]))[0] for _ in range(2))
say('Server.plot([])', f'{t_plot * 1e3:.0f} ms')
t_row = min(timed(lambda: srv.row('5'))[0] for _ in range(2))
say('Server.row(key)', f'{t_row * 1e3:.0f} ms')
t_step = min(timed(s.step)[0] for _ in range(3))
say('loop step, nothing new', f'{t_step * 1e3:.0f} ms')

url = s.start_http()
t_http, body = timed(lambda: urllib.request.urlopen(url + 'api/state').read())
say('GET /api/state over HTTP', f'{t_http * 1e3:.0f} ms, {len(body) / 1e6:.2f} MB')
if os.environ.get('NOBROWSER'):
    s.stop()
    sys.exit()
s.steps.clear()
s.start_loop()

with sync_api.sync_playwright() as pw:
    bundled = pw.chromium.executable_path
    browser = pw.chromium.launch(
        executable_path=bundled if Path(bundled).exists() else shutil.which('chromium')
    )
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.set_default_timeout(600_000)
    states: list[int] = []
    page.on(
        'response',
        lambda r: states.append(int(r.headers.get('content-length', 0)))
        if '/api/state' in r.url and r.status == 200
        else None,
    )
    page.add_init_script(f'({LONG})()')
    t_open, _ = timed(
        lambda: (page.goto(url), page.wait_for_function(f'() => ({JS_ROWS})() >= {N}', timeout=600_000))
    )
    say('page open to all rows drawn', f'{t_open:.1f} s')
    page.evaluate('() => { window.longtasks = []; }')
    time.sleep(3)
    idle_long = page.evaluate('() => window.longtasks.reduce((a, [, d]) => a + d, 0)')
    say('browser busy over 3 s, nothing new', f'{idle_long:.0f} ms')
    for k in range(3):
        page.evaluate('() => { window.longtasks = []; }')
        states.clear()
        s.sample(50_000_000 + k, f'new{k}')
        t_shown, _ = timed(
            lambda: page.wait_for_function(f'() => ({JS_ROWS})() >= {N + k + 1}', timeout=600_000)
        )
        time.sleep(2)
        tasks = page.evaluate('() => window.longtasks')
        busy = sum(d for _, d in tasks)
        longest = max((d for _, d in tasks), default=0)
        say(
            f'one new row #{k}: shown after',
            f'{t_shown:.1f} s; browser busy {busy:.0f} ms (longest {longest:.0f}); '
            f'states received {len(states)} of {sum(states) / 1e6:.2f} MB',
        )
    # scrolling stays responsive? time a scroll
    t_scroll = page.evaluate(
        """async () => { const h = document.querySelector('#rows .tabulator-tableholder');
        const t0 = performance.now(); h.scrollTop = h.scrollHeight / 2;
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        return performance.now() - t0; }"""
    )
    say('scroll to the middle, two frames', f'{t_scroll:.0f} ms')
    heap = page.evaluate('() => performance.memory?.usedJSHeapSize ?? 0')
    say('JS heap', f'{heap / 1e6:.0f} MB')
    browser.close()

steps = [ms for _, ms, _ in s.steps]
say('loop steps while the page was open', f'{len(steps)} steps, mean {sum(steps) / len(steps):.0f} ms, max {max(steps):.0f} ms')
say('loop errors', s.errors[:1])
s.stop()

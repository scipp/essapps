"""
Runs probe/index.html in headless Chromium: Tabulator at N rows, one new row
shown by replaceData, addData, or updateData, with the 'basic' renderer of
tables.js and the 'virtual' one.

Run: /workspace/essapps/.scratch/venv-container/bin/python probe.py
"""

import functools
import sys
import http.server
import shutil
import threading
from pathlib import Path

from playwright import sync_api

HERE = Path(__file__).parent / 'probe'
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


handler = functools.partial(Quiet, directory=str(HERE))
httpd = http.server.ThreadingHTTPServer(('localhost', 0), handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f'http://localhost:{httpd.server_address[1]}/index.html'

with sync_api.sync_playwright() as pw:
    bundled = pw.chromium.executable_path
    browser = pw.chromium.launch(
        executable_path=bundled if Path(bundled).exists() else shutil.which('chromium')
    )
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.set_default_timeout(900_000)
    page.goto(url)
    page.wait_for_function('() => window.ready')
    for renderer in sys.argv[1:] or ('basic', 'virtual'):
        for n in (300, 3000, 10_000, 100_000):
            if renderer == 'basic' and n == 100_000:
                print(f'{renderer:<8} N={n:<7} not run (10 000 took long already)')
                continue
            out = page.evaluate(f'() => window.probe({n}, {renderer!r})')
            print(
                f'{renderer:<8} N={n:<7} initial {out["initial"]:8.0f} ms  replaceData {out["replace"]:8.0f} ms '
                f'scroll {out["replaceScroll"]}  addData {out["add"]:6.0f} ms scroll {out["addScroll"]}  '
                f'updateData {out["update"]:5.0f} ms  heap {out["heapMB"]:.0f} MB',
                flush=True,
            )
    browser.close()
httpd.shutdown()

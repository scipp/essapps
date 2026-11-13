"""
The batch page in headless Chromium while the loop steps the same Batch and
files arrive. Prints what each situation shows.

Run: /workspace/essapps/.scratch/venv-container/bin/python browser.py [N]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import Counter
from typing import Any

from playwright import sync_api

from harness import Setup

N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
JS_ROWS = "() => document.querySelectorAll('#rows .tabulator-row').length"
JS_KEYS = """() => [...document.querySelectorAll('#rows .tabulator-row')]
  .map(r => r.querySelector('[tabulator-field=name]')?.textContent)"""
HOLDER = '#rows .tabulator-tableholder'


def say(what: str, *rest: Any) -> None:
    print(f'{what:<44}', *rest, flush=True)


def wait_rows(page: Any, n: int, timeout: float = 10.0) -> float | None:
    """Seconds until the table shows ``n`` rows, or None."""
    t0 = time.perf_counter()
    try:
        page.wait_for_function(f'() => ({JS_ROWS})() >= {n}', timeout=timeout * 1e3)
    except sync_api.TimeoutError:
        return None
    return time.perf_counter() - t0


def status_of(page: Any, name: str) -> str:
    row = page.locator('#rows .tabulator-row', has_text=name).first
    return row.locator('[tabulator-field=status]').inner_text()


def top_row(page: Any) -> str:
    return page.evaluate(
        """() => {
      const holder = document.querySelector('#rows .tabulator-tableholder');
      const top = holder.getBoundingClientRect().top;
      const row = [...holder.querySelectorAll('.tabulator-row')]
        .find(r => r.getBoundingClientRect().bottom > top + 2);
      return row?.querySelector('[tabulator-field=name]')?.textContent;
    }"""
    )


def scroll_top(page: Any) -> float:
    return page.evaluate(f"() => document.querySelector('{HOLDER}').scrollTop")


def primary(page: Any) -> str:
    return page.evaluate(
        """() => {
      const b = document.getElementById('reduce');
      return `${b.querySelector('.label').textContent} (disabled: ${b.disabled})`;
    }"""
    )


s = Setup()
s.fill(N)
url = s.start_http()
s.start_loop()
run = 10_000_000  # runs of files that arrive
requests: Counter[str] = Counter()

with sync_api.sync_playwright() as pw:
    from pathlib import Path
    import shutil
    bundled = pw.chromium.executable_path
    browser = pw.chromium.launch(executable_path=bundled if Path(bundled).exists() else shutil.which('chromium'))
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors: list[str] = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))

    def counted(response: Any) -> None:
        path = response.url.split('?')[0].removeprefix(url)
        requests[f'{path} {response.status}'] += 1

    page.on('response', counted)
    t0 = time.perf_counter()
    page.goto(url)
    page.wait_for_function(f'() => ({JS_ROWS})() >= {N}', timeout=120_000)
    say('page open, rows drawn', f'{time.perf_counter() - t0:.2f} s, {N} rows')

    def arrive(name: str, **kw: Any) -> None:
        global run
        run += 1
        s.sample(run, name, **kw)

    # 1. A new row with its partner, nothing clicked
    n = N
    requests.clear()
    arrive('a1')
    t = wait_rows(page, n + 1)
    n += 1
    say('1. complete row: shown after', None if t is None else f'{t:.2f} s')
    say('   its status when first shown', status_of(page, 'a1'))
    time.sleep(2.5)
    say('   its status 2.5 s later', status_of(page, 'a1'))
    say('   requests in that time', dict(requests))

    # 2. A row that waits for its partner
    arrive('b1', partner=False)
    t = wait_rows(page, n + 1, timeout=6)
    say('2. sample without transmission: shown', 'no, after 6 s' if t is None else f'{t:.2f} s')
    t_server = time.perf_counter()
    rows_server = len(s.batch.rows)
    say('   rows the server holds', rows_server, f'(the page shows {page.evaluate(JS_ROWS)})')
    s.transmission(run + 5_000_000, 'b1')
    t = wait_rows(page, n + 1)
    n += 1
    say('   after its partner arrived: shown', None if t is None else f'{t:.2f} s', status_of(page, 'b1'))
    arrive('b2', partner=False)
    time.sleep(3)
    arrive('a2')
    t = wait_rows(page, n + 2)
    n += 2
    say('   lonely b2 then complete a2: both shown', None if t is None else f'{t:.2f} s', status_of(page, 'b2'))

    # 3. A row that fails
    arrive('f1', value=-1.0)
    t = wait_rows(page, n + 1)
    n += 1
    time.sleep(2.5)
    say('3. failing row: shown', None if t is None else f'{t:.2f} s', status_of(page, 'f1'))
    say('   primary button', primary(page))
    time.sleep(3)
    say('   the loop retried it', s.batch.state(keys=[str(run)])[str(run)].record is not None, 'refused:', s.auto.refused)

    # 4. Scroll position
    page.evaluate(f"() => document.querySelector('{HOLDER}').scrollTop = 3000")
    time.sleep(0.3)
    before = (scroll_top(page), top_row(page))
    arrive('a3')
    wait_rows(page, n + 1)
    n += 1
    time.sleep(0.5)
    say('4. scrolled: before, after a new row', before, (scroll_top(page), top_row(page)))
    # Sorted by status (a click on the header): where does the new row go?

    # 5. Selection
    target = page.locator('#rows .tabulator-row', has_text='s150').first
    target.locator('.tabulator-row-header input, .tabulator-row-header').first.click()
    time.sleep(0.3)
    sel = page.inner_text('#selection')
    arrive('a4')
    wait_rows(page, n + 1)
    n += 1
    time.sleep(0.5)
    say('5. selection before, after a new row', repr(sel), repr(page.inner_text('#selection')))
    say('   scroll after the click and the row', (scroll_top(page), top_row(page)))

    # 6. Details panel with the tweak form open and a value typed
    page.click('[data-tab=row]')
    page.wait_for_selector('#tab-row .row-head')
    page.click('#tab-row details.tweak summary')
    field = page.locator('#tab-row details.tweak input[type=number]').first
    field.click()
    field.fill('7')
    arrive('a5')
    wait_rows(page, n + 1)
    n += 1
    time.sleep(1.5)
    say(
        '6. row tab, tweak form typed in: after a new row',
        'open' if page.locator('#tab-row details.tweak[open]').count() else 'closed',
        'value', field.input_value() if field.count() else '(gone)',
        'focused', page.evaluate("() => document.activeElement?.closest('details.tweak') !== null"),
    )
    page.keyboard.press('Escape')
    page.click('#title')

    # 7. A cell being edited
    cell = page.locator('#rows .tabulator-row', has_text='s20').first.locator(
        '.tabulator-cell[tabulator-field="cells.thickness.value"]'
    )
    cell.click()
    editor = page.locator('#rows .tabulator-cell.tabulator-editing input')
    editor.fill('2.5')
    arrive('a6')
    time.sleep(3.5)
    say(
        '7. cell being edited: after a new row',
        f'editor open {editor.count() == 1}',
        f'rows shown {page.evaluate(JS_ROWS)} of {n + 1}',
    )
    editor.press('Enter')
    t = wait_rows(page, n + 1)
    n += 1
    time.sleep(1.5)
    say('   after Enter: new row shown', t is not None, 'edit kept', s.batch.rows.edits.get('20'), status_of(page, 's20'))
    say('   primary button', primary(page))

    # 8. Settings form, a value typed and not sent
    page.click('[data-tab=settings]')
    bins = page.locator('#settings input[type=number]').first
    bins.click()
    bins.fill('9')
    arrive('a7')
    wait_rows(page, n + 1)
    n += 1
    time.sleep(1.5)
    say(
        '8. settings typed, not sent: after a new row',
        'value', bins.input_value(),
        'focused', page.evaluate("() => document.activeElement?.closest('#settings') !== null"),
    )
    bins.press('Enter')
    time.sleep(2.5)
    say('   after Enter: primary button', primary(page))
    arrive('a8')
    wait_rows(page, n + 1)
    n += 1
    time.sleep(2.5)
    say('   a row that arrives then', status_of(page, 'a8'), 'chips', page.inner_text('#chips').replace('\n', ' '))
    page.locator('#settings input[type=number]').first.fill('2')
    page.locator('#settings input[type=number]').first.press('Enter')
    time.sleep(2.5)

    # 9. Buttons while the loop runs
    page.click('[data-tab=result]')
    arrive('a9')
    page.wait_for_function(
        "() => document.querySelector('#reduce .label').textContent.startsWith('Reducing')",
        timeout=5000,
    ) if False else None
    labels = set()
    for _ in range(30):
        labels.add(primary(page))
        time.sleep(0.1)
    n += 1
    say('9. primary button around one arrival', sorted(labels))
    toasts = page.inner_text('#toast') if page.is_visible('#toast') else ''
    say('   toast', repr(toasts))

    # Retry the failed row from the page while the loop runs
    page.click('#chips button:has-text("failed")')
    time.sleep(0.3)
    say('   filter "failed": rows shown', page.locator('#rows .tabulator-row').count())
    arrive('a10')
    time.sleep(3)
    n += 1
    say('   filter "failed" after a new row', page.locator('#rows .tabulator-row').count(), page.inner_text('#chips').replace('\n', ' '))
    page.click('#chips button:has-text("failed")')

    # Rescan from the page
    page.click('#rescan')
    time.sleep(1)
    say('   rescan from the page: notice', repr(page.inner_text('#toast')))

    # Remove a row; does the loop bring it back?
    page.click('#title')
    page.locator('#rows .tabulator-row', has_text='a10').first.locator('.tabulator-row-header').click()
    page.click('#remove')
    time.sleep(3)
    say('   removed a10: shown after 3 s', page.locator('#rows .tabulator-row', has_text='a10').count(), 'in batch.rows', any(r.info.get('name') == 'a10' for r in s.batch.rows))

    # Load a table while the loop runs: does the next step replace it?
    csv = s.server.table_file('.csv')
    lines = csv.decode().splitlines()
    say('   table file header', lines[0][:120])
    say('   console errors', errors)

    say('steps of the loop (count, ms max, ms mean)', len(s.steps), f'{max(x[1] for x in s.steps):.0f}', f'{sum(x[1] for x in s.steps) / len(s.steps):.0f}')
    say('loop errors', s.errors[:1])
    browser.close()

s.stop()

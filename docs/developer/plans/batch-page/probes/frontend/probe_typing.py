# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
F2: while a person types 12 in the settings form, a poll draws the form again:
the half-typed 1 is sent, and the caret moves, so that the field ends as 21.
"""

from harness import arrive, browser, new_page, post, serve_demo, show

with browser() as b, serve_demo() as served:
    api = served['url'] + 'api/'
    page = new_page(b, served['url'], errors := [])
    sent: list[str] = []
    page.on(
        'request',
        lambda r: sent.append(r.post_data) if r.url.endswith('/api/settings') else None,
    )
    page.click('[data-tab=settings]')
    page.click('button[role=radio]:has-text("Use a run")')
    page.wait_for_timeout(2500)
    sent.clear()
    field = page.locator('#settings input[type=number]').first
    field.click()
    field.press('Control+a')
    page.keyboard.type('1')  # the person means 12, and is typing
    arrive(served, 70001)
    post(api + 'scan', {})
    page.wait_for_timeout(2500)  # a poll draws the form again
    page.keyboard.type('2')  # goes on typing
    page.keyboard.press('Tab')
    page.wait_for_timeout(1000)
    show('sent to /api/settings:', sent)
    show('field shows:', field.input_value())
    show('errors:', errors)

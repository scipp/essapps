# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch as a web page, without Panel: a JSON API in FastAPI (:mod:`.api`) over
the state that :mod:`.server` holds, and one page of plain JavaScript in
``static/``. It needs the extra ``web``.

This is a spike. It exists to compare a plain web application with
:mod:`ess.apps.batch.panel` as the technology of a hosted interface; nothing
depends on it. Run the demo with::

    sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh
    python -m ess.apps.batch.web.demo [DIRECTORY] --port 8000

``vendor.sh`` fetches Tabulator and Plotly with ``npm pack`` into the
git-ignored ``static/vendor/``; the page has no build step.

The server owns rows, edits, settings and corrections. The page keeps only
what is shown (selection, filter, tab, sort, axes) and polls
``GET /api/state?after=<version>``; every action answers with the new state.
Forms are :class:`ess.apps.fields.Fields` trees sent as JSON and edited with
``{path, op, value}``.

Limits:

- One server process serves one batch to one user. Two open pages edit the
  same state.
- Edits, settings and corrections live in memory and are lost when the server
  stops. Results stay, since the folder holds them.
- The browser tests in ``tests/batch/web_browser_test.py`` skip without
  Playwright, ``/usr/bin/chromium`` and the fetched libraries. They do not
  cover loading an XLSX table, an edit of several rows in the Runs view, or
  the messages for a wrong settings file.
"""

from .api import create_app
from .server import Draw, Server, Shown

__all__ = ['Draw', 'Server', 'Shown', 'create_app']

# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace as a web page: a JSON API in FastAPI (:mod:`.api`)
and one page of plain JavaScript in ``static/``. It needs the extra ``web``.
See ``README.md`` next to this file.
"""

from .api import create_app, serve
from .page import Draw, Page, Shown
from .pages import Pages

__all__ = ['Draw', 'Page', 'Pages', 'Shown', 'create_app', 'serve']

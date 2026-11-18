# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace as web pages, without Panel: a JSON API in FastAPI
(:mod:`.api`) over :class:`~.pages.Pages` and the :class:`~.page.Page` of
each batch, and one page of plain JavaScript in ``static/``. It needs the
extra ``web``. How to run it, its layers and its limits are in
``README.md`` next to this file.
"""

from .api import create_app
from .page import Draw, Page, Shown
from .pages import Pages

__all__ = ['Draw', 'Page', 'Pages', 'Shown', 'create_app']

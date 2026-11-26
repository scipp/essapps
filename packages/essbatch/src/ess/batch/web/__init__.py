# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batches of a workspace as a web page: a JSON API in FastAPI (:mod:`.api`)
and one page of plain JavaScript in ``static/``; and a launch page of the
workspaces of a root folder (:mod:`.launch`). It needs the extra ``web``.
See ``README.md`` next to this file.
"""

from .api import create_app, serve
from .launch import Launcher, OpenPages, create_launch_app
from .page import Draw, Page, Shown
from .pages import Pages

__all__ = [
    'Draw',
    'Launcher',
    'OpenPages',
    'Page',
    'Pages',
    'Shown',
    'create_app',
    'create_launch_app',
    'serve',
]

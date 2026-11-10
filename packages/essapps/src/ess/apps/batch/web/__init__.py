# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch as a web page: a JSON API in FastAPI (:mod:`.api`) over the state that
:mod:`.server` holds, and one page of plain JavaScript in ``static/``. It needs
the extra ``web``.
"""

from .api import create_app
from .server import Draw, Server

__all__ = ['Draw', 'Server', 'create_app']

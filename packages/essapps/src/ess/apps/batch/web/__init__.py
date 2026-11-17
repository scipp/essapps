# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch as a web page, without Panel: a JSON API in FastAPI (:mod:`.api`) over
:class:`~.server.Server`, and one page of plain JavaScript in ``static/``. It
needs the extra ``web``. How to run it, its layers and its limits are in
``README.md`` next to this file.
"""

from .api import create_app
from .server import Draw, Server, Shown

__all__ = ['Draw', 'Server', 'Shown', 'create_app']

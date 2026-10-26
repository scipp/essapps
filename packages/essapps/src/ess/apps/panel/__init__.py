# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Panel forms and apps generated from specs, on a client of ess.apps."""

try:
    import panel  # noqa: F401
except ImportError as error:
    raise ImportError(
        "ess.apps.panel needs Panel: pip install 'essapps[panel]'"
    ) from error

from .app import App
from .form import Form, Listing

__all__ = ['App', 'Form', 'Listing']

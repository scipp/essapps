# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Panel forms and apps generated from specs, on a client of ess.apps."""

try:
    import panel  # noqa: F401
    import plopp  # noqa: F401
except ImportError as error:
    raise ImportError(
        "ess.apps.panel needs Panel and plopp: pip install 'essapps[panel]'"
    ) from error

from .app import App, Tab
from .form import Form
from .graph import Graph
from .outputs import Outputs
from .records import Records, describe
from .table import Table

__all__ = ['App', 'Form', 'Graph', 'Outputs', 'Records', 'Tab', 'Table', 'describe']

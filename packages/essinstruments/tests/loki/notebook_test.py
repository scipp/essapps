# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The notebooks of the docs run, so that what they show is what the code does."""

import json
from pathlib import Path
from typing import Any

import matplotlib as mpl

DOCS = Path(__file__).parents[2] / 'docs'


def run_notebook(name: str) -> dict[str, Any]:
    """Run the code cells of a notebook in one namespace, without a kernel."""
    mpl.use('Agg')
    namespace: dict[str, Any] = {}
    cells = json.loads((DOCS / name).read_text())['cells']
    for number, cell in enumerate(cells):
        if cell['cell_type'] == 'code':
            code = compile(''.join(cell['source']), f'{name}, cell {number}', 'exec')
            exec(code, namespace)  # noqa: S102
    return namespace


def test_the_batch_notebook_reduces_and_exports_every_row() -> None:
    exported = run_notebook('loki-batch.ipynb')['exported']

    assert list(exported['status']) == ['completed'] * 4
    assert all(Path(path).is_file() for path in exported['iofq'])

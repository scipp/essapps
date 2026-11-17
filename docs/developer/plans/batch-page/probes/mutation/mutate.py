# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Apply each mutation of ``mutations.py`` to a copy of ``packages/essapps``, run
``tests/batch`` of that copy, and restore the file. One JSON line per mutation
goes to standard output, and is appended to ``--out`` if given.

Usage::

    mutate.py [--copy DIR] [--check] [--browser] [--out FILE] [ID ...]

``--copy DIR``
    A copy of ``packages/essapps`` (with ``src/`` and ``tests/``) to mutate. Without
    it, the script copies ``packages/essapps`` of this repository to a temporary
    folder, which it removes at the end. The tracked tree is never changed.
``--check``
    Only say which mutations no longer apply: their search string is not found
    exactly once. Reads the copy, or ``packages/essapps`` if none is given.
``--browser``
    Include ``web_browser_test.py``.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mutations import MUTATIONS

REPO = Path(__file__).resolve().parents[6]
PACKAGE = REPO / 'packages' / 'essapps'
BATCH = Path('src', 'ess', 'apps', 'batch')


def clear_pycache(copy: Path) -> None:
    for d in (copy / 'src').rglob('__pycache__'):
        shutil.rmtree(d, ignore_errors=True)


def run_tests(copy: Path, browser: bool) -> tuple[int, str, float]:
    args = [
        sys.executable,
        '-m',
        'pytest',
        'tests/batch',
        '-q',
        '-p',
        'no:cacheprovider',
        '-n',
        'auto',
        '--tb=line',
        '-rf',
    ]
    if not browser:
        args.append('--ignore=tests/batch/web_browser_test.py')
    env = {**os.environ, 'PYTHONPATH': 'src', 'PYTHONDONTWRITEBYTECODE': '1'}
    start = time.monotonic()
    try:
        done = subprocess.run(  # noqa: S603  our interpreter, fixed arguments
            args, cwd=copy, env=env, capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired as error:
        return -9, f'TIMEOUT {error.stdout!r}', time.monotonic() - start
    return done.returncode, done.stdout + done.stderr, time.monotonic() - start


def mutate(copy: Path, mutation: tuple[str, ...], browser: bool) -> dict[str, Any]:
    mid, rel, old, new, what = mutation
    path = copy / BATCH / rel
    original = path.read_text()
    if (n := original.count(old)) != 1:
        return {'id': mid, 'file': rel, 'what': what, 'result': f'NOT APPLIED ({n})'}
    path.write_text(original.replace(old, new))
    clear_pycache(copy)
    try:
        code, text, secs = run_tests(copy, browser)
    finally:
        path.write_text(original)
    summary = [
        line for line in text.splitlines() if re.search(r'\d+ (passed|failed)', line)
    ]
    return {
        'id': mid,
        'file': rel,
        'what': what,
        'result': 'survived' if code == 0 else 'killed',
        'code': code,
        'secs': round(secs, 1),
        'failed': sorted(set(re.findall(r'^FAILED (\S+)', text, re.MULTILINE))),
        'errors': sorted(set(re.findall(r'^ERROR (\S+)', text, re.MULTILINE))),
        'summary': summary[-1] if summary else text[-500:],
    }


def check(source: Path) -> None:
    """Say which mutations do not find their search string exactly once."""
    stale = 0
    for mid, rel, old, _, what in MUTATIONS:
        if (n := (source / BATCH / rel).read_text().count(old)) != 1:
            stale += 1
            sys.stdout.write(f'{mid} {rel}: {n} matches ({what})\n')
    sys.stdout.write(f'{len(MUTATIONS) - stale} of {len(MUTATIONS)} apply\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('ids', nargs='*', help='the mutations to run; all if none')
    parser.add_argument('--copy', type=Path, help='a copy of packages/essapps')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    copy = None if args.copy is None else args.copy.resolve()
    if copy is not None and copy.is_relative_to(REPO / 'packages'):
        parser.error('--copy must not be inside packages/ of the repository')
    if args.check:
        check(PACKAGE if copy is None else copy)
        return
    temporary = None
    if copy is None:
        temporary = Path(tempfile.mkdtemp(prefix='batch-mutate-'))
        copy = temporary / 'essapps'
        shutil.copytree(
            PACKAGE,
            copy,
            ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', '.*'),
        )
    try:
        for mutation in MUTATIONS:
            if args.ids and mutation[0] not in args.ids:
                continue
            line = json.dumps(mutate(copy, mutation, args.browser))
            sys.stdout.write(line + '\n')
            sys.stdout.flush()
            if args.out is not None:
                with args.out.open('a') as f:
                    f.write(line + '\n')
    finally:
        clear_pycache(copy)
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == '__main__':
    main()

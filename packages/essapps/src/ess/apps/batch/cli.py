# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch from files, as a command::

    essapps-batch files my.package:read_metadata --with runs.xlsx -o files.csv
    essapps-batch -b my.package:BINDINGS reduce iofq.yaml runs.csv -l water -o done.csv
    essapps-batch -b my.package:BINDINGS export done.csv reduced -o exported.csv

``iofq.yaml`` is a :class:`~ess.apps.Template`, and ``runs.csv`` has a row for each
sample, with a column for each blank (see :mod:`.frames`). Each command is one
function, for a notebook: :func:`.frames.reduce_table` and
:func:`.export.export_table`. The workspace is kept in a folder (see
:mod:`ess.apps.folder`), so a template may reference a record of an earlier
command, by its ID or by its label (see :func:`.text.with_latest`).

The guide is ``packages/essapps/docs/batch.md``.

Exit status: 0 if done, and every row completed; 1 for an error; 2 if the
command or a row was refused, so that nothing ran; 3 if a row did not complete.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import TypeAdapter

from ..client import Client
from ..errors import SubmitError
from ..folder import open_folder, scan_folder
from ..records import Status, Template
from . import frames
from .export import export_table
from .reduction import file_infos
from .text import with_latest


def load_template(path: Path) -> Template:
    """The template in a YAML file, which may be JSON, with files as written."""
    return TypeAdapter(Template).validate_python(yaml.safe_load(path.read_text()))


def _named(name: str) -> Any:
    """The object that ``package.module:name`` names."""
    module, _, attribute = name.partition(':')
    return getattr(import_module(module), attribute)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='essapps-batch',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '-w',
        '--workspace',
        type=Path,
        default=Path(),
        help='the folder of the workspace, with files.csv (default: here)',
    )
    parser.add_argument(
        '-b', '--bind', help='the bindings to run, a list, as package.module:NAME'
    )
    parser.add_argument('--proposal', default='local', help='default: local')
    commands = parser.add_subparsers(dest='command', required=True)
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument('-o', '--output', type=Path, help='default: CSV on stdout')

    reduce = commands.add_parser(
        'reduce', parents=[output], help='reduce the rows of a table'
    )
    reduce.add_argument('template', type=Path, help='a template, YAML')
    reduce.add_argument(
        'table',
        type=Path,
        nargs='?',
        help='the rows, .csv, .tsv, or .xlsx; without it, the template is one row',
    )
    reduce.add_argument(
        '-l', '--label', required=True, help='names the batch; each has its own'
    )
    reduce.add_argument('--staged', action='store_true', help='run through a stage')
    reduce.add_argument(
        '--remaining', action='store_true', help='leave out the rows completed'
    )

    export = commands.add_parser(
        'export', parents=[output], help='write the outputs of a reduced table'
    )
    export.add_argument('table', type=Path, help='a table that reduce wrote')
    export.add_argument('directory', type=Path, help='the folder to write to')

    files = commands.add_parser(
        'files', parents=[output], help='make files.csv from the files in the folder'
    )
    files.add_argument('read', help='a file to its metadata, as package.module:name')
    files.add_argument(
        '--with',
        dest='run_table',
        type=Path,
        metavar='TABLE',
        help='a table with a row per run, which says its role and other metadata',
    )

    rows = commands.add_parser(
        'rows', parents=[output], help='make a table from the files'
    )
    rows.add_argument('source', help='files to rows, as package.module:name')
    return parser


def _run(client: Client, args: argparse.Namespace) -> pd.DataFrame:
    if args.command == 'rows':
        return frames.table_of(_named(args.source)(file_infos(client)))
    if args.command == 'export':
        return export_table(client, frames.read_table(args.table), args.directory)
    table = (
        pd.DataFrame({'key': [args.label]})
        if args.table is None
        else frames.read_table(args.table)
    )
    template = load_template(args.template)
    return frames.reduce_table(
        client,
        replace(template, params=with_latest(client, template.params)),
        table,
        label=args.label,
        staged=args.staged,
        persist=True,
        remaining=args.remaining,
    )


def _write(table: pd.DataFrame, path: Path | None) -> None:
    if path is None:
        table.to_csv(sys.stdout, index=False)
    else:
        frames.write_table(table, path)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command, and return its exit status (see the module)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.output is not None and args.output.suffix not in frames.SUFFIXES:
        parser.error(f'{args.output}: a table is one of {", ".join(frames.SUFFIXES)}')
    if args.command == 'files':
        by_hand = args.run_table is None
        if by_hand and args.output is not None and args.output.exists():
            parser.error(f'{args.output} exists, and may hold what a person added')
        try:
            rows = scan_folder(args.workspace, _named(args.read))
            files = pd.DataFrame(rows, dtype=str).fillna('')
            if not by_hand:
                files = frames.described(files, frames.read_table(args.run_table))
        except ValueError as error:
            sys.stderr.write(f'{error}\n')
            return 2
        _write(files, args.output)
        return 0
    if args.bind is None:
        # a workspace goes on with pending records, which need their bindings
        parser.error('--bind is required: the bindings of the workspace')
    bind = _named(args.bind)
    with open_folder(args.workspace, bind, proposal=args.proposal) as client:
        try:
            result = _run(client, args)
        except (SubmitError, ValueError) as error:
            sys.stderr.write(f'{error}\n')
            return 2
    _write(result, args.output)
    done = args.command != 'reduce' or all(result['status'] == Status.COMPLETED)
    return 0 if done else 3


if __name__ == '__main__':
    sys.exit(main())

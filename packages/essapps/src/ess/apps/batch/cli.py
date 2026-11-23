# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch from files, as a command::

    essapps-batch files my.package:read_metadata --with runs.xlsx -o files.csv
    essapps-batch -b my.package:BINDINGS reduce iofq.yaml runs.csv -l water -o done.csv
    essapps-batch -b my.package:BINDINGS export done.csv reduced -o exported.csv

``iofq.yaml`` is a :class:`~ess.apps.Template`, and ``runs.csv`` is a table
file with a row for each sample: its ``key``, and a column for each parameter
that differs between rows (see :mod:`.table`). Each command is one
function, for a notebook: :func:`.frames.reduce_table`, which keeps no batch,
and :func:`.export.export_table`. The workspace is kept in a folder (see
:mod:`ess.apps.folder`), so a template may reference a record of an earlier
command, by its ID or by its label (see :func:`.templates.with_latest`).

The command makes and reads its batch with a :class:`~.manager.BatchManager`,
and an :class:`~.intake.Intake` for the row sources, as a page does, so a
label is one batch whoever works on it:

- ``reduce`` reduces the batch of its label, with the row sources of the
  application that ``-s/--sources`` names::

      essapps-batch -b my.package:BINDINGS reduce -s my.package:SOURCES -l LABEL

  For a label without a batch, it needs a template, and the manager makes
  the batch (:meth:`~.manager.BatchManager.create`) of the template's spec,
  with the label as title. Its table is the table given
  (:func:`.table.read_table`), or the rows that the source ``--source NAME``
  has filled now (:meth:`~.intake.Intake.create`), not both; without either,
  one row keyed by the label. With ``--source``, the batch keeps its
  source, so that its page offers the rows of the source that its table
  lacks; it does not follow. ``create`` refuses a label that has
  definitions or records.

  For a label with a batch, the template becomes its settings and the table
  replaces its table (:meth:`.reduction.Batch.put`); ``--source`` is refused.

  A template must be of the batch's spec and its version, as the page's
  "Load settings" checks it (:func:`.templates.checked_settings`), and is
  checked before anything is written. Its blanks are not read. A value at a
  column of the table is the setting that each row with an empty cell takes,
  but at a column that the batch's source fills, where it is left out
  (:meth:`.reduction.Batch.settable`). It then reduces every row of the
  batch. With ``--remaining`` it reduces the rows that the page's "Reduce"
  does (:func:`.state.left_to_reduce`), and leaves those reduced with earlier
  settings, which it says on stderr; ``--earlier-settings`` adds them.
- ``rows`` and ``files`` write a table from the files, as corrected in the
  definitions; ``export`` writes the outputs of a table that ``reduce`` wrote.
  None of them reads or writes a batch. ``files --with`` gives the table of
  files what a person's run table says of each file
  (:func:`.frames.described`).

The guide is ``packages/essapps/docs/batch.md``.

The table written has the columns ``status``, in the words of the page,
such as ``done``, ``earlier`` (reduced with earlier settings) or ``failed``,
``record`` and ``failure`` (:func:`.frames.with_results`).

Exit status: 0 if done, and every row is done; 1 for an error; 2 if the
command or a row was refused, so that nothing ran; 3 if a row is not done,
such as one that ``--remaining`` left as ``earlier``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

import pandas as pd

from ..client import Client
from ..errors import SubmitError
from ..folder import folder_files, open_folder, scan_folder
from . import frames
from .corrections import CorrectedFiles
from .definition import FolderDefinitions, NotKept
from .export import export_table
from .intake import Intake
from .manager import BatchManager
from .source import RowSource, rows_of
from .table import KEY, SUFFIXES, read_table, read_text, write_table
from .templates import checked_settings, load_template


def _named(name: str) -> Any:
    """The object that ``package.module:name`` names."""
    module, _, attribute = name.partition(':')
    return getattr(import_module(module), attribute)


def _source(parser: argparse.ArgumentParser, name: str) -> RowSource:
    """The :class:`~.source.RowSource` that ``name`` names; exits if it is none."""
    source = _named(name)
    if not isinstance(source, RowSource):
        parser.error(
            f'{name} is a {type(source).__name__}, not a row source '
            '(ess.apps.batch.RowSource), such as package.module:SAMPLE_RUNS'
        )
    return source


def _sources(parser: argparse.ArgumentParser, name: str) -> Mapping[str, RowSource]:
    """The sources by name that ``name`` names; exits if it names other things."""
    sources = _named(name)
    if not isinstance(sources, Mapping) or not all(
        isinstance(source, RowSource) for source in sources.values()
    ):
        parser.error(
            f'{name} is a {type(sources).__name__}, not a mapping of names to '
            'row sources (ess.apps.batch.RowSource), such as package.module:SOURCES'
        )
    return sources


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
    reduce.add_argument(
        'template',
        type=Path,
        nargs='?',
        help='a template, YAML; without it, the settings that the label keeps',
    )
    reduce.add_argument(
        'table',
        type=Path,
        nargs='?',
        help='the rows, .csv, .tsv, or .xlsx; without it, those of the batch',
    )
    reduce.add_argument(
        '-s',
        '--sources',
        help='the sources of the application, by name, as package.module:NAME',
    )
    reduce.add_argument(
        '--source',
        metavar='NAME',
        help='the name of a source in --sources; a new batch is made from its rows',
    )
    reduce.add_argument(
        '-l', '--label', required=True, help='names the batch; each has its own'
    )
    reduce.add_argument('--staged', action='store_true', help='run through a stage')
    reduce.add_argument(
        '--remaining',
        action='store_true',
        help='reduce only the rows that the page reduces with "Reduce": '
        'not those reduced with earlier settings',
    )
    reduce.add_argument(
        '--earlier-settings',
        action='store_true',
        help='with --remaining, reduce the rows reduced with earlier settings too',
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
    rows.add_argument('source', help='a RowSource, as package.module:NAME')
    return parser


def _reduce(
    client: Client, args: argparse.Namespace, definitions: FolderDefinitions
) -> pd.DataFrame:
    """
    Reduce the batch of the label, which the manager makes if the label has
    none (see the module).
    """
    label = args.label
    template = None if args.template is None else load_template(args.template)
    manager = BatchManager(
        client,
        definitions=definitions,
        staged={bound.name for bound in client.specs()} if args.staged else (),
    )
    batch = manager.batch(label) if label in manager.headers() else None
    if batch is not None and args.source is not None:
        raise ValueError(f'{label}: the batch exists; --source makes a new batch')
    if batch is not None:
        spec = batch.spec
    elif template is None:
        raise ValueError(f'{label}: no batch has the label, so give a template')
    else:
        try:
            spec = client.spec_named(template.spec.name)
        except KeyError as error:
            raise ValueError(error.args[0]) from None
    if args.table is not None:
        rows = read_table(args.table, spec.params)
    elif batch is not None:
        rows = batch.frame()
    elif args.source is None:
        rows = pd.DataFrame(index=pd.Index([label], name=KEY))
    else:
        rows = None  # Intake takes them from the source
    settings = None
    if template is not None:
        # Checked before anything is written.
        try:
            settings = checked_settings(client, spec, template)
        except ValueError as error:
            raise ValueError(f'{args.template}: {error}') from None
    if batch is not None:
        batch.put(rows)
        if settings is not None:
            batch.settings = batch.settable(settings)
    elif args.source is not None:
        intake = Intake(manager, sources=args.sources)
        batch = intake.create(
            spec.name, title=label, source=args.source, settings=settings, label=label
        )
    else:
        batch = manager.create(
            spec.name, title=label, rows=rows, settings=settings, label=label
        )
    return frames.reduce_batch(
        batch, remaining=args.remaining, earlier_settings=args.earlier_settings
    )


def _run(
    client: Client, args: argparse.Namespace, definitions: FolderDefinitions
) -> pd.DataFrame:
    if args.command == 'rows':
        return rows_of(client, args.source)
    if args.command == 'export':
        table = read_text(args.table)
        if KEY not in table.columns:
            raise ValueError(
                f'{args.table}: no column {KEY}, so reduce did not write it'
            )
        return export_table(client, table.set_index(KEY), args.directory)
    return _reduce(client, args, definitions)


def _write(table: pd.DataFrame, path: Path | None) -> None:
    write_table(table, sys.stdout if path is None else path)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command, and return its exit status (see the module)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.output is not None and args.output.suffix not in SUFFIXES:
        parser.error(f'{args.output}: a table is one of {", ".join(SUFFIXES)}')
    if args.command == 'files':
        by_hand = args.run_table is None
        if by_hand and args.output is not None and args.output.exists():
            parser.error(f'{args.output} exists, and may hold what a person added')
        try:
            rows = scan_folder(args.workspace, _named(args.read))
            files = pd.DataFrame(rows, dtype=str).fillna('')
            if not by_hand:
                files = frames.described(files, read_text(args.run_table))
        except ValueError as error:
            sys.stderr.write(f'{error}\n')
            return 2
        _write(files, args.output)
        return 0
    if args.command == 'reduce' and args.earlier_settings and not args.remaining:
        parser.error('--earlier-settings needs --remaining')
    if args.command == 'reduce' and args.table is not None and args.source:
        parser.error('give a table or --source, not both')
    if args.bind is None:
        # a workspace goes on with pending records, which need their bindings
        parser.error('--bind is required: the bindings of the workspace')
    bind = _named(args.bind)
    if args.command == 'rows':
        args.source = _source(parser, args.source)
    if args.command == 'reduce':
        args.sources = {} if args.sources is None else _sources(parser, args.sources)
        if args.source is not None and args.source not in args.sources:
            parser.error(
                f'--source {args.source} is not a name in --sources: '
                f'{", ".join(args.sources) or "none given"}'
            )
    try:
        definitions = FolderDefinitions(args.workspace)
        files = CorrectedFiles(folder_files(args.workspace), definitions=definitions)
    except ValueError as error:  # also definitions of another format
        sys.stderr.write(f'{error}\n')
        return 2
    try:
        client = open_folder(args.workspace, bind, files=files, proposal=args.proposal)
    except RuntimeError as error:  # another process holds the folder
        sys.stderr.write(f'{error}\n')
        return 1
    with client:
        try:
            result = _run(client, args, definitions)
        except (SubmitError, ValueError) as error:
            sys.stderr.write(f'{error}\n')
            return 2
        except NotKept as error:
            sys.stderr.write(f'not kept: {error}\n')
            return 1

    _write(result, args.output)
    if args.command != 'reduce':
        return 0
    # Only --remaining without --earlier-settings leaves such rows.
    if left := int((result['status'] == 'earlier').sum()):
        sys.stderr.write(
            f'left {left} row{"s" if left > 1 else ""} reduced with earlier '
            'settings: add --earlier-settings to reduce them\n'
        )
    return 0 if all(result['status'] == 'done') else 3


if __name__ == '__main__':
    sys.exit(main())

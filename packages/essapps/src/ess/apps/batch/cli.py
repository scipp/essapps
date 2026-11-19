# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch from files, as a command::

    essapps-batch files my.package:read_metadata --with runs.xlsx -o files.csv
    essapps-batch -b my.package:BINDINGS reduce iofq.yaml runs.csv -l water -o done.csv
    essapps-batch -b my.package:BINDINGS export done.csv reduced -o exported.csv

``iofq.yaml`` is a :class:`~ess.apps.Template`, and ``runs.csv`` has a row for each
sample, with a column for each blank (see :mod:`.frames`). Each command is one
function, for a notebook: :func:`.frames.reduce_table`, which keeps no batch,
and :func:`.export.export_table`. The workspace is kept in a folder (see
:mod:`ess.apps.folder`), so a template may reference a record of an earlier
command, by its ID or by its label (see :func:`.text.with_latest`).

The command makes and reads its batch with a :class:`~.manager.BatchManager`,
as a page does, so a label is one batch whoever works on it:

- ``reduce`` reduces the batch of its label, with the sources of the
  application that ``-s/--sources`` names::

      essapps-batch -b my.package:BINDINGS reduce -s my.package:SOURCES -l LABEL

  A template given must be of the batch's spec and its version, with its
  columns as the blanks, as the page's "Load settings" checks it
  (:func:`.text.checked_settings`), and ``--source``, if given, must name the
  batch's source. For a label without a batch, the manager makes one
  (:meth:`~.manager.BatchManager.create`) of the template's spec, with its
  blanks as the columns, the source that ``--source NAME`` names, and the
  label as title; it refuses a label that has definitions or records. A batch
  made so is removed again if the run raises before a row of it was
  submitted.
 The template becomes the batch's settings, and the table is
  imported as an application imports one (:meth:`.reduction.Batch.put`): set
  by hand on the rows that the source makes from the files, and on those that
  the definitions hold. It then reduces the rows of the table; without a
  table, those of the batch, or, given a template, one row keyed by the label
  if the batch has no source.
  With ``--remaining`` it reduces the rows that the page's "Reduce" does
  (:func:`.state.left_to_reduce`), and leaves those reduced with earlier
  settings, which it says on stderr; ``--earlier-settings`` adds them.
- ``rows`` and ``files`` write a table from the files, as corrected in the
  definitions; ``export`` writes the outputs of a table that ``reduce`` wrote.
  None of them reads or writes a batch.

The guide is ``packages/essapps/docs/batch.md``.

Exit status: 0 if done, and every row completed; 1 for an error; 2 if the
command or a row was refused, so that nothing ran; 3 if a row did not complete,
or ``--remaining`` left a row reduced with earlier settings.
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
from ..records import Status
from . import frames
from .corrections import CorrectedFiles
from .definition import FolderDefinitions, NotKept
from .export import export_table
from .manager import BatchManager, Defaults
from .reduction import Source, file_infos
from .state import EARLIER
from .text import checked_settings, load_template


def _named(name: str) -> Any:
    """The object that ``package.module:name`` names."""
    module, _, attribute = name.partition(':')
    return getattr(import_module(module), attribute)


def _source(parser: argparse.ArgumentParser, name: str) -> Source:
    """The :class:`~.reduction.Source` that ``name`` names; exits if it is none."""
    source = _named(name)
    if not isinstance(source, Source):
        parser.error(
            f'{name} is a {type(source).__name__}, not a Source '
            '(ess.apps.batch.Source), such as package.module:SAMPLE_RUNS'
        )
    return source


def _sources(parser: argparse.ArgumentParser, name: str) -> Mapping[str, Source]:
    """The sources by name that ``name`` names; exits if it names other things."""
    sources = _named(name)
    if not isinstance(sources, Mapping) or not all(
        isinstance(source, Source) for source in sources.values()
    ):
        parser.error(
            f'{name} is a {type(sources).__name__}, not a mapping of names to '
            'Sources (ess.apps.batch.Source), such as package.module:SOURCES'
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
        help='the name of a source in --sources; the table is set on its rows',
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
    rows.add_argument('source', help='a Source, as package.module:NAME')
    return parser


def _reduce(
    client: Client, args: argparse.Namespace, definitions: FolderDefinitions
) -> pd.DataFrame:
    """
    Reduce the batch of the label, which the manager makes from the template
    if the label has none (see the module).
    """
    label = args.label
    template = None if args.template is None else load_template(args.template)
    staged = Defaults(staged=args.staged)
    manager = BatchManager(
        client,
        sources=args.sources,
        definitions=definitions,
        defaults={bound.name: staged for bound in client.specs()},
    )
    if (header := manager.headers().get(label)) is None:
        if template is None:
            raise ValueError(f'{label}: no batch has the label, so give a template')
        manager.create(
            template.spec.name,
            title=label,
            source=args.source,
            columns=template.blanks,
            label=label,
        )
    elif args.source not in (None, header.source):
        has = 'no source' if header.source is None else f'the source {header.source}'
        raise ValueError(f'{label}: the batch has {has}, not {args.source}')
    try:
        try:
            batch = manager.batch(label)
        except ValueError as error:
            if header is None or header.source in (None, *args.sources):
                raise
            raise ValueError(
                f'{label}: {error}; give the sources of the application with '
                '-s/--sources package.module:NAME'
            ) from None
        settings = None
        if template is not None:
            try:  # as the page's "Load settings" checks a template
                settings = checked_settings(batch, template)
            except ValueError as error:
                raise ValueError(f'{args.template}: {error}') from None
        table = None
        if args.table is not None:
            table = frames.read_table(args.table)
        elif template is not None and batch.source is None:
            table = pd.DataFrame({'key': [label]})
        return frames.reduce_batch(
            batch,
            table,
            settings=settings,
            remaining=args.remaining,
            earlier_settings=args.earlier_settings,
        )
    except BaseException:
        # A batch made here that submitted nothing would fix the spec, the
        # columns and the source of the label for the next run.
        if header is None and not client.records(label=label):
            manager.remove(label)
        raise


def _run(
    client: Client, args: argparse.Namespace, definitions: FolderDefinitions
) -> pd.DataFrame:
    if args.command == 'rows':
        source = args.source
        return frames.table_of(source.rows(file_infos(client, source.selector)))
    if args.command == 'export':
        return export_table(client, frames.read_table(args.table), args.directory)
    return _reduce(client, args, definitions)


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
    if args.command == 'reduce' and args.earlier_settings and not args.remaining:
        parser.error('--earlier-settings needs --remaining')
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
    definitions = FolderDefinitions(args.workspace)
    try:
        files = CorrectedFiles(folder_files(args.workspace), definitions=definitions)
    except ValueError as error:
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
    if args.command == 'reduce' and args.remaining and not args.earlier_settings:
        left = int(result['status'].str.endswith(EARLIER).sum())
        if left:
            sys.stderr.write(
                f'left {left} row{"s" if left > 1 else ""} reduced with earlier '
                'settings: add --earlier-settings to reduce them\n'
            )
    done = args.command != 'reduce' or all(result['status'] == Status.COMPLETED)
    return 0 if done else 3


if __name__ == '__main__':
    sys.exit(main())

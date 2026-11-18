# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch from files, as a command::

    essapps-batch files my.package:read_metadata --with runs.xlsx -o files.csv
    essapps-batch -b my.package:BINDINGS reduce iofq.yaml runs.csv -l water -o done.csv
    essapps-batch -b my.package:BINDINGS export done.csv reduced -o exported.csv

``iofq.yaml`` is a :class:`~ess.apps.Template`, and ``runs.csv`` has a row for each
sample, with a column for each blank (see :mod:`.frames`). Each command is one
function, for a notebook: :func:`.frames.reduce_table`, which keeps nothing in
the definitions, and :func:`.export.export_table`. The workspace is kept in a
folder (see :mod:`ess.apps.folder`), so a template may reference a record of an
earlier command, by its ID or by its label (see :func:`.text.with_latest`).

The command keeps a batch where an application does, in the folder's
definitions (:mod:`.definition`), so a label is one batch whoever works on it:

- ``reduce`` reduces the batch of its label. A label with a header, such as
  that of a batch made on a page, is the batch that the header says
  (:func:`.manager.header_batch`): its spec, of the version that the
  workspace binds, its columns, its source, which ``-s/--sources`` registers
  by name as an application does, and its scope::

      essapps-batch -b my.package:BINDINGS reduce -s my.package:SOURCES -l LABEL

  A template given there must be of the header's spec, with its columns as
  the blanks, and ``--source``, if given, must name the header's source. A
  label without a header takes the spec and the columns from the template
  given, else from the settings that the label keeps, and the source from
  ``--source NAME``; the command then writes the header, with the label as
  title, so that the batch shows in the list of batches of a page. The
  template given becomes the batch's settings, and the table is imported as
  an application imports one (:meth:`.reduction.Batch.put`): set by hand on
  the rows that the source makes from the files, and on those that the
  definitions hold. It then reduces the rows of the table; without a
  template and a table, those of the batch.
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
from dataclasses import replace
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
from .definition import (
    FolderDefinitions,
    Header,
    header_data,
    header_key,
    header_of,
    settings_key,
    template_of,
)
from .export import export_table
from .manager import Defaults, check_label, header_batch
from .reduction import Source, file_infos
from .state import EARLIER
from .text import load_template, with_latest


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
        help='the rows, .csv, .tsv, or .xlsx; without it, the template is one row',
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
    Reduce the batch of the label: that of its header, else that of the
    template given or kept, whose header it writes (see the module).
    """
    label = args.label
    template, table = None, None
    if args.template is not None:
        template = load_template(args.template)
        template = replace(template, params=with_latest(client, template.params))
        table = (
            pd.DataFrame({'key': [label]})
            if args.table is None
            else frames.read_table(args.table)
        )
    header = definitions.load(header_key(label), header_of)
    if header is not None:
        if args.source not in (None, header.source):
            has = (
                'no source' if header.source is None else f'the source {header.source}'
            )
            raise ValueError(f'{label}: the batch has {has}, not {args.source}')
        if template is not None and (
            template.spec.name != header.spec
            or set(template.blanks) != set(header.columns)
        ):
            raise ValueError(
                f'{label}: the batch is of {header.spec} with the columns '
                f'{list(header.columns)}, the template of {template.spec.name} '
                f'with the blanks {list(template.blanks)}'
            )
        made = header
    else:
        check_label(label)
        kept = template or definitions.load(settings_key(label), template_of)
        if kept is None:
            raise ValueError(f'{label}: no settings are kept, so give a template')
        made = Header(
            spec=kept.spec.name,
            columns=tuple(kept.blanks),
            source=args.source,
            title=label,
        )
    batch = header_batch(
        client,
        label,
        made,
        sources=args.sources,
        definitions=definitions,
        defaults=Defaults(staged=args.staged),
    )
    if header is None:
        definitions.save(header_key(label), header_data(made))
    return frames.reduce_batch(
        batch,
        table,
        settings=None if template is None else template.params,
        remaining=args.remaining,
        earlier_settings=args.earlier_settings,
    )


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
    with open_folder(
        args.workspace, bind, files=files, proposal=args.proposal
    ) as client:
        try:
            result = _run(client, args, definitions)
        except (SubmitError, ValueError, KeyError) as error:
            # KeyError: no spec bound, or no source registered, of a name
            said = error.args[0] if isinstance(error, KeyError) else error
            sys.stderr.write(f'{said}\n')
            return 2
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

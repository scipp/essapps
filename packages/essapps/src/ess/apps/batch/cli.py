# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Reduce many runs as batches, on a workspace folder.

A batch is a table and a template, kept in the folder under a label. Each row of
the table is one reduction, such as of one sample run; the template holds the
settings that the rows share.

The options before the command name the folder, the bindings (the reductions
that the folder runs) and the row sources (which make rows from the files):

    B="essapps-batch -w FOLDER -b package.module:BINDINGS -s package.module:SOURCES"

A first session, in this order; each command has its own --help:

    essapps-batch -w FOLDER files package.module:read -o FOLDER/files.csv
                                  # once: the table of the files of the folder
    $B specs                      # the reductions: name, version, title, outputs
    $B template SPEC -o t.yaml    # a template of SPEC: what each parameter takes
    $B sources                    # the row sources, and the parameters each fills
    $B rows SOURCE                # the rows that the row source SOURCE makes now
    $B reduce t.yaml --source SOURCE -l water
                                  # make the batch water of these rows; reduce it
    $B reduce t.yaml runs.csv -l water
                                  # or make it of a table file of your own
    $B batches                    # the batches, with how many rows are done
    $B show -l water -o done.csv  # the table of water, with the status of each row
    $B reduce -l water --remaining
                                  # reduce the rows that are not done
    $B template -l water -o now.yaml
                                  # the settings of water, as a template
    $B export done.csv results    # write the outputs of the rows of done.csv

A template is YAML: the spec, and a value for each parameter that the rows
share. A parameter that the table gives for each row is left out:

    spec: {name: loki-masks, version: 1}
    params:
      detector: {'@latest': detector, output: detector}
      mask_files: [path:calibration/masks.xml]

Here detector is the output detector of the newest record under the label
detector, and mask_files a list of one file, by its path in files.csv. The
template that "template SPEC" writes says how to write each kind of value.

reduce on a label without a batch makes the batch: of the rows of --source, of
a table file, or else of one row keyed by the label. On a label with a batch, a
template replaces its settings and a table its table. Then it reduces every
row, or with --remaining those not done, and writes the table with a status, a
record and a failure per row. A refused reduce leaves no new batch behind.

The status of a row:
    ready       no result yet, and it can be reduced
    incomplete  it cannot be reduced: a value is missing, such as an empty cell,
                or refused
    running     submitted, not finished
    done        reduced with the cells and settings it has now
    earlier     reduced, but a setting it takes changed since; --remaining
                leaves it, --earlier-settings adds it
    outdated    reduced, but a cell of the row changed since
    failed      the reduction failed; the column failure says why
    cancelled   the reduction was cancelled

Exit status:
    0  done, and every row is done
    1  an error, such as a folder that another process holds
    2  refused, so that nothing ran: a wrong command line, template or table,
       or a row that cannot be reduced
    3  a row is not done, such as one that failed, or one that --remaining left

One process works on a folder at a time: the command refuses a folder that
another process holds, such as a batch page that serves it. --json writes a
table as JSON: a list of objects, one per row. Messages go to stderr.
"""

# The commands are thin: the rules are those of BatchManager, Intake and Batch,
# as the page applies them, so a label is one batch whoever works on it. Only
# reduce changes a batch. The guide is packages/essapps/docs/batch.md, section
# "Table files and the command".

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ess.spec import Binding

from ..client import Client
from ..errors import SubmitError
from ..folder import folder_files, open_folder, scan_folder
from ..records import Template
from . import frames
from .corrections import CorrectedFiles
from .definition import FolderDefinitions, NotKept
from .export import export_table
from .intake import Intake
from .manager import BatchManager
from .reduction import Batch
from .source import RowSource, rows_of
from .table import KEY, SUFFIXES, read_table, read_text, write_table
from .templates import checked_settings, load_template, template_text, template_to_fill


def _named(parser: argparse.ArgumentParser, name: str) -> Any:
    """The object that ``package.module:NAME`` names; exits if there is none."""
    module, colon, attribute = name.partition(':')
    if not (module and colon and attribute):
        parser.error(f'{name}: name it as package.module:NAME')
    try:
        found = import_module(module)
    except ImportError as error:
        parser.error(f'{name}: cannot import {module}: {error}')
    if not hasattr(found, attribute):
        parser.error(f'{name}: the module {module} has no {attribute}')
    return getattr(found, attribute)


def _bindings(parser: argparse.ArgumentParser, name: str) -> list[Binding[Any, Any]]:
    """The bindings that ``name`` names; exits if it names something else."""
    bind = _named(parser, name)
    if isinstance(bind, str | bytes) or not (
        isinstance(bind, Sequence) and all(isinstance(b, Binding) for b in bind)
    ):
        parser.error(
            f'{name} is a {type(bind).__name__}, not a list of bindings '
            '(ess.spec.Binding), such as package.module:BINDINGS'
        )
    return list(bind)


def _sources(parser: argparse.ArgumentParser, name: str) -> Mapping[str, RowSource]:
    """The sources by name that ``name`` names; exits if it names other things."""
    sources = _named(parser, name)
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
        help='the workspace folder, with its files.csv (default: here)',
    )
    parser.add_argument(
        '-b',
        '--bind',
        metavar='MODULE:NAME',
        help='the bindings, the reductions that the folder runs; '
        'every command but files and sources needs them',
    )
    parser.add_argument(
        '-s',
        '--sources',
        metavar='MODULE:NAME',
        help='the row sources, by name; sources, rows and reduce --source need them',
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help='write a table as JSON, a list of objects, one per row',
    )
    parser.add_argument(
        '--proposal', default='local', help='the proposal of the files (default: local)'
    )
    commands = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument(
        '-o',
        '--output',
        type=Path,
        help=f'the file to write, of {", ".join(SUFFIXES)}, or .json with --json '
        '(default: stdout)',
    )
    label = argparse.ArgumentParser(add_help=False)
    label.add_argument('-l', '--label', required=True, help='the label of the batch')

    files = commands.add_parser(
        'files',
        parents=[output],
        help='write the table of the files of the folder, files.csv',
        description='Write the table of the files of the folder (files.csv), '
        'a row per file that READ knows. It refuses to overwrite an -o that '
        'exists, as a person may have added to it, unless --with is given.',
    )
    files.add_argument(
        'read',
        metavar='READ',
        help='package.module:NAME of a function from the path of a file to its '
        'metadata',
    )
    files.add_argument(
        '--with',
        dest='run_table',
        type=Path,
        metavar='TABLE',
        help='a table with a row per run, by its column run or file, which says '
        'its role, its sample, or any other column',
    )

    commands.add_parser(
        'specs',
        parents=[output],
        help='list the specs that the bindings bind',
        description='List the specs that the bindings bind, one per row: name, '
        'version, title and the names of its outputs.',
    )

    template = commands.add_parser(
        'template',
        help='write a template of a spec to fill in, or the settings of a batch',
        description='Write a template of SPEC to fill in, with a comment on each '
        'parameter: whether it is required or its default, what it takes, and '
        'what it is. Or, with -l, the settings of the batch of LABEL, as a '
        'template for reduce.',
    )
    template.add_argument('spec', nargs='?', metavar='SPEC', help='the name of a spec')
    template.add_argument(
        '-l', '--label', help='the label of a batch, in place of SPEC'
    )
    template.add_argument(
        '-o', '--output', type=Path, help='the YAML file to write (default: stdout)'
    )

    commands.add_parser(
        'sources',
        parents=[output],
        help='list the row sources, with the parameters each fills',
        description='List the row sources of -s/--sources by name, with the '
        'parameters that each fills for each row.',
    )

    rows = commands.add_parser(
        'rows',
        parents=[output],
        help='write the rows that a row source makes from the files now',
        description='Write the table that the row source NAME makes from the '
        'files now. It reads and writes no batch.',
    )
    rows.add_argument(
        'source', metavar='NAME', help='the name of a row source in -s/--sources'
    )

    commands.add_parser(
        'batches',
        parents=[output],
        help='list the batches of the folder',
        description='List the batches of the folder: label, title, spec, number '
        'of rows, how many are done, the row source it was made from, and whether '
        'it follows that source.',
    )

    commands.add_parser(
        'show',
        parents=[output, label],
        help='write the table of a batch, with what came of each row',
        description='Write the table of the batch of LABEL, with the columns '
        'status, record and failure, as reduce writes it. It reduces nothing.',
    )

    reduce = commands.add_parser(
        'reduce',
        parents=[output, label],
        help='reduce the rows of a batch; make the batch if the label has none',
        description='Make the batch of LABEL if it has none, or give it new '
        'settings and a new table; then reduce its rows, and write its table '
        'with the columns status, record and failure.',
    )
    reduce.add_argument(
        'template',
        metavar='TEMPLATE',
        type=Path,
        nargs='?',
        help='a template, YAML (see the command template); without it, the '
        'settings of the batch',
    )
    reduce.add_argument(
        'table',
        metavar='TABLE',
        type=Path,
        nargs='?',
        help=f'the rows: a file of {", ".join(SUFFIXES)} with a column key and a '
        'column per parameter that differs between rows; without it, those of '
        'the batch',
    )
    reduce.add_argument(
        '--source',
        metavar='NAME',
        help='make a new batch of the rows that the row source NAME in '
        '-s/--sources has filled now; not with a table',
    )
    reduce.add_argument(
        '--staged',
        action='store_true',
        help='run the rows through a stage, which reduces once what they share',
    )
    reduce.add_argument(
        '--remaining',
        action='store_true',
        help='reduce only the rows that are not done: not those that read earlier',
    )
    reduce.add_argument(
        '--earlier-settings',
        action='store_true',
        help='with --remaining, reduce the rows that read earlier too',
    )

    export = commands.add_parser(
        'export',
        parents=[output],
        help='write the outputs of the rows of a table to files',
        description='Write the outputs of each done row of TABLE to a folder of '
        'its key in DIRECTORY, and the table with the path of each output.',
    )
    export.add_argument(
        'table', metavar='TABLE', type=Path, help='a table that reduce or show wrote'
    )
    export.add_argument(
        'directory', metavar='DIRECTORY', type=Path, help='the folder to write to'
    )
    return parser


def _manager(
    client: Client, definitions: FolderDefinitions, *, staged: bool = False
) -> BatchManager:
    """The batches of the folder, of specs that run through a stage if ``staged``."""
    return BatchManager(
        client,
        definitions=definitions,
        staged={bound.name for bound in client.specs()} if staged else (),
    )


def _batch(manager: BatchManager, label: str) -> Batch:
    """The batch of ``label``; ``ValueError`` if the label has none."""
    if label not in manager.headers():
        raise ValueError(f'{label}: no batch has the label; batches lists them')
    return manager.batch(label)


def _reduce(
    client: Client,
    args: argparse.Namespace,
    definitions: FolderDefinitions,
    sources: Mapping[str, RowSource],
) -> pd.DataFrame:
    """
    Reduce the batch of the label, and return its table with what came of
    each row (:func:`.frames.reduce_batch`).

    For a label without a batch, the manager makes the batch
    (:meth:`~.manager.BatchManager.create`) of the template's spec, with the
    label as title. Its table is the table given (:func:`.table.read_table`),
    or the rows that the source ``--source`` has filled now
    (:meth:`~.intake.Intake.create`), not both; without either, one row keyed
    by the label. With ``--source``, the batch keeps its source, so that its
    page offers the rows of the source that its table lacks; it does not
    follow. ``create`` refuses a label that has definitions or records. If the
    reduction is then refused, the batch is removed again: nothing was
    submitted, and the same command line works once the template is right.

    For a label with a batch, the template becomes its settings and the table
    replaces its table (:meth:`.reduction.Batch.put`); ``--source`` is refused.

    A template must be of the batch's spec and its version, as the page's
    "Load settings" checks it (:func:`.templates.checked_settings`), and is
    checked before anything is written. Its blanks are not read. A value at a
    column of the table is the setting that each row with an empty cell takes,
    but at a column that the batch's source fills, where it is left out
    (:meth:`.reduction.Batch.settable`). With ``--remaining`` it reduces the
    rows that the page's "Reduce" does (:func:`.state.left_to_reduce`);
    ``--earlier-settings`` adds those reduced with earlier settings.
    """
    label = args.label
    template = None if args.template is None else load_template(args.template)
    manager = _manager(client, definitions, staged=args.staged)
    batch = manager.batch(label) if label in manager.headers() else None
    if batch is not None and args.source is not None:
        raise ValueError(f'{label}: the batch exists; --source makes a new batch')
    if batch is not None:
        spec = batch.spec
    elif template is None:
        raise ValueError(f'{label}: no batch has the label, so give a template')
    else:
        spec = manager.spec_named(template.spec.name)
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
    made = batch is None
    if batch is not None:
        batch.put(rows)
        if settings is not None:
            batch.settings = batch.settable(settings)
    elif args.source is not None:
        intake = Intake(manager, sources=sources)
        batch = intake.create(
            spec.name, title=label, source=args.source, settings=settings, label=label
        )
    else:
        batch = manager.create(
            spec.name, title=label, rows=rows, settings=settings, label=label
        )
    try:
        return frames.reduce_batch(
            batch, remaining=args.remaining, earlier_settings=args.earlier_settings
        )
    except (SubmitError, ValueError):
        if made:  # a refusal submits nothing, so the label is free again
            manager.remove(label)
        raise


def _batches(intake: Intake) -> pd.DataFrame:
    """
    A row per batch: its label, title, spec, number of rows, how many are
    done, the name of its row source, and whether it follows. A batch that
    cannot be read is said on stderr, and its row has no spec and no counts.
    """
    manager = intake.manager
    following = intake.following
    listed = []
    for label, header in manager.headers().items():
        row: dict[str, Any] = {'label': label, 'title': header.title}
        try:
            batch = manager.batch(label)
            states, _ = batch.shown()
            row |= {
                'spec': batch.spec.name,
                'rows': len(states),
                'done': sum(state.text == 'done' for state in states.values()),
                'source': intake.source(label),
            }
        except ValueError as error:
            sys.stderr.write(f'{label}: {error}\n')
        listed.append(row | {'follows': label == following})
    columns = ['label', 'title', 'spec', 'rows', 'done', 'source', 'follows']
    return pd.DataFrame(listed, columns=columns).astype(
        {'rows': 'Int64', 'done': 'Int64'}
    )


def _run(
    client: Client,
    args: argparse.Namespace,
    definitions: FolderDefinitions,
    sources: Mapping[str, RowSource],
) -> pd.DataFrame | str:
    """What the command gives: a table, or the text of a template."""
    if args.command == 'specs':
        specs = [
            [spec.name, spec.version, spec.title, list(spec.outputs.model_fields)]
            for spec in client.specs()
        ]
        return pd.DataFrame(specs, columns=['name', 'version', 'title', 'outputs'])
    if args.command == 'template' and args.label is None:
        try:
            spec = client.spec_named(args.spec)
        except KeyError as error:
            raise ValueError(f'{error.args[0]}; specs lists them') from None
        return template_to_fill(spec, client.specs(), sources)
    if args.command == 'rows':
        return rows_of(client, sources[args.source])
    if args.command == 'export':
        table = read_text(args.table)
        if KEY not in table.columns:
            raise ValueError(
                f'{args.table}: no column {KEY}, so reduce did not write it'
            )
        return export_table(client, table.set_index(KEY), args.directory)
    if args.command == 'reduce':
        return _reduce(client, args, definitions, sources)
    manager = _manager(client, definitions)
    if args.command == 'batches':
        return _batches(Intake(manager, sources=sources))
    batch = _batch(manager, args.label)
    if args.command == 'template':
        return template_text(Template(batch.spec, params=batch.settings))
    states, refused = batch.shown()
    if refused:
        sys.stderr.write(f'the settings are refused: {SubmitError(refused)}\n')
    return frames.with_results(client, states, batch.frame())


def _cell(value: Any) -> Any:
    """A cell as JSON takes it: ``None`` if empty, a Python value."""
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or value is pd.NA or (isinstance(value, float) and value != value):
        return None
    return value


def _write(result: pd.DataFrame | str, args: argparse.Namespace) -> None:
    """Write a template's text, or a table as a table file or as JSON."""
    if isinstance(result, pd.DataFrame) and not args.json:
        write_table(result, sys.stdout if args.output is None else args.output)
        return
    if isinstance(result, pd.DataFrame):
        if result.index.name is not None:
            result = result.reset_index()
        rows = [
            {name: _cell(value) for name, value in row.items()}
            for row in result.to_dict('records')
        ]
        result = json.dumps(rows, indent=2, ensure_ascii=False, default=str) + '\n'
    if args.output is None:
        sys.stdout.write(result)
    else:
        args.output.write_text(result)


def _files(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """The command ``files``; its exit status."""
    read = _named(parser, args.read)
    if not callable(read):
        parser.error(
            f'{args.read} is a {type(read).__name__}, not a function from the '
            'path of a file to its metadata'
        )
    by_hand = args.run_table is None
    if by_hand and args.output is not None and args.output.exists():
        parser.error(f'{args.output} exists, and may hold what a person added')
    try:
        rows = scan_folder(args.workspace, read)
        files = pd.DataFrame(rows, dtype=str).fillna('')
        if not by_hand:
            files = frames.described(files, read_text(args.run_table))
    except ValueError as error:
        sys.stderr.write(f'{error}\n')
        return 2
    _write(files, args)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command, and return its exit status (see the module)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command != 'template' and args.output is not None:
        suffixes = ('.json',) if args.json else SUFFIXES
        if args.output.suffix not in suffixes:
            parser.error(f'{args.output}: write a file of {", ".join(suffixes)}')
    if args.command == 'files':
        return _files(parser, args)
    sources = {} if args.sources is None else _sources(parser, args.sources)
    if args.command == 'sources':
        if args.sources is None:
            parser.error('sources needs -s/--sources, as package.module:SOURCES')
        fills = [[name, list(s.fills)] for name, s in sources.items()]
        _write(pd.DataFrame(fills, columns=['name', 'fills']), args)
        return 0
    if (source := getattr(args, 'source', None)) is not None and source not in sources:
        flag = '--source ' if args.command == 'reduce' else ''
        parser.error(
            f'{flag}{source} is not a name in --sources: '
            f'{", ".join(sources) or "none given"}'
        )
    if args.command == 'template' and (args.spec is None) == (args.label is None):
        parser.error('template takes a SPEC or -l LABEL')
    if args.command == 'reduce' and args.earlier_settings and not args.remaining:
        parser.error('--earlier-settings needs --remaining')
    if args.command == 'reduce' and args.table is not None and args.source:
        parser.error('give a table or --source, not both')
    if args.bind is None:
        # a workspace goes on with pending records, which need their bindings
        parser.error('--bind is required: the bindings of the workspace')
    bind = _bindings(parser, args.bind)
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
            result = _run(client, args, definitions, sources)
        except (SubmitError, ValueError) as error:
            sys.stderr.write(f'{error}\n')
            return 2
        except NotKept as error:
            sys.stderr.write(f'not kept: {error}\n')
            return 1

    _write(result, args)
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

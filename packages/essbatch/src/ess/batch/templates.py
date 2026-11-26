# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates as YAML files that a person writes and reads.

How a person writes each kind of value is in :data:`_HOW`, which every
template to fill in carries.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python

from ess.apps.client import Client
from ess.apps.records import SpecId, Template
from ess.spec import File, WorkflowSpec, field_types
from ess.spec.field_types import Choice, FileKind, Group, List, Scalar

from .definition import template_data, template_of
from .reduction import Batch
from .source import RowSource
from .table import as_request, not_outputs


def with_latest(client: Client, value: Any) -> Any:
    """
    ``value`` with each object of the key ``@latest`` replaced by a reference
    to an output of the newest record under that label::

        with_latest(client, {'masks': {'@latest': 'masks', 'output': 'masks'}})
        # {'masks': OutputRef(record='r17', output='masks')}

    An optional ``member`` selects among the members of the label. The newest
    record is the one now, so a request made of the result holds that record's
    ID.

    Raises ``ValueError`` if no record has the label, or the record lacks the
    output.
    """
    if isinstance(value, list):
        return [with_latest(client, v) for v in value]
    if not isinstance(value, Mapping):
        return value
    if '@latest' not in value:
        return {k: with_latest(client, v) for k, v in value.items()}
    if value.keys() - {'@latest', 'member', 'output'}:
        raise ValueError(f'{dict(value)}: @latest goes with member and output only')
    if not all(isinstance(v, str) for v in value.values()):
        raise ValueError(f'{dict(value)}: write each of these as text, in quotes')
    try:
        return client.latest(value['@latest'], value.get('member')).ref(value['output'])
    except KeyError as error:
        raise ValueError(f'{dict(value)}: {error.args[0]}') from None


def load_template(path: Path) -> Template:
    """
    The template in a YAML or JSON file, with files as written.

    Raises ``ValueError`` if the file holds no template.
    """
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError(f'{path}: {error}') from None
    if isinstance(data, dict) and 'params' in data and data['params'] is None:
        # ``params:`` with nothing under it, as in a template to fill in whose
        # every parameter is still commented out.
        data = {k: v for k, v in data.items() if k != 'params'}
    return template_of(data)


def checked_settings(
    client: Client, spec: WorkflowSpec[Any, Any], template: Template
) -> dict[str, Any]:
    """
    The settings of ``spec`` in ``template``, as a request reads them.

    Files are converted as in a cell (:func:`~.table.as_request`), and
    ``@latest`` is resolved on ``client`` (:func:`with_latest`). The
    ``blanks`` of ``template`` are ignored: a batch takes its parameter
    columns from its batch table.

    Raises ``ValueError`` if the template is of another spec or version, or a
    value cannot be read.
    """
    theirs, ours = template.spec, SpecId.of(spec)
    if theirs.name != ours.name:
        raise ValueError(f'it holds the settings of {theirs.name}, not {ours.name}')
    if theirs.version != ours.version:
        raise ValueError(
            f'it holds the settings of version {theirs.version} of {ours.name}, '
            f'not of version {ours.version}; the settings of one version may '
            'mean something else in another, so check them, and set the '
            f'version in the template to {ours.version}'
        )
    values = as_request(spec.params, template.params)
    if names := not_outputs(spec.params, values):
        raise ValueError(
            '; '.join(f'{n}: {values[n]!r} {_NOT_AN_OUTPUT}' for n in names)
        )
    return with_latest(client, values)


_NOT_AN_OUTPUT = (
    "names no output: a template names one as {'@latest': LABEL, output: NAME} "
    'or RECORD.OUTPUT'
)
"""Error text for a value that refers to no output where one is expected."""


def settings_text(batch: Batch) -> str:
    """
    The settings of ``batch`` as the YAML of a template, those at its parameter
    columns included.

    Each file and reference is in its JSON form, so :func:`load_template`
    reads it back unchanged.
    """
    template = Template(batch.spec, params=batch.settings)
    return yaml.safe_dump(template_data(template), sort_keys=False)


_WIDTH = 88
"""The width of the lines of :func:`template_to_fill`."""

_HOW = """\
To set a parameter, remove the "#" before its name and write the value after
the colon. A parameter left commented takes its default, or, if it is marked
"required", is missing: give it a value, unless the batch table gives it for
each row, as a parameter column or a parameter that the row source fills.

How to write a value:
  a run         70003, or run:70003
  a file        path:raw/70003.nxs, its path in files.csv
  a file in a group, or in a list of groups:
                {'@ref': file, file: run:70003}, {'@ref': file, file: path:...}
  an output     {'@latest': LABEL, output: NAME}: the output NAME of the newest
                record under the label LABEL; add member: KEY for the row KEY
                of a batch
                RECORD.OUTPUT: the output OUTPUT of the record RECORD, as the
                column record of "show" gives it; in a group, write
                {'@ref': output, record: RECORD, output: OUTPUT}
  a group       {start: 0.01, stop: 0.3, num_bins: 100}, or a field per line
  a list        [a, b]
  none          null"""


def template_to_fill(
    spec: WorkflowSpec[Any, Any],
    bound: Sequence[WorkflowSpec[Any, Any]],
    sources: Mapping[str, RowSource] | None = None,
) -> str:
    """
    A template of ``spec`` for a person to fill in, as YAML.

    Every parameter is commented out, with a comment on whether it is
    required, what it accepts, and its description::

        # q (required): a group of the fields below. Bin edges in Q.
        #   start (required): a number. First bin edge.
        #   scale (default linear): one of linear, log. Spacing of the edges.
        #q:
        # corrections (default {gravity: true}): a group of the fields below.
        #corrections: {gravity: true}

    A template loaded unchanged is rejected, naming each required parameter.
    A parameter left commented takes the spec's default: the template holds no
    copy of it. A parameter that accepts an output lists the outputs of
    ``bound`` that fit. A parameter that a row source of ``sources`` fills
    states so.
    """
    filled: dict[str, list[str]] = {}
    for name, source in (sources or {}).items():
        for parameter in source.source_columns:
            filled.setdefault(parameter, []).append(name)
    title = f'A template of {spec.name}, version {spec.version}: {spec.title}.'
    lines = [*_comment(f'{title} {spec.description}', 0), '#']
    lines += [f'# {line}'.rstrip() for line in _HOW.splitlines()]
    lines.append(f'spec: {_flow(SpecId.of(spec).model_dump())}')
    lines.append('params:')
    for name, info in spec.params.model_fields.items():
        note = ''
        if name in filled:
            note = f' The row source {" and ".join(filled[name])} gives it per row.'
        lines += [f'  {line}' for line in _described(name, info, bound, 0, note)]
        default = '' if info.is_required() else f' {_flow(_default(info))}'
        lines.append(f'  #{name}:{default}')
    return '\n'.join(lines) + '\n'


def _comment(text: str, depth: int) -> list[str]:
    """
    ``text`` as comment lines, indented by ``depth`` levels inside the
    comment, and a line after the first by two more.
    """
    indent = '# ' + '  ' * depth
    return textwrap.wrap(
        text,
        width=_WIDTH - 2,  # a parameter's lines are indented by two
        initial_indent=indent,
        subsequent_indent=f'{indent}    ',
        break_on_hyphens=False,
    )


def _default(info: FieldInfo) -> Any:
    """The default of a field that has one, in its JSON form."""
    return to_jsonable_python(info.get_default(call_default_factory=True))


def _flow(value: Any) -> str:
    """``value``, plain data, as YAML on one line."""
    text = yaml.safe_dump(
        value, default_flow_style=True, width=float('inf'), allow_unicode=True
    )
    return text.strip().removesuffix('...').strip()


def _described(
    name: str,
    info: FieldInfo,
    bound: Sequence[WorkflowSpec[Any, Any]],
    depth: int,
    note: str = '',
) -> list[str]:
    """
    The comment lines on the field ``name``, then those of the fields of each
    group it accepts, one level deeper.
    """
    given = 'required' if info.is_required() else f'default {_flow(_default(info))}'
    metadata = field_types.field_metadata(info)
    takes, groups = _describe_type(info.annotation, metadata, bound, top=depth == 0)
    text = f'{name} ({given}): {takes}.'
    if info.description:
        text += f' {info.description}'
    lines = _comment(text + note, depth)
    for tag, group in groups:
        inner = depth + 1
        if tag is not None:
            lines += _comment(f'with {tag[0]}: {tag[1]}', inner)
            inner += 1
        for field, field_info in group.model_fields.items():
            if tag is None or field != tag[0]:
                lines += _described(field, field_info, bound, inner)
    return lines


type _Groups = list[tuple[tuple[str, str] | None, type[BaseModel]]]
"""
The groups that a value may be. Each has the tag field and the value that
select it in a union, or ``None`` for a single group.
"""


def _describe_type(
    annotation: Any,
    metadata: tuple[Any, ...],
    bound: Sequence[WorkflowSpec[Any, Any]],
    *,
    top: bool,
) -> tuple[str, _Groups]:
    """
    What a field of type ``annotation`` accepts, in words, and the groups whose
    fields it accepts.

    ``top`` is true for a parameter. Only there is a file written as in a cell,
    since :func:`~.table.as_request` converts the value of a parameter and not
    the fields inside it. A file in a group is written in its JSON form.
    """
    field = field_types.shape(annotation, metadata)
    ways: list[str] = []
    groups: _Groups = []
    match field.inline:
        case List(element):
            each, groups = _describe_type(element, (), bound, top=top)
            ways.append(f'a list, each {each}')
        case Choice(tag, alternatives):
            ways.append(f'a group of one of the kinds below, chosen by its field {tag}')
            groups = [((tag, value), g) for value, g in alternatives.items()]
        case FileKind(marker):
            ways.append(_file(marker, top=top))
        case Group(group):
            ways.append('a group of the fields below')
            groups = [(None, group)]
        case Scalar(tp):
            ways.append(_scalar(tp))
    if referenced := field.referenced:
        ways.append(_output(Union[referenced], bound))  # noqa: UP007
    if field.none:
        ways.append('null for none')
    return ', or '.join(ways), groups


def _file(marker: File, *, top: bool) -> str:
    """
    What a file field of ``marker`` accepts, in words.

    For a raw file, the role is a suggestion, as a picker offers it: a run need
    not have it. A file of another kind must have the metadata of the marker.
    """
    metadata = marker.metadata
    kind = metadata.pop('kind')
    if kind == 'raw':
        said = 'a run'
        if role := metadata.pop('role', None):
            said += f', such as one of role {role} in files.csv'
    else:
        said = f'a file of kind {kind}'
    if metadata:
        said += f', with {", ".join(f"{k} {v}" for k, v in metadata.items())}'
        said += ' in files.csv'
    if not top:
        said += ", written {'@ref': file, file: ...}"
    return said


def _scalar(tp: Any) -> str:
    """What a field of a scalar type ``tp`` accepts, in words."""
    words = {bool: 'true or false', int: 'a whole number', float: 'a number'}
    if tp in words:
        return words[tp]
    if tp is str:
        return 'text'
    if tp in field_types.TIMES:
        return 'a time, such as 2026-11-23T14:00:00+01:00'
    if isinstance(tp, type) and issubclass(tp, Enum):
        return f'one of {", ".join(str(m.value) for m in tp)}'
    if get_origin(tp) is Literal:
        return f'one of {", ".join(map(str, get_args(tp)))}'
    return field_types.type_name(tp)


def _output(tp: Any, bound: Sequence[WorkflowSpec[Any, Any]]) -> str:
    """What a field of type ``Referenced[tp]`` accepts: the outputs that fit."""
    fitting = dict.fromkeys(
        f'{name} of {spec.name}'
        for spec in bound
        for name, info in spec.outputs.model_fields.items()
        if field_types.fits(info.annotation, tp)
    )
    said = f'an output of type {field_types.type_name(tp)}'
    if not fitting:
        return f'{said}, which no spec bound gives'
    return f'{said}, such as {", ".join(fitting)}'

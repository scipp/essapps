# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Params as a person writes them in a text file: a cell of a table, or a value of
a YAML file.

A cell is text, and is read as JSON if it is JSON, so that a cell holds anything
a request can::

    1.5                 a number
    water               a string, since it is not JSON
    null                None
    {"x": 0, "y": 1}    a group
    (empty)             no value: the parameter takes its default

A file has no JSON form that a person would write, so at a parameter that is a
file field, or a list of them, a file is named by text (see :func:`named_file`)::

    60339               the run 60339
    /data/mask.xml      a path
    pid:20.500/abc      anything else by its kind, as ``FileRef.file`` holds it

Only a parameter is read this way, not a file field inside a group, which takes
the JSON form of a file, as the record holds it.

A file that is written before the records it reads exist names them by label
(see :func:`with_latest`)::

    masks: {'@latest': masks, output: masks}

A template is a YAML file (see :func:`load_template`). :func:`write_template`
writes each file and reference in its JSON form, which reads back as the same.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args, get_origin

import yaml
from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_json

from ess.spec import FileRef, field_types

from ..client import Client
from ..files import file
from ..records import Template

_KIND = re.compile(r'[a-z]+:')


def read_cell(text: str) -> Any:
    """The value of a cell that is not empty: its JSON value, or else its text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def write_cell(value: Any) -> str:
    """The text that :func:`read_cell` and :func:`named_file` read as ``value``."""
    if isinstance(value, FileRef):
        return value.file
    if isinstance(value, str) and read_cell(value) == value:
        return value
    if isinstance(value, list):
        value = [v.file if isinstance(v, FileRef) else v for v in value]
    return to_json(value).decode()


def named_file(name: Any) -> Any:
    """
    The file that ``name`` names: a whole number is a run, text that starts with
    a kind, such as ``pid:``, is the file of that kind, and other text is a
    path, relative to the working directory. Anything else is returned as it is,
    for the workspace to refuse.
    """
    if isinstance(name, int) and not isinstance(name, bool):
        return file(run=name)
    if not isinstance(name, str):
        return name
    if _KIND.match(name):
        return FileRef(file=name)
    return file(path=Path(name).absolute())


def _takes_files(annotation: Any, metadata: tuple[Any, ...]) -> bool:
    """Whether a parameter is a file field or a list of file fields."""
    tp, _, metadata = field_types.unwrapped(annotation, metadata)
    if get_origin(tp) is list:
        tp, _, metadata = field_types.unwrapped(get_args(tp)[0])
    inline, inline_metadata, _ = field_types.split(tp, *metadata)
    return field_types.file_marker(inline, *inline_metadata) is not None


def with_named_files(
    model: type[BaseModel], params: Mapping[str, Any]
) -> dict[str, Any]:
    """
    ``params`` of the params model ``model``, with each name at a parameter that
    takes files as the file it names (see :func:`named_file`).
    """
    read = dict(params)
    for name, info in model.model_fields.items():
        if name in read and _takes_files(
            info.annotation, field_types.field_metadata(info)
        ):
            value = read[name]
            read[name] = (
                [named_file(v) for v in value]
                if isinstance(value, list)
                else named_file(value)
            )
    return read


def with_latest(client: Client, value: Any) -> Any:
    """
    ``value``, such as the params of a template, with each object that has the
    key ``@latest`` as the reference to ``output`` of the newest record under
    that label, of ``member`` if given, as the client finds it now. The request
    made of it holds the record's ID.

    Raises ``ValueError`` if no record has the label, or the output.
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
    The template in a YAML file, which may be JSON, with files as written.
    Raises ``ValueError`` if the file is not a template.
    """
    try:
        return TypeAdapter(Template).validate_python(yaml.safe_load(path.read_text()))
    except yaml.YAMLError as error:
        raise ValueError(f'{path}: {error}') from None


def write_template(template: Template, path: Path) -> None:
    """Write ``template`` as :func:`load_template` reads it back."""
    value = TypeAdapter(Template).dump_python(template, mode='json')
    path.write_text(yaml.safe_dump(value, sort_keys=False))

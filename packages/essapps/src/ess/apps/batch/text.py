# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates as a person writes them: YAML files (see :func:`load_template`).

At a parameter that takes a file, or a list of them, a file is named as in a
cell of a table (:func:`~.table.as_request`)::

    sample: run:60339           the file of that name
    sample: 60339               the run 60339
    masks: [path:masks/a.xml]   a file of the workspace's folder, by its identity

A file field inside a group takes the JSON form of a file, as the record holds
it.

A file that is written before the records it reads exist names them by label
(see :func:`with_latest`)::

    masks: {'@latest': masks, output: masks}

:func:`write_template` writes each file and reference in its JSON form, which
reads back as the same. :func:`checked_settings` reads the settings of a batch
from a template, which must be of the batch's spec and its version, and
``write_template(batch.template(), path)`` writes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter

from ..client import Client
from ..records import SpecId, Template
from .reduction import Batch
from .table import as_request


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


def checked_settings(batch: Batch, template: Template) -> dict[str, Any]:
    """
    The settings of ``batch`` that ``template`` holds: the value of each
    parameter that is not a column of ``batch``, with files named as in a cell
    (:func:`~.table.as_request`) and ``@latest`` read (:func:`with_latest`).
    The blanks of the template are not read: the columns of the batch are the
    blanks.

    Raises ``ValueError``, saying why in a person's words, if the template is
    of another spec or of another version of it, or if a value cannot be read.
    """
    theirs, ours = template.spec, SpecId.of(batch.spec)
    if theirs.name != ours.name:
        raise ValueError(f'it holds the settings of {theirs.name}, not {ours.name}')
    if theirs.version != ours.version:
        raise ValueError(
            f'it holds the settings of version {theirs.version} of {ours.name}, '
            f'not of version {ours.version}; the settings of one version may '
            'mean something else in another, so check them, and set the '
            f'version in the template to {ours.version}'
        )
    params = {k: v for k, v in template.params.items() if k not in batch.columns}
    return with_latest(batch.client, as_request(batch.spec.params, params))


def write_template(template: Template, path: Path) -> None:
    """Write ``template`` as :func:`load_template` reads it back."""
    value = TypeAdapter(Template).dump_python(template, mode='json')
    path.write_text(yaml.safe_dump(value, sort_keys=False))

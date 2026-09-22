# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The ``essapps`` command: serve a backend, list its specs and datasets, submit
a run, wait for it, read an output.

Records are the only state kept between invocations, which is what these
commands are meant to show: the shape a service needs on top of the client
interface is small.
"""

from __future__ import annotations

import getpass
import json
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from .backend import SubmitError
from .client import Client, local_backend
from .records import RunRecord, Status
from .remote import RemoteBackend, remote
from .server import serve as serve_backend
from .sources import FolderSource
from .spec import OutputRef, SpecId, parse_ref


@dataclass
class Env:
    """The group's options, held until a command needs a client."""

    url: str
    instrument: str | None
    proposal: str | None
    submitter: str

    def client(self) -> Client:
        if self.instrument is None or self.proposal is None:
            raise click.ClickException('--instrument and --proposal are required')
        return remote(
            self.url,
            instrument=self.instrument,
            proposal=self.proposal,
            submitter=self.submitter,
        )


@click.group()
@click.option('--url', envvar='ESSAPPS_URL', default='http://127.0.0.1:8000')
@click.option('--instrument', envvar='ESSAPPS_INSTRUMENT', default=None)
@click.option('--proposal', envvar='ESSAPPS_PROPOSAL', default=None)
@click.option('--submitter', envvar='ESSAPPS_SUBMITTER', default=None)
@click.pass_context
def main(
    ctx: click.Context,
    url: str,
    instrument: str | None,
    proposal: str | None,
    submitter: str | None,
) -> None:
    """Talk to an essapps backend over HTTP: serve, submit, wait, or read an output."""
    ctx.obj = Env(
        url=url,
        instrument=instrument,
        proposal=proposal,
        submitter=submitter or getpass.getuser(),
    )


@main.command()
@click.option('--root', required=True, type=click.Path(path_type=Path))
@click.option('--registry', required=True, help='module:function, importable by a run.')
@click.option('--datasets', required=True, type=click.Path(path_type=Path))
@click.option('--pattern', default='*.h5', show_default=True)
@click.option('--host', default='127.0.0.1', show_default=True)
@click.option('--port', default=8000, show_default=True, type=int)
def serve(
    root: Path, registry: str, datasets: Path, pattern: str, host: str, port: int
) -> None:
    """Run a backend as a service; every request is its own throwaway process."""
    backend = local_backend(
        root,
        registry=registry,
        throwaway=True,
        sources=[FolderSource(datasets, pattern)],
    )
    serve_backend(backend, host=host, port=port)


@main.command()
@click.option(
    '--json', 'as_json', is_flag=True, help='The serialized specs, schemas included.'
)
@click.pass_obj
def specs(env: Env, as_json: bool) -> None:
    """List the backend's specs, one per line: id, title, description."""
    with closing(RemoteBackend(env.url)) as backend:
        found = backend.specs()
    if as_json:
        click.echo(json.dumps([s.model_dump(mode='json') for s in found], indent=2))
        return
    for spec in found:
        click.echo(f'{spec.id}\t{spec.title}\t{spec.description}')


@main.command()
@click.pass_obj
def datasets(env: Env) -> None:
    """List the datasets the backend's sources know: reference and path."""
    with closing(env.client()) as client:
        for dataset in client.datasets():
            click.echo(f'{dataset.ref}\t{dataset.path}')


def _unwrap_optional(schema: dict[str, Any]) -> dict[str, Any]:
    """The non-null branch of an ``anyOf`` schema that allows null, else itself."""
    for branch in schema.get('anyOf', ()):
        if branch.get('type') != 'null':
            return branch
    return schema


def _parse_ref(ctx: click.Context, param: click.Parameter, value: str | None) -> Any:
    return None if value is None else parse_ref(value).model_dump(mode='json')


def _parse_json(ctx: click.Context, param: click.Parameter, value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _option(
    name: str, prop: dict[str, Any], *, required: bool, data: bool
) -> click.Option:
    """
    A click option for one property of a params model's JSON schema.

    A data field takes a reference string, parsed with :func:`parse_ref`.
    Otherwise ``anyOf`` with null unwraps to its non-null branch; an enum
    becomes a ``click.Choice``; ``integer``, ``number``, ``string``, ``boolean``
    map to the matching click type, a boolean as ``--x/--no-x``; anything else
    (array, object, ``$ref``, a collection of references) is a JSON string.
    """
    flag = f'--{name.replace("_", "-")}'
    kwargs: dict[str, Any] = {'required': required, 'help': prop.get('description')}
    if 'default' in prop:
        # Passing default=None explicitly would satisfy click's required
        # check, so a field with no schema default gets no default kwarg.
        kwargs['default'] = prop['default']
    if data:
        prefix = f'{kwargs["help"]}; ' if kwargs['help'] else ''
        kwargs['help'] = (
            f'{prefix}a reference: run:<instrument>/<run>, pid:<pid>, '
            'path:<path>, or <record>.<output>[key]'
        )
        return click.Option([flag], callback=_parse_ref, **kwargs)
    schema = _unwrap_optional(prop)
    if 'enum' in schema:
        choices = [str(v) for v in schema['enum']]
        return click.Option([flag], type=click.Choice(choices), **kwargs)
    kind = schema.get('type')
    if kind == 'boolean':
        return click.Option([f'{flag}/--no-{name.replace("_", "-")}'], **kwargs)
    if kind == 'integer':
        return click.Option([flag], type=int, **kwargs)
    if kind == 'number':
        return click.Option([flag], type=float, **kwargs)
    if kind == 'string':
        return click.Option([flag], type=str, **kwargs)
    return click.Option([flag], callback=_parse_json, **kwargs)


def _submit_command(schema: dict[str, Any]) -> click.Command:
    """A click command whose options are generated from a params JSON schema."""
    required = set(schema.get('required', ()))
    params = [
        _option(name, prop, required=name in required, data='dataField' in prop)
        for name, prop in schema.get('properties', {}).items()
    ]
    return click.Command('submit', params=params, callback=lambda **kwargs: kwargs)


@main.command(
    add_help_option=False,
    context_settings={'ignore_unknown_options': True, 'allow_extra_args': True},
)
@click.argument('spec_text', metavar='SPEC')
@click.option('--label', default=None)
@click.option('--member-key', default=None)
@click.pass_context
def submit(
    ctx: click.Context, spec_text: str, label: str | None, member_key: str | None
) -> None:
    """Submit a run of SPEC (name/vN); its flags come from the spec's params."""
    try:
        spec_id = SpecId.parse(spec_text)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    with closing(ctx.obj.client()) as client:
        try:
            spec = client.spec(spec_id)
        except KeyError as e:
            raise click.ClickException(str(e)) from e
        command = _submit_command(spec.params_schema)
        sub_ctx = command.make_context('submit', list(ctx.args), parent=ctx)
        with sub_ctx:
            params = command.invoke(sub_ctx)
        request = client.request(spec_id, params, label=label, member_key=member_key)
        try:
            record = client.submit(request)
        except SubmitError as e:
            raise click.ClickException(str(e)) from e
    click.echo(record.id)


@main.command()
@click.argument('record_id')
@click.option('--timeout', default=60.0, type=float, show_default=True)
@click.pass_obj
def wait(env: Env, record_id: str, timeout: float) -> None:
    """Block until RECORD_ID is terminal; print its id and status."""
    with closing(env.client()) as client:
        try:
            (record,) = client.wait([record_id], timeout=timeout)
        except TimeoutError:
            click.echo(f'{record_id}: timed out after {timeout}s', err=True)
            sys.exit(1)
    click.echo(f'{record.id} {record.status.value}')
    if record.status == Status.FAILED:
        click.echo(record.failure.message if record.failure else 'failed', err=True)
        sys.exit(1)


def _literal_or_file(
    client: Client, record: RunRecord, ref: OutputRef, into: Path | None, server: bool
) -> str | None:
    """A literal as JSON, a stored output as a path when one was asked for."""
    if ref.output in record.outputs:
        value = record.outputs[ref.output]
        return json.dumps(value if ref.key is None else value[ref.key])
    if into is not None:
        return str(client.write_out(ref, into))
    if server:
        return str(client.write_out(ref))
    return None


@main.command()
@click.argument('record_id')
@click.argument('output', required=False)
@click.option('--key', default=None)
@click.option(
    '--to',
    'into',
    type=click.Path(path_type=Path),
    default=None,
    help='Download a stored output into this folder and print its path.',
)
@click.option(
    '--server-path',
    is_flag=True,
    help="Print a stored output's path on the server instead of its value.",
)
@click.pass_obj
def output(
    env: Env,
    record_id: str,
    output: str | None,
    key: str | None,
    into: Path | None,
    server_path: bool,
) -> None:
    """
    Print an output of RECORD_ID, or list its outputs when OUTPUT is omitted.

    A literal prints as JSON. A stored output prints as its value, or as a
    path with --to or --server-path; in a listing it prints as its reference.
    """
    with closing(env.client()) as client:
        record = client.record(record_id)
        if output is not None:
            ref = OutputRef(record=record_id, output=output, key=key)
            text = _literal_or_file(client, record, ref, into, server_path)
            click.echo(str(client.output(ref)) if text is None else text)
            return
        for name in sorted(record.output_names()):
            ref = OutputRef(record=record_id, output=name)
            text = _literal_or_file(client, record, ref, into, server_path)
            click.echo(f'{name}\t{ref if text is None else text}')

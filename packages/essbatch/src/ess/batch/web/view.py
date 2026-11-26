# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The models of :mod:`.models` built from the state of a batch, and the value of
a cell that the page sends. These are functions of their arguments only;
:class:`~.page.Page` gathers the arguments.

A parameter has the same title in a column, a field of the settings and a
message (:func:`titles`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import pandas as pd
from pydantic import BaseModel

from ess.apps.errors import FieldError
from ess.apps.fields import UNSET, Fields
from ess.apps.listing import Listing
from ess.apps.records import Status
from ess.spec import WorkflowSpec, field_types

from ..definition import OutputOptions
from ..state import RowState, left_to_reduce, ready
from ..table import NAME, as_request, row_names
from . import forms
from .errors import Rejected, request_error_text, titled_field_error
from .models import (
    Action,
    CellEdit,
    Column,
    Format,
    Output,
    OutputShown,
    SourceRow,
    SourceRows,
    SubmitProblem,
)

if TYPE_CHECKING:
    from ess.apps.writers import Writer

ORDER = (
    'ready',
    'running',
    'done',
    'settings changed',
    'outdated',
    'failed',
    'cancelled',
    'incomplete',
)
"""The statuses of a row, in the order of the page."""


def titles(params: type[BaseModel]) -> dict[str, str]:
    """The title of each parameter of ``params``, in their order."""
    return {n: field_types.title(n, i) for n, i in params.model_fields.items()}


def note(name: str) -> Column:
    """The column of the note ``name``."""
    return Column(name=name, title=name.replace('_', ' ').capitalize())


def listed(given: pd.DataFrame, source_columns: Collection[str]) -> SourceRows:
    """
    The rows of a row source, as a dialog lists them.

    ``given`` has a column ``incomplete`` and may have a column ``taken``.
    Every other column except ``NAME`` and ``source_columns`` is a note.
    """
    named = row_names(given, ())
    marks = {'incomplete', 'taken'}
    shown = [c for c in given.columns if c not in {NAME, *source_columns, *marks}]

    def text(value: Any) -> str:
        return '' if pd.api.types.is_scalar(value) and pd.isna(value) else str(value)

    def mark(row: Mapping[str, Any]) -> str:
        if row['incomplete']:
            return 'incomplete'
        if 'taken' not in row:
            return ''
        return 'taken' if row['taken'] else 'left out'

    return SourceRows(
        columns=[note(c) for c in shown],
        rows=[
            SourceRow(
                key=key,
                name=named[key],
                cells={c: text(row[c]) for c in shown},
                mark=mark(row),
            )
            for key, row in given.to_dict('index').items()
        ],
    )


def read(fields: Fields, edit: CellEdit) -> Any:
    """
    The value of ``edit``, read by the field of its column. Raises
    :class:`Rejected` for an unknown column or a value the field rejects.
    """
    if edit.column not in fields.fields:
        raise Rejected(f'There is no column {edit.column}.')
    try:
        return forms.read(fields.fields[edit.column], edit.value)
    except (KeyError, ValueError) as error:
        raise Rejected(
            titled_field_error(fields.titles[edit.column], request_error_text(error))
        ) from None


def text(listing: Listing, value: Any) -> str:
    """The text of a cell that holds ``value``, empty if unset."""
    return '' if value is UNSET else listing.text(value)


def cell_text(listing: Listing, params: type[BaseModel], name: str, cell: Any) -> str:
    """The text of a cell of the column ``name`` of ``params``."""
    if cell is None:
        return ''
    return text(listing, as_request(params, {name: cell})[name])


def field_errors_text(errors: Sequence[FieldError], titles: Mapping[str, str]) -> str:
    """``errors`` as one message, each with the title of its setting."""
    return '; '.join(_field_error_text(e, titles) for e in errors)


def _field_error_text(error: FieldError, titles: Mapping[str, str]) -> str:
    if not error.loc:
        return error.msg
    name = str(error.loc[0])
    return titled_field_error(titles.get(name, name), error.msg)


def settings_problem(
    errors: Sequence[FieldError], unfit: Sequence[str], titles: Mapping[str, str]
) -> str | None:
    """
    The message of ``errors`` and of the settings ``unfit`` that the form
    cannot show; ``None`` if there are none.
    """
    told = [field_errors_text(errors, titles)] if errors else []
    if unfit:
        told.insert(0, f'Settings that this form cannot show: {_join(unfit)}')
    return '; '.join(told) or None


def refusal(
    error: FieldError, named: Mapping[str, str], titles: Mapping[str, str]
) -> SubmitProblem:
    """``error`` of a rejected reduction, with the row name from ``named``."""
    text = _field_error_text(error, titles)
    if error.request_key is not None:
        text = f'{named.get(error.request_key, error.request_key)}: {text}'
    return SubmitProblem(
        request_key=error.request_key, loc=list(error.loc), message=error.msg, text=text
    )


def problems(
    key: str, errors: Iterable[FieldError], titles: Mapping[str, str]
) -> dict[str, str]:
    """The message of each error at a cell of row ``key``, by column."""
    return {
        str(e.loc[0]): _field_error_text(e, titles)
        for e in errors
        if e.request_key == key and e.loc
    }


def failure(text: str | None) -> str:
    """
    The message of a failed result, without the type of the exception;
    ``'Cancelled.'`` for ``None``.
    """
    return re.sub(r'^\w+(Error|Exception): ', '', text) if text else 'Cancelled.'


def reason(
    state: RowState,
    problems: Mapping[str, str],
    unsubmitted: str,
    failure: str,
    titles: Mapping[str, str],
) -> tuple[str, str]:
    """
    Why a row has its status, and for ``settings changed``, the titles of the
    settings changed since.

    ``problems`` are the messages at its cells, ``unsubmitted`` is why Follow
    could not reduce it, ``failure`` is why its result failed.
    """
    status = state.status
    if unsubmitted:
        return unsubmitted, ''
    if status in ('failed', 'cancelled') and state.record is not None:
        return failure, ''
    if problems:
        return '; '.join(problems.values()), ''
    if status == 'settings changed':
        named = _changed(state, titles)
        changed = f'{named} changed' if named else ''
        since = f': {changed} since' if changed else ''
        return (
            f'Reduced with earlier settings{since}. "Reduce" leaves such rows;'
            ' "Reduce rows of earlier settings" reduces them with the settings now.',
            changed,
        )
    if status == 'outdated' and state.outdated:
        return _outdated(state, titles), ''
    if status == 'outdated':
        return 'The settings cannot be used, so this result may not match them.', ''
    return '', ''


def _outdated(state: RowState, titles: Mapping[str, str]) -> str:
    """Why the result of a row is outdated: what changed since."""
    told = []
    if changed := _changed(state, titles):
        told.append(f'{changed} changed.')
    if state.spec_changed:
        told.append('It was reduced by another version of the workflow.')
    return ' '.join(told)


def _changed(state: RowState, titles: Mapping[str, str]) -> str:
    """The titles of the parameters that changed, in words."""
    return _join([t for n, t in titles.items() if n in (state.changed or ())])


def result(state: RowState) -> str | None:
    """``'current'`` or ``'outdated'`` for a completed result, else ``None``."""
    if state.record is None or state.record_status is not Status.COMPLETED:
        return None
    return 'outdated' if state.status == 'outdated' else 'current'


def counts(states: Mapping[str, RowState]) -> dict[str, int]:
    """The number of rows, as ``'all'``, and of rows with each status."""
    counted = dict.fromkeys(ORDER, 0)
    for s in states.values():
        counted[s.status] += 1
    return {'all': len(states), **counted}


def action(
    states: Mapping[str, RowState], blocked: str | None, label: str | None = None
) -> Action:
    """The primary button, labelled ``label`` if ``blocked``."""
    left = [] if blocked else left_to_reduce(states)
    kinds = {states[k].status for k in left}
    n = len(left)
    if blocked:
        label = label or 'Check the settings'
    elif not left:
        label = 'Nothing left to reduce'
    elif kinds == {'failed'}:
        label = f'Retry {n} failed'
    elif kinds == {'cancelled'}:
        label = f'Retry {n} cancelled'
    elif kinds <= {'failed', 'cancelled'}:
        label = f'Retry {n}'
    elif kinds == {'outdated'}:
        label = f'Reduce {n} outdated'
    else:
        label = f'Reduce {n} remaining'
    return Action(
        label=label,
        blocked=blocked,
        remaining=n,
        again=len(ready(states)),
        running=sum(s.record_status is Status.PENDING for s in states.values()),
        progress=_progress(states),
    )


def _progress(states: Mapping[str, RowState]) -> tuple[int, int] | None:
    """``(finished, total)`` of the rows submitted since the earliest running."""
    pending = [
        s.record
        for s in states.values()
        if s.record_status is Status.PENDING and s.record
    ]
    if not pending:
        return None
    since = min(r.created for r in pending)
    started = [
        s for s in states.values() if s.record is not None and s.record.created >= since
    ]
    return sum(s.record_status is not Status.PENDING for s in started), len(started)


def output(
    options: OutputOptions,
    writers: Sequence[Writer],
    spec: WorkflowSpec[Any, Any],
    catalogue: bool,
) -> OutputShown:
    """The tab "Output"; ``catalogue`` is true if the page can publish."""
    formats = [
        Format(
            name=w.name,
            title=w.title,
            description=w.description,
            outputs=None if w.outputs is None else list(w.outputs),
        )
        for w in writers
    ]
    problem = (
        ''
        if any(f.name == options.format for f in formats)
        else f'This application has no writer of the format {options.format} '
        'that the form names: pick a format and apply.'
    )
    lacking = ', no '.join(options.lacking)
    outputs = spec.outputs.model_fields
    return OutputShown(
        options=options,
        formats=formats,
        outputs=[Output(name=n, title=f.title or n) for n, f in outputs.items()],
        catalogue=catalogue,
        problem=problem,
        unpublishable=(
            f'The form names no {lacking}: fill it in and apply it.' if lacking else ''
        ),
    )


def _join(words: Sequence[str]) -> str:
    """``['a', 'b', 'c']`` as ``'a, b and c'``."""
    return ' and '.join([', '.join(words[:-1]), words[-1]] if len(words) > 1 else words)

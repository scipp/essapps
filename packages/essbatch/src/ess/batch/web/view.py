# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
How the state of the page of one batch reads, as the models of
:mod:`.models`, and how a cell sent back reads: functions of their arguments
only. :class:`~.page.Page` gathers the arguments and does the actions. The
rows of a source in a dialog (:func:`listed`) and a cell (:func:`read`,
:func:`text`) read the same in the list of batches and the Runs view.

A parameter has one title wherever the page names it, in a column, a field
of the settings or a message (:func:`titles`).
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

from ..definition import Form
from ..state import RowState, left_to_reduce, ready
from ..table import NAME, as_request, names
from . import forms
from .models import (
    Action,
    CellEdit,
    Column,
    Format,
    Output,
    OutputShown,
    Refused,
    SourceRow,
    SourceRows,
)
from .refusals import Problem, say, why

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
"""The statuses of a row, in the words and the order of the page."""


def titles(params: type[BaseModel]) -> dict[str, str]:
    """The title of each parameter of ``params``, in their order."""
    return {n: field_types.title(n, i) for n, i in params.model_fields.items()}


def note(name: str) -> Column:
    """The column of the note ``name``, titled as the page shows it."""
    return Column(name=name, title=name.replace('_', ' ').capitalize())


def listed(given: pd.DataFrame, fills: Collection[str]) -> SourceRows:
    """
    The rows of ``given``, the rows of a source with a column
    ``waits``, and maybe a column ``taken``, as a dialog lists them: each
    named as :func:`names` names it, with the text of each note, the columns
    but ``fills``, and its mark.
    """
    named = names(given, ())
    marks = {'waits', 'taken'}
    shown = [c for c in given.columns if c not in {NAME, *fills, *marks}]

    def text(value: Any) -> str:
        return '' if pd.api.types.is_scalar(value) and pd.isna(value) else str(value)

    def mark(row: Mapping[str, Any]) -> str:
        if row['waits']:
            return 'waits'
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
    """The value of ``edit`` as the field of its column reads it."""
    if edit.column not in fields.fields:
        raise Problem(f'There is no column {edit.column}.')
    try:
        return forms.read(fields.fields[edit.column], edit.value)
    except (KeyError, ValueError) as error:
        raise Problem(say(fields.titles[edit.column], why(error))) from None


def text(listing: Listing, value: Any) -> str:
    """
    What a cell shows (:meth:`~ess.apps.listing.Listing.text`); nothing if
    it is unset.
    """
    return '' if value is UNSET else listing.text(value)


def cell_text(listing: Listing, params: type[BaseModel], name: str, cell: Any) -> str:
    """What a cell of the column ``name`` of ``params`` that holds ``cell`` shows."""
    if cell is None:
        return ''
    return text(listing, as_request(params, {name: cell})[name])


def said(errors: Sequence[FieldError], titles: Mapping[str, str]) -> str:
    """Errors of the settings in words, each with its setting's title."""
    return '; '.join(_said_at(e, titles) for e in errors)


def _said_at(error: FieldError, titles: Mapping[str, str]) -> str:
    if not error.loc:
        return error.msg
    name = str(error.loc[0])
    return say(titles.get(name, name), error.msg)


def settings_problem(
    errors: Sequence[FieldError], unfit: Sequence[str], titles: Mapping[str, str]
) -> str | None:
    """
    What the settings lack or what is refused of them, and the settings
    ``unfit`` that the form cannot show; ``None`` if nothing.
    """
    told = [said(errors, titles)] if errors else []
    if unfit:
        told.insert(0, f'Settings that this form cannot show: {_join(unfit)}')
    return '; '.join(told) or None


def refusal(
    error: FieldError, named: Mapping[str, str], titles: Mapping[str, str]
) -> Refused:
    """``error`` of a reduction refused, with its row named as ``named`` names it."""
    text = _said_at(error, titles)
    if error.request_key is not None:
        text = f'{named.get(error.request_key, error.request_key)}: {text}'
    return Refused(
        entry=error.request_key, loc=list(error.loc), message=error.msg, text=text
    )


def problems(
    key: str, errors: Iterable[FieldError], titles: Mapping[str, str]
) -> dict[str, str]:
    """The reason of each of ``errors`` at a cell of row ``key``, by column."""
    return {
        str(e.loc[0]): _said_at(e, titles)
        for e in errors
        if e.request_key == key and e.loc
    }


def failure(text: str | None) -> str:
    """
    Why a result failed, without the name of the exception that made it;
    ``text`` is ``None`` for a result cancelled.
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
    Why a row reads as its status, and, if it reads ``settings changed``, the titles
    of what changed since. ``problems`` are those of its cells,
    ``unsubmitted`` is why Follow could not reduce it, and ``failure`` why
    its result failed.
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
    """Whether a row has a result, ``'current'`` or ``'outdated'``; else ``None``."""
    if state.record is None or state.record_status is not Status.COMPLETED:
        return None
    return 'outdated' if state.status == 'outdated' else 'current'


def counts(states: Mapping[str, RowState]) -> dict[str, int]:
    """How many rows there are, and how many read as each status."""
    counted = dict.fromkeys(ORDER, 0)
    for s in states.values():
        counted[s.status] += 1
    return {'all': len(states), **counted}


def action(
    states: Mapping[str, RowState], blocked: str | None, label: str | None = None
) -> Action:
    """What the button does now, or ``label`` if blocked; how far rows run."""
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
    """
    Of the rows whose results were submitted since the first still running,
    how many are finished and how many there are; ``None`` if none is running.
    """
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
    form: Form,
    writers: Sequence[Writer],
    spec: WorkflowSpec[Any, Any],
    catalogue: bool,
) -> OutputShown:
    """
    The tab "Output": the ``form`` kept, the ``writers`` that it may pick,
    the outputs of ``spec``, and whether a catalogue takes the results.
    """
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
        if any(f.name == form.format for f in formats)
        else f'This application has no writer of the format {form.format} '
        'that the form names: pick a format and apply.'
    )
    lacking = ', no '.join(form.lacking)
    outputs = spec.outputs.model_fields
    return OutputShown(
        form=form,
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

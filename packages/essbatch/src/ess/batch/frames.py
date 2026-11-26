# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch run as a command: reduce its rows, wait, and return its batch table
with what came of each row.

:func:`reduce_table` reduces a table
with a template, keeping nothing; :func:`reduce_batch` reduces the rows of a
batch, such as one of a manager. Both return the table with the columns of
:data:`RESULT`, so the table that comes back is the table that went in::

    key    sample     name    status  record    failure         published
    60339  run:60339  water   done    3f2a...                   pid:20.500/...
    60340  run:60340  sds     failed  81c0...   refused: ...
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from ess.apps.client import Client
from ess.apps.records import Template

from . import definition
from .reduction import Batch
from .state import RowState, Which, to_reduce
from .table import RESULT
from .templates import checked_settings


def reduce_table(
    client: Client,
    template: Template,
    table: pd.DataFrame,
    *,
    label: str,
    staged: bool = False,
    persist: bool | Sequence[str] = False,
    remaining: bool = False,
    settings_changed: bool = False,
) -> pd.DataFrame:
    """
    Reduce each row of ``table`` with the settings of ``template``, wait, and
    return the table with what came of each row (see :func:`reduce_batch`).
    The batch is of the spec of the template's name that the workspace binds
    (:meth:`~ess.apps.Client.spec_named`). It keeps nothing in definitions, so
    no manager lists it.

    Parameters
    ----------
    template:
        The settings, as :func:`~.templates.checked_settings` reads them.
    table:
        The rows, as :meth:`~.reduction.Batch.set_table` takes them.
    label:
        The label of the records. A batch is found again by it, so each batch
        has its own.
    staged:
        Run the rows through a stage.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    remaining, settings_changed:
        As :func:`reduce_batch` takes them.

    Raises
    ------
    KeyError
        If the workspace binds no spec of the template's name.
    SubmitError, ValueError
        As :func:`~.templates.checked_settings`, :func:`reduce_batch` and
        :class:`~.reduction.Batch` raise them.
    """
    spec = client.spec_named(template.spec.name)
    batch = Batch(
        client,
        spec,
        label=label,
        settings=checked_settings(client, spec, template),
        table=table,
        staged=staged,
        persist=persist,
    )
    return reduce_batch(batch, remaining=remaining, settings_changed=settings_changed)


def reduce_batch(
    batch: Batch,
    keys: Sequence[str] | None = None,
    *,
    remaining: bool = False,
    settings_changed: bool = False,
) -> pd.DataFrame:
    """
    Reduce the rows of ``keys`` of ``batch``, or all of its rows, wait, close
    the batch, and return those rows of its table with what came of each
    (:func:`with_results`).

    Parameters
    ----------
    remaining:
        Reduce only the rows that :func:`~.state.left_to_reduce` gives. A row
        reduced with earlier settings is left.
    settings_changed:
        Together with ``remaining``, reduce also the rows reduced with earlier
        settings (:func:`~.state.with_changed_settings`).

    Raises
    ------
    SubmitError
        If a row cannot run, or the settings are refused; nothing is
        submitted.
    ValueError
        If a blank is named as one of :data:`RESULT`.
    """
    if taken := sorted(set(batch.parameter_columns) & set(RESULT)):
        raise ValueError(f'a table cannot hold the blank {", ".join(taken)}')
    client = batch.client
    which: list[Which] = ['remaining'] if remaining else ['all']
    if remaining and settings_changed:
        which.append('settings changed')
    try:
        # Records that a workspace went on with after a restart finish first.
        found = batch.state(keys, compare=False).values()
        client.wait([s.record for s in found if s.record])
        states = batch.state(keys)
        left = {k for w in which for k in to_reduce(states, w)}
        # A row that cannot run is given too, so that reduce refuses it by key.
        picked = [k for k, s in states.items() if k in left or s.problems]
        client.wait(batch.reduce(picked))
    except KeyboardInterrupt:
        batch.cancel()
        raise
    finally:
        batch.close()
    return with_results(batch, batch.state(keys), keys)


def with_results(
    batch: Batch,
    states: Mapping[str, RowState],
    keys: Sequence[str] | None = None,
) -> pd.DataFrame:
    """
    The rows of ``keys`` of the table of ``batch``, or all, with the columns
    of :data:`RESULT`, replaced if it has them: what ``states`` say of the
    row of each key. ``status`` is the row's status in the words of the page
    (:attr:`~.state.RowState.status`), ``record`` the ID of its result,
    ``failure`` why that failed, and ``published`` its catalogue identifier
    (:func:`~.definition.published`); each text is empty where there is
    nothing to say.
    """
    client = batch.client
    kept = batch.definitions
    published = {} if kept is None else definition.published(kept, batch.label)
    frame = batch.table()
    if keys is not None:
        frame = frame.loc[list(keys)]
    keys = list(frame.index)
    records = [states[key].record for key in keys]
    found = [r for r in records if r is not None]
    failure = dict(zip((r.id for r in found), client.failure(found), strict=True))
    return frame.drop(columns=list(RESULT), errors='ignore').assign(
        status=[states[key].status for key in keys],
        record=['' if r is None else r.id for r in records],
        failure=['' if r is None else failure[r.id] or '' for r in records],
        published=['' if r is None else published.get(r.id, '') for r in records],
    )

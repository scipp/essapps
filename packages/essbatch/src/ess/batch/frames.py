# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch run as a command: reduce its rows, wait, and return its batch table
with what came of each row::

    reduce_batch(batch)
    #        sample     name   status  record   failure       published
    # key
    # 60339  run:60339  water  done    3f2a...                pid:20.500/...
    # 60340  run:60340  sds    failed  81c0...  refused: ...

A table file written from the result reads back as the batch table:
:func:`~.table.read_table` drops the columns of :data:`~.table.RESULT`.
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
    Returns ``table`` with what came of each row, after reducing the rows
    with the settings of ``template`` and waiting.

    The spec is the one of the template's name that the workspace binds
    (:meth:`~ess.apps.Client.spec_named`). The batch has no definitions, so no
    manager lists it.

    Parameters
    ----------
    template:
        The settings, as :func:`~.templates.checked_settings` reads them.
    table:
        The batch table, as :meth:`~.reduction.Batch.set_table` accepts it.
    label:
        The label of the records. Give each batch its own: a batch is found
        again by its label.
    staged:
        Run the rows through a stage.
    persist:
        As in :meth:`~ess.apps.Client.submit`.
    remaining, settings_changed:
        As in :func:`reduce_batch`.

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
    Returns the rows of ``keys``, or all rows, with what came of each
    (:func:`with_results`), after reducing them and waiting.

    ``batch`` is closed afterwards.

    Parameters
    ----------
    remaining:
        Reduce only the rows that :func:`~.state.left_to_reduce` returns. It
        leaves the rows with status ``settings changed``.
    settings_changed:
        With ``remaining``, also reduce the rows with status ``settings
        changed`` (:func:`~.state.with_changed_settings`).

    Raises
    ------
    SubmitError
        If a row cannot run or the settings are invalid. Nothing is submitted.
    ValueError
        If a parameter column has the name of a column of :data:`RESULT`.
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
        # A row that cannot run is passed too, so that reduce raises for it by
        # key.
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
    The rows of ``keys`` of the batch table, or all rows, with the columns of
    :data:`RESULT` set from ``states``.

    ``status`` is :attr:`~.state.RowState.status`, ``record`` the ID of the
    row's result, ``failure`` why that result failed, and ``published`` its
    catalogue identifier. Each is empty text where there is no value.
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

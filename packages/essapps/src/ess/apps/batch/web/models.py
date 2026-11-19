# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the API sends and takes, as pydantic models, so that ``/docs`` shows them.

This module does not depend on HTTP: it imports neither ``fastapi`` nor
``starlette``.

A page reads :class:`State` and sends the requests at the end. A form is a
list of :class:`Node`; a cell of the table, of a sample or of a run, is a
:class:`Cell`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from ..reduction import Words
from ..table import Index


class Way(BaseModel):
    value: str
    label: str


class Node(BaseModel):
    """
    One field of a form, with its children. ``path`` is where an edit and an
    error point: the names of fields and the indices of elements, the same as
    the ``loc`` of a :class:`~ess.apps.FieldError`. A choice and the field of
    the way chosen have the same path. Which keys matter depends on ``kind``:

    - ``input``: ``input`` (``float``, ``int``, ``text``, ``time``, ``pick``),
      ``value`` (the label of the option for a ``pick``), ``options``;
    - ``scalars``: ``elements``, inputs;
    - ``group``: ``children``;
    - ``choice``: ``ways``, ``chosen``, whether to show the ``selector``, and the
      field of the way chosen as ``child``;
    - ``rows``: ``columns`` of text, shown but not edited;
    - ``nothing``: none.
    """

    kind: Literal['input', 'scalars', 'group', 'choice', 'nothing', 'rows']
    path: list[str | int]
    label: str
    problem: str | None = None
    description: str | None = None
    input: Literal['float', 'int', 'text', 'time', 'pick'] | None = None
    value: Any = None
    options: list[str] = []
    elements: list[Node] = []
    children: list[Node] = []
    ways: list[Way] = []
    chosen: str | None = None
    selector: bool = False
    child: Node | None = None
    columns: Any = None


class Edit(BaseModel):
    """
    What a person did to a form at ``path``: ``set`` an input to ``value`` (the
    label of an option for a picker), ``choose`` the way ``value`` of a choice,
    or ``add`` or ``remove`` the last element of a list.
    """

    path: list[str | int]
    op: Literal['set', 'choose', 'add', 'remove']
    value: Any = None


class Editor(BaseModel):
    """How a cell is edited: a list of ``options``, a number, or text."""

    type: Literal['select', 'number', 'text']
    options: list[str] = []


class Column(BaseModel):
    """A column of a table; ``default`` is the text an empty cell stands for."""

    name: str
    title: str
    editor: Editor | None = None
    default: str = ''


class Cell(BaseModel):
    """
    One cell: ``value`` is where its editor starts, ``text`` what it shows.
    ``edited`` if set by hand, with ``scanned`` the text it had before.
    ``problem`` is why the row cannot be reduced, if it is this cell.
    """

    value: Any = None
    text: str = ''
    edited: bool = False
    scanned: str = ''
    problem: str | None = None


class Row(BaseModel):
    """
    One row. ``result`` says whether it has a result to show, ``current`` or
    ``outdated``. ``earlier`` if it was reduced with earlier settings, which
    differ from the settings now at settings only; ``added`` if the row was
    added by hand. ``strays`` names the edits at a name that is not a column,
    such as one kept for an older version of the application, which make the
    row incomplete until they are taken back.
    """

    key: str
    name: str
    info: dict[str, str]
    status: str
    reason: str
    added: bool
    earlier: bool = False
    record: str | None
    result: Literal['current', 'outdated'] | None
    cells: dict[str, Cell]
    strays: list[str] = []


class Action(BaseModel):
    """
    What the primary button does, ``label``, or why it cannot, ``blocked``;
    how many rows each way of reducing takes: ``remaining``, ``again`` (all),
    and ``earlier``, those reduced with earlier settings, which "Reduce them
    again" in the Settings tab reduces; and while rows run, how many of the
    rows submitted with them are finished, as ``[finished, all]``.
    """

    label: str
    blocked: str | None
    remaining: int
    again: int
    earlier: int
    running: int
    progress: tuple[int, int] | None


class Refused(BaseModel):
    """
    A reason a reduction was refused that a cell or a setting shows, kept until
    its value changes; ``text`` says it whole.
    """

    entry: str | None
    loc: list[str | int]
    message: str
    text: str


class Following(BaseModel):
    """
    Whether the batch of the page follows the files, ``on``: a step every
    ``interval`` seconds scans them and reduces the rows that are new: ready
    and without a result. ``other`` is the title of the
    batch that follows, if another one does. ``failure`` says why the last
    step raised, while this batch follows.
    """

    on: bool
    other: str | None
    interval: float
    failure: str | None


class HeaderShown(BaseModel):
    """
    The header of the batch of a page, as the tab "Batch" shows it, but its
    title and columns, which :class:`State` has: the title of its
    ``workflow``, the name of its ``source``, ``None`` for rows added by hand
    only, and what the source calls the index of a row; its description; and
    its scope, from ``start`` up to ``stop`` (``None`` an open end).
    """

    workflow: str
    source: str | None
    index_title: str
    description: str
    start: Index | None
    stop: Index | None


class State(BaseModel):
    """
    Everything the page of a batch shows but the details of one row, the plot,
    and the runs. ``label`` names the batch, ``title`` is its title, and
    ``header`` the rest of its header; ``application`` is the title of the
    application. ``start`` changes when
    the server starts again, ``version`` with each change, and ``runs`` with
    each change of the runs. ``batches`` lists the batches of the workspace,
    as :class:`Batches` has them. ``settings`` is the form
    of the settings. ``settings_changed`` says whether a person changed it and
    has not applied the change; the rows read as with the batch's settings.
    ``settings_problem`` names the batch's settings that the form cannot show,
    what the form misses, and why the batch's settings are refused where the
    form shows them. ``follow`` is ``None`` if the batch has no source, so
    cannot follow the files. ``notice`` says what the action that answers
    with this state did.
    """

    version: int
    start: str
    runs: int | None
    batches: list[BatchRow]
    label: str
    title: str
    header: HeaderShown
    application: str
    words: Words
    folder: str
    info: list[Column]
    columns: list[Column]
    rows: list[Row]
    counts: dict[str, int]
    removed: int
    action: Action
    settings: list[Node]
    settings_problem: str | None
    settings_changed: bool
    refused: list[Refused]
    follow: Following | None = None
    notice: str = ''


class RowDetail(BaseModel):
    """
    What one row's result was reduced with, ``reduced_with``, and the names of
    the settings that differ from those now.
    """

    key: str
    reduced_with: list[Node] | None
    created: datetime | None
    differs: list[str]


class RunRow(BaseModel):
    key: str
    file: str
    cells: dict[str, Cell]


class Runs(BaseModel):
    version: int
    columns: list[Column]
    rows: list[RunRow]


class BatchRow(BaseModel):
    """
    A batch of the workspace: its label, title and description, what its
    rows are called, its scope as text, such as ``Run 60339 \N{EN DASH}``,
    empty if it holds every row, and whether it is the one that follows.
    """

    label: str
    title: str
    description: str
    words: Words
    scope: str
    follows: bool


class Batches(BaseModel):
    """
    The batches; and for the page without a batch, the title of the
    application, and the version of the runs, as :class:`State` has them.
    """

    application: str
    runs: int | None
    rows: list[BatchRow]


class Param(BaseModel):
    """A parameter of a spec, by name, with its title."""

    name: str
    title: str


class SpecChoice(BaseModel):
    """
    A spec that a new batch can be of, by name, with its title, and its
    parameters, those that the source fills first.
    """

    name: str
    title: str
    params: list[Param]


class SourceChoice(BaseModel):
    """
    A source that a new batch can be made with, by name, ``None`` for rows
    added by hand only; the parameters that it fills, which are columns of
    every batch of it; what it calls a row, and the index of a row; and the
    specs that fit it, those that have a parameter for each that it fills.
    """

    name: str | None
    fills: list[str]
    words: Words
    index_title: str
    specs: list[SpecChoice]


class BatchCounts(BaseModel):
    """
    The rows of each batch by status, in the words of the page, by label; why
    each other batch could not be counted, by label; and the rows of each
    registered source that no batch shows, in its words, such as
    ``3 samples``, by its name. A source of a batch that could not be counted
    is left out of ``outside``.
    """

    rows: dict[str, dict[str, int]]
    failed: dict[str, str]
    outside: dict[str, str]


class Plot(BaseModel):
    """
    A Plotly figure of results, or ``None``, a note of what it shows, and the
    colour of the curve of each row drawn, by key.
    """

    figure: dict[str, Any] | None
    note: str
    colors: dict[str, str] = {}


# Requests


class Key(BaseModel):
    key: str


class Keys(BaseModel):
    keys: list[str] = []


class Cells(BaseModel):
    """The cells of ``column`` of the rows ``keys``."""

    keys: list[str]
    column: str


class CellEdit(Cells):
    """Set the cells of ``column`` of the rows ``keys`` to ``value``."""

    value: Any = None


class Reduce(BaseModel):
    """
    Reduce the rows remaining, those reduced with earlier settings, all that
    can be, or those of ``keys``.
    """

    which: Literal['remaining', 'earlier', 'all', 'selected'] = 'remaining'
    keys: list[str] = []


class On(BaseModel):
    on: bool


class Ends(BaseModel):
    """
    The ends of the scope of a batch as typed; the manager converts them to
    the type of the index of its source. ``None`` is an open end.
    """

    start: Index | None = None
    stop: Index | None = None


class NewBatch(Ends):
    """
    Make a batch, as :meth:`~ess.apps.batch.BatchManager.create` takes it:
    ``columns`` are all columns, and ``start`` and ``stop`` the ends of its
    scope.
    """

    title: str
    description: str = ''
    source: str | None = None
    spec: str
    columns: list[str]


class HeaderEdit(Ends):
    """
    The title, description and scope of the batch of a page, as a person
    saves them.
    """

    title: str
    description: str = ''


class Next(BaseModel):
    """Start the next batch at row ``key``, called ``title``."""

    key: str
    title: str


class Made(BaseModel):
    """The label of the batch made."""

    label: str


class Notice(BaseModel):
    """What an action of the workspace did, in words."""

    notice: str

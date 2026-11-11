# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the API sends and takes, as pydantic models, so that ``/docs`` shows them.

A page reads :class:`State` and sends the requests at the end. A form is a
list of :class:`Node`; a cell of the table, of a sample or of a run, is a
:class:`Cell`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


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
    ``outdated``. ``alone`` if that result was reduced with its own parameters;
    ``added`` if the row was added by hand.
    """

    key: str
    name: str
    info: dict[str, str]
    status: str
    reason: str
    added: bool
    alone: bool
    record: str | None
    result: Literal['current', 'outdated'] | None
    cells: dict[str, Cell]


class Action(BaseModel):
    """
    What the primary button does, ``label``, or why it cannot, ``blocked``;
    how many rows each way of reducing takes; and while rows run, how many of
    the rows submitted with them are finished, as ``[finished, all]``.
    """

    label: str
    blocked: str | None
    remaining: int
    again: int
    running: int
    progress: tuple[int, int] | None


class Refused(BaseModel):
    """
    A reason a reduction was refused, kept until the next change. ``placed`` if
    a cell or a setting shows it; ``text`` says it whole.
    """

    entry: str | None
    loc: list[str | int]
    message: str
    text: str
    placed: bool


class Words(BaseModel):
    """What a row is called, as ``1 sample``, ``2 samples``."""

    one: str = 'row'
    many: str = 'rows'

    def count(self, n: int) -> str:
        return f'{n} {self.one if n == 1 else self.many}'


class State(BaseModel):
    """
    Everything the page shows but the details of one row, the plot, and the
    runs. ``start`` changes when the server starts again, ``version`` with each
    change, and ``runs`` with each change of the runs. ``notice`` says what
    the action that answers with this state did. ``loaded`` is the name of
    the table file whose rows stand in place of a scan, until the next scan.
    """

    version: int
    start: str
    runs: int | None
    title: str
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
    refused: list[Refused]
    loaded: str | None = None
    notice: str = ''


class RowDetail(BaseModel):
    """
    What one row's result was reduced with, ``reduced_with``, the names of the
    settings that differ from those now, and the form of a reduction of it alone.
    """

    key: str
    reduced_with: list[Node] | None
    created: datetime | None
    differs: list[str]
    tweak: list[Node]


class RunRow(BaseModel):
    key: str
    file: str
    cells: dict[str, Cell]


class Runs(BaseModel):
    version: int
    columns: list[Column]
    rows: list[RunRow]


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
    """Reduce the rows remaining, all that can be, or those of ``keys``."""

    which: Literal['remaining', 'all', 'selected'] = 'remaining'
    keys: list[str] = []


class RowEdit(Edit):
    """An edit of the form of the reduction of row ``key`` alone."""

    key: str

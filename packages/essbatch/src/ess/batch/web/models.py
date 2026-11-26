# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the API sends and takes, as pydantic models, so that ``/docs`` shows them.

This module does not depend on HTTP: it imports neither ``fastapi`` nor
``starlette``.

A page reads :class:`State` and sends the requests at the end. A form is a
list of :class:`Node`; a cell of the batch table is a :class:`Cell`, and
one of a run a :class:`RunCell`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, model_validator

from ..definition import Form


class Alternative(BaseModel):
    value: str
    label: str


class Node(BaseModel):
    """
    One field of a form, with its children. ``path`` is where an edit and an
    error point: the names of fields and the indices of elements, the same as
    the ``loc`` of a :class:`~ess.apps.FieldError`. A choice and the field of
    the alternative chosen have the same path. Which keys matter depends on ``kind``:

    - ``input``: ``input`` (``float``, ``int``, ``text``, ``time``, ``pick``),
      ``value`` (the label of the option for a ``pick``), ``options``;
    - ``scalars``: ``elements``, inputs;
    - ``group``: ``children``;
    - ``choice``: ``alternatives``, ``chosen``, whether to show the
      ``selector``, and the field of the alternative chosen as ``child``;
    - ``rows``: ``columns`` of text, shown but not edited;
    - ``nothing``: none.

    ``column`` marks a parameter of a form of settings that is a column of
    the batch: its value is the setting of each row whose cell is empty.
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
    alternatives: list[Alternative] = []
    chosen: str | None = None
    selector: bool = False
    child: Node | None = None
    columns: Any = None
    column: bool = False


class Edit(BaseModel):
    """
    What a person did to a form at ``path``: ``set`` an input to ``value`` (the
    label of an option for a picker), ``choose`` the alternative ``value`` of a choice,
    ``add`` or ``remove`` the last element of a list, or ``reset`` a whole
    parameter to its default, or unset if it has none.
    """

    path: list[str | int]
    op: Literal['set', 'choose', 'add', 'remove', 'reset']
    value: Any = None


class Editor(BaseModel):
    """How a cell is edited: a list of ``options``, a number, or text."""

    type: Literal['select', 'number', 'text']
    options: list[str] = []


class Column(BaseModel):
    """
    A column of a table; ``default`` is the text of the value that an empty
    cell takes: the setting of the batch, else the default of the parameter.
    ``waits`` says that the source of the batch fills it: an empty cell
    there means that the row waits for its file, and "Make this a setting"
    is not offered.
    """

    name: str
    title: str
    editor: Editor | None = None
    default: str = ''
    waits: bool = False


class Cell(BaseModel):
    """
    One cell: ``value`` is where its editor starts, ``text`` what it shows.
    ``problem`` is why the row cannot be reduced, if it is this cell.
    """

    value: Any = None
    text: str = ''
    problem: str | None = None


class RunCell(Cell):
    """
    A cell of a run: ``edited`` if a person corrected it, with ``original``
    the text that the file gives.
    """

    edited: bool = False
    original: str = ''


class Row(BaseModel):
    """
    One row: its key, its ``name`` (its note ``name``, else its key), the
    text of each of its other notes by column, and a :class:`Cell` per
    parameter column. ``result`` says whether it has a result to show,
    ``current`` or ``outdated``. ``changed`` names, for a row that reads
    ``settings changed``, the settings that changed since its result, such as
    ``'Binning changed'``. ``published`` is the catalogue identifier of its
    result, if published, and ``publishable`` says whether its result may be
    published and is not (:func:`~ess.batch.state.publishable`).
    """

    key: str
    name: str
    notes: dict[str, str]
    status: str
    reason: str
    changed: str = ''
    record: str | None
    result: Literal['current', 'outdated'] | None
    cells: dict[str, Cell]
    published: str = ''
    publishable: bool = False


class Action(BaseModel):
    """
    What the primary button does, ``label``, or why it cannot, ``blocked``;
    how many rows each way of reducing takes: ``remaining`` and ``again``
    (all), and while rows run, how many of the rows submitted with them are
    finished, as ``[finished, all]``. The rows reduced with earlier settings,
    which "Reduce rows of earlier settings" reduces, are the count
    ``settings changed`` of :attr:`State.counts`.
    """

    label: str
    blocked: str | None
    remaining: int
    again: int
    running: int
    progress: tuple[int, int] | None


class SubmitProblem(BaseModel):
    """
    A reason a reduction was refused that a cell or a setting shows, kept until
    its value changes; ``text`` says it whole.
    """

    request_key: str | None
    loc: list[str | int]
    message: str
    text: str


class Following(BaseModel):
    """
    Whether the batch of the page follows its source, ``on``: a step every
    ``interval`` seconds adds the rows of the source that are new and filled,
    and reduces the rows not reduced yet: ready and without a result.
    ``source`` names the source that the batch was made from, ``None`` if it
    was made without one, so that it offers no rows of a source.
    While this batch follows, ``failure`` says why the last step raised, and
    ``waiting`` counts the rows of the source that the last step did not add
    since they are not filled yet.
    """

    on: bool
    source: str | None
    interval: float
    failure: str | None
    waiting: int = 0


class HeaderShown(BaseModel):
    """
    The batch of a page, as the tab "Batch" shows it, but its title and
    columns, which :class:`State` has: the title of its ``workflow``, the
    name of its ``spec``, and its description.
    """

    workflow: str
    spec: str
    description: str


class Output(BaseModel):
    """An output of the spec of a batch: its name and its title."""

    name: str
    title: str


class Format(BaseModel):
    """
    A format that the form may pick: its ``name``, which the form keeps, its
    ``title`` and ``description``, shown to a person, and the ``outputs`` that
    its writer reads, or ``None`` if it writes the outputs chosen in the form.
    """

    name: str
    title: str
    description: str
    outputs: list[str] | None


class OutputShown(BaseModel):
    """
    The tab "Output": the ``form`` of the batch, the ``formats`` it may pick,
    the default first, the ``outputs`` of its spec that the form may name,
    whether a ``catalogue`` is given, to publish to, and the ``problem`` of
    the form in words: why nothing is written by it, such as a kept format
    that is not among ``formats``; empty if none. ``unpublishable`` says
    what the form lacks to publish (:attr:`~ess.batch.definition.Form.lacking`);
    empty if nothing.
    """

    form: Form
    formats: list[Format]
    outputs: list[Output]
    catalogue: bool
    problem: str
    unpublishable: str


class State(BaseModel):
    """
    Everything the page of a batch shows but the details of one row, the plot,
    and the runs. ``label`` names the batch, ``title`` is its title, and
    ``header`` the rest of its header; ``application`` is the title of the
    application. ``start`` changes when
    the server starts again, ``version`` with each change, and ``runs`` with
    each change of the runs. ``batches`` lists the batches of the workspace,
    as :class:`Batches` has them. ``notes`` are the note columns of the batch
    table, but ``name``, and ``columns`` the parameter columns; both change
    when the batch table does. ``settings`` is the form
    of the settings. ``settings_unapplied`` says whether a person changed it and
    has not applied the change; the rows read as with the batch's settings.
    ``settings_problem`` names the batch's settings that the form cannot show,
    what the form misses, and why the batch's settings are refused where the
    form shows them; it has a field per parameter, a column's for its setting.
    ``may_be_columns`` names the parameters that "Make this a column" offers:
    those that are no column and whose cell the page edits.
    ``output`` is the tab "Output".
    ``files_problem`` says why the files cannot be listed,
    if the file source raises: the pickers then offer none. ``notice`` says
    what the action that answers with this state did.
    """

    version: int
    start: str
    runs: int | None
    batches: list[BatchRow]
    label: str
    title: str
    header: HeaderShown
    application: str
    folder: str
    notes: list[Column]
    columns: list[Column]
    rows: list[Row]
    counts: dict[str, int]
    action: Action
    settings: list[Node]
    settings_problem: str | None
    settings_unapplied: bool
    may_be_columns: list[str]
    submit_errors: list[SubmitProblem]
    follow: Following
    files_problem: str | None
    output: OutputShown
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
    cells: dict[str, RunCell]


class Runs(BaseModel):
    """
    The runs, with their columns; none if the file source raises, and
    ``problem`` says why.
    """

    version: int
    columns: list[Column]
    rows: list[RunRow]
    problem: str | None = None


class BatchRow(BaseModel):
    """
    A batch of the workspace: its label, title and description, and whether
    it is the one that follows.
    """

    label: str
    title: str
    description: str
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
    A source that a new batch can be made with, by name, ``None`` for a batch
    without rows; the parameters that it fills, which are columns of every
    batch made from it; and the specs that fit it, those that have a
    parameter for each that it fills.
    """

    name: str | None
    fills: list[str]
    specs: list[SpecChoice]


class SourceRow(BaseModel):
    """
    One of the rows of a source: its key, its ``name`` (as
    :class:`Row` has it), the text of each note, and its ``mark``:
    ``waits`` if a cell that the source fills is empty; for a range of a new
    batch, ``taken`` or ``left out``; else empty.
    """

    key: str
    name: str
    cells: dict[str, str]
    mark: Literal['waits', 'taken', 'left out', '']


class SourceRows(BaseModel):
    """The rows of a source, with the columns of ``cells``."""

    columns: list[Column]
    rows: list[SourceRow]


class BatchCounts(BaseModel):
    """
    The rows of each batch by status, in the words of the page, and the
    identities of the files that it names, sorted, each by label; and why
    each other batch could not be counted, by label.
    """

    rows: dict[str, dict[str, int]]
    files: dict[str, list[str]]
    failed: dict[str, str]


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

    which: Literal['remaining', 'settings changed', 'all', 'selected'] = 'remaining'
    keys: list[str] = []


class NewBatch(BaseModel):
    """
    Make a batch of ``spec``: with a ``source``, of the rows that the source
    has filled now from the row of key ``first`` to that of ``last``, or of
    none of them if ``later``, so that it gets only the rows added later
    (:class:`~ess.batch.Range`); without, of no rows, or of those of a
    table file. ``parameter_columns`` are the parameter columns, those that
    the source fills among them. The batch starts with the
    settings of the batch of label ``settings_from`` if given, else with
    those that the application gives ``spec``; a setting at a column stays,
    the value of each row whose cell is empty, but at a column that the
    source fills. With ``follow``, the batch follows its source from then
    on, in place of the batch that followed before; the
    :class:`~ess.batch.Intake`
    refuses it with a ``last``. A ``follow`` without a ``source`` is refused
    here, since no call of the batch layer can express it.
    """

    title: str
    description: str = ''
    source: str | None = None
    first: str | None = None
    last: str | None = None
    spec: str
    parameter_columns: list[str] = []
    settings_from: str | None = None
    later: bool = False
    follow: bool = False

    @model_validator(mode='after')
    def _follows_a_source(self) -> Self:
        if self.follow and self.source is None:
            raise ValueError('a batch follows its source, and none is given')
        return self


class Value(BaseModel):
    """A value of a column, as ``text``, and the ``key`` of a row that has it."""

    key: str
    text: str


class MakeColumn(BaseModel):
    """Make the parameter ``name`` a column, with empty cells."""

    name: str


class MakeSetting(BaseModel):
    """
    Make the column ``name`` a setting, of the value that the row of ``key``
    takes, or that every row takes if not given.
    """

    name: str
    key: str | None = None


class HeaderEdit(BaseModel):
    """The title and description of the batch of a page, as a person saves them."""

    title: str
    description: str = ''


class Made(BaseModel):
    """The label of the batch made."""

    label: str


class Notice(BaseModel):
    """What an action of the workspace did, in words."""

    notice: str

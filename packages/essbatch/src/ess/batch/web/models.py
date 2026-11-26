# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The JSON that the server and the page exchange, as pydantic models, so that
``/docs`` shows them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, model_validator

from ..definition import OutputOptions


class Alternative(BaseModel):
    value: str
    label: str


class Node(BaseModel):
    """
    One field of a form, with its children.

    ``path`` is the ``loc`` of a :class:`~ess.apps.FieldError` at this field.
    A choice and its ``child``, the field of the chosen alternative, have the
    same path. The ``value`` of a ``pick`` is the label of its option.
    ``column`` is true for a setting whose parameter is a column. The setting
    applies to each row whose cell is empty.
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
    An edit of a form at ``path``.

    - ``set``: set an input to ``value``, the label of an option for a picker
    - ``choose``: choose the alternative ``value`` of a choice
    - ``add``, ``remove``: add or remove the last element of a list
    - ``reset``: reset a whole parameter to its default, or unset it if it has
      no default
    """

    path: list[str | int]
    op: Literal['set', 'choose', 'add', 'remove', 'reset']
    value: Any = None


class Editor(BaseModel):
    """How the page edits a cell; ``options`` belong to a ``select``."""

    type: Literal['select', 'number', 'text']
    options: list[str] = []


class Column(BaseModel):
    """
    A column of a table.

    ``default`` is the text of the value of an empty cell: the batch's
    setting, else the parameter's default. ``from_source`` is true for a
    column that the row source fills. There an empty cell means that the row
    waits for its file, and the page does not offer "Make this a setting".
    """

    name: str
    title: str
    editor: Editor | None = None
    default: str = ''
    from_source: bool = False


class Cell(BaseModel):
    """
    One cell. ``value`` is where its editor starts, ``text`` is what it shows.
    ``problem`` is why the row cannot be reduced, if the reason is at this cell.
    """

    value: Any = None
    text: str = ''
    problem: str | None = None


class RunCell(Cell):
    """
    A cell of a run. ``edited`` is true if a person corrected it, ``original``
    is the text of the value in the file.
    """

    edited: bool = False
    original: str = ''


class Row(BaseModel):
    """
    One row of the batch table.

    ``name`` is the row's note ``name``, else its key. ``notes`` holds the
    text of each other note, ``cells`` a cell per parameter column.
    For a row with status ``settings changed``, ``changed`` lists the settings
    changed since its result, such as ``'Binning changed'``. ``published`` is
    the catalogue identifier of its published result. ``publishable`` is true
    if its result may be published and is not yet
    (:func:`~ess.batch.state.publishable`).
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
    The primary button: its ``label``, and ``blocked``, why it is disabled.

    ``remaining`` and ``again`` count the rows that a :class:`Reduce` of
    ``'remaining'`` and of ``'all'`` reduces. For ``'settings changed'``, the
    count is ``State.counts['settings changed']``. ``progress`` is
    ``[finished, total]`` of the rows submitted since the earliest one still
    running, ``None`` if none is running.
    """

    label: str
    blocked: str | None
    remaining: int
    again: int
    running: int
    progress: tuple[int, int] | None


class SubmitProblem(BaseModel):
    """
    Why a reduction was rejected, at a cell or a setting.

    The page shows it until that value changes or a reduction succeeds. Apply
    drops those at a setting. ``message`` is the reason alone. ``text`` adds
    the name of the row and the title of the field.
    """

    request_key: str | None
    loc: list[str | int]
    message: str
    text: str


class Following(BaseModel):
    """
    Whether the page's batch follows its row source, with a step every
    ``interval`` seconds.

    ``row_source`` is ``None`` for a batch made without a row source. The
    page then hides "Add rows". While the batch follows, ``failure`` is why
    the last step raised. ``incomplete`` counts the rows of the source that the
    last step left out as incomplete.
    """

    on: bool
    row_source: str | None
    interval: float
    failure: str | None
    incomplete: int = 0


class HeaderShown(BaseModel):
    """The header of the tab "Batch", except ``State.title``."""

    workflow: str
    spec: str
    description: str


class Output(BaseModel):
    name: str
    title: str


class Format(BaseModel):
    """
    A format offered in the tab "Output". ``outputs`` are the outputs that its
    writer reads, ``None`` if it writes the outputs chosen in the form.
    """

    name: str
    title: str
    description: str
    outputs: list[str] | None


class OutputShown(BaseModel):
    """
    The tab "Output". ``catalogue`` is true if the page can publish.

    ``problem`` is why the ``options`` write nothing: their format is not
    among ``formats``. ``unpublishable`` is what the options lack to
    publish (:attr:`~ess.batch.definition.OutputOptions.lacking`).
    """

    options: OutputOptions
    formats: list[Format]
    outputs: list[Output]
    catalogue: bool
    problem: str
    unpublishable: str


class State(BaseModel):
    """
    What the page of a batch shows, except a row's details, the plot and the runs.

    ``start`` changes when the server restarts, ``revision`` with each change.
    ``runs`` is the revision of the runs, ``None`` if the page has no Runs
    view. ``notes`` are the note columns except ``name``.
    ``settings_unapplied`` is true while the form holds a change that is not
    applied. ``settings_problem`` lists the settings that the form cannot
    show, what the form misses, and why the batch rejects its settings.
    ``may_be_columns`` are the parameters without a column whose cell the
    page can edit. ``notice`` is what the request that returns this state did.
    """

    revision: int
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
    The parameters that a row's result was reduced with, and ``differs``, the
    names of those that differ from what the row would be reduced with now.
    ``reduced_with`` is ``None`` if the row has no result, or one of another
    version of the workflow.
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
    The runs. If the file source raises, ``rows`` is empty and ``problem`` is
    the reason.
    """

    revision: int
    columns: list[Column]
    rows: list[RunRow]
    problem: str | None = None


class BatchRow(BaseModel):
    """A batch of the workspace. ``follows`` is true for the batch that follows."""

    label: str
    title: str
    description: str
    follows: bool


class Batches(BaseModel):
    """
    The batches of the workspace, with ``application`` and ``runs`` as in
    :class:`State`, for the page without a batch.
    """

    application: str
    runs: int | None
    rows: list[BatchRow]


class Param(BaseModel):
    name: str
    title: str


class SpecChoice(BaseModel):
    """
    A spec offered for a new batch. ``params`` are those that may be columns:
    the ones that the row source fills first.
    """

    name: str
    title: str
    params: list[Param]


class SourceChoice(BaseModel):
    """
    A row source offered for a new batch, ``name=None`` for none.
    ``source_columns`` are the parameters that it fills. ``specs`` are the
    specs that have each of these parameters.
    """

    name: str | None
    source_columns: list[str]
    specs: list[SpecChoice]


class SourceRow(BaseModel):
    """
    A row of a row source, as a dialog lists it. ``cells`` holds the text of
    each note. ``mark`` is ``incomplete`` if a cell that the source fills is
    empty. For the range of a new batch, it is ``taken`` or ``left out``.
    """

    key: str
    name: str
    cells: dict[str, str]
    mark: Literal['incomplete', 'taken', 'left out', '']


class SourceRows(BaseModel):
    columns: list[Column]
    rows: list[SourceRow]


class BatchCounts(BaseModel):
    """
    By batch label: ``rows`` counts the rows by status, ``files`` lists the
    files that the batch refers to, sorted, and ``failed`` is why a batch
    could not be counted.
    """

    rows: dict[str, dict[str, int]]
    files: dict[str, list[str]]
    failed: dict[str, str]


class Plot(BaseModel):
    """
    A Plotly figure of results. ``note`` describes what it shows, and
    ``colors`` maps the key of each row drawn to the colour of its curve.
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
    keys: list[str]
    column: str


class CellEdit(Cells):
    """
    Sets the cells of ``column`` of the rows ``keys`` to ``value``, which is
    the label of the option for a ``select``.
    """

    value: Any = None


class Reduce(BaseModel):
    """
    Which rows to reduce (:func:`~ess.batch.state.to_reduce`), or ``keys``
    for ``'selected'``.
    """

    which: Literal['remaining', 'settings changed', 'all', 'selected'] = 'remaining'
    keys: list[str] = []


class NewBatch(BaseModel):
    """
    A request for a new batch of ``spec``.

    With a ``source``, ``first``, ``last`` and ``later`` choose its rows
    among the complete rows of the source (:class:`~ess.batch.Range`).
    The columns that the source fills are columns whether or not
    ``parameter_columns`` lists them.
    The batch starts with the settings of the batch ``settings_from``, else
    with the application's settings for ``spec``. With ``follow``, the new
    batch follows its source in place of the batch that followed before.
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
    """A value of a column, as ``text``, and the ``key`` of a row with it."""

    key: str
    text: str


class MakeColumn(BaseModel):
    """Makes the parameter ``name`` a column with empty cells."""

    name: str


class MakeSetting(BaseModel):
    """
    Makes the column ``name`` a setting with the value of row ``key``, or
    with the value that every row has if ``key`` is ``None``.
    """

    name: str
    key: str | None = None


class HeaderEdit(BaseModel):
    title: str
    description: str = ''


class Made(BaseModel):
    """The label of the new batch."""

    label: str


class Notice(BaseModel):
    """What an action of the workspace did, in words for a person."""

    notice: str

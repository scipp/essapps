"""
The outputs of the client's records, by type: what a scientist picks, to
plot it, to see where it came from, or to remove it.

Each output is to be a group, so that its type says what it is,
``BeamCentre``, where an array's type says only ``DataArray``. An output is
listed by its type and the time its record was created,
``BeamCentre (11:45)``, as the pickers of a form offer it (:class:`Listing`).

What an output is comes from the record's spec: the output's description,
else the first paragraph of the docstring of its type. What its value is
comes from :meth:`~ess.apps.Client.describe`, once its record has completed:
the sizes, unit, and coordinates of each array, with no data read. An output
that another client released after it was listed raises ``NotReadable`` when
selected, which the detail shows as text. Where it came from is the
provenance graph (:class:`~ess.apps.panel.graph.Graph`), which shows the
records pending and where a failure started.

The output selected is shown with its plot, a plot a workflow package
configured for its spec, or its provenance graph, as picked; the pick stays as
other outputs are selected, unless the record's spec has no plot of that name.
The plot has a figure per array field of an output, drawn with plopp, once its
record has completed. A field of one or two dimensions is read with the output
and drawn whole. A field of more dimensions has a slider for each dimension but
the last two, and a cut of it is read with ``select=`` for the positions of the
sliders, so no more of the array is read than is drawn. A plot configured for a
spec (:class:`~ess.apps.plotting.Plot`) is a function of the record's outputs, every
one of them read, and of the values its request referenced that it declares: it
returns a plopp figure, or fails, which the view shows as text.

Removing an output releases its record (``client.release``), so the record
stays in the proposal's history and in the graphs of the records that read it.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

import panel as pn
import plopp as pp
from plopp.core.typing import FigureLike as Figure
from pydantic.fields import FieldInfo

from ess.apps import ArrayInfo, Client, Listing, NotReadable, Record, SpecId, Status
from ess.apps.plotting import Plot, read_inputs, slider_dims
from ess.spec import field_types

from .graph import Graph
from .table import Table, with_sidebar

_PLOT = 'Plot'
_PROVENANCE = 'Provenance'

Plots = Mapping[SpecId, Mapping[str, Plot]]
"""Plots by the name of a view, for the records of a spec."""


class Outputs:
    """
    A sidebar with a table of the outputs of the client's records, grouped by
    type, newest first, and below it the output selected, with a button that
    removes it. Beside the sidebar are its plot, the plots configured for its
    spec, or its provenance graph, as ``views`` picks.

    An output is listed while a request of the client may reference it, so
    while its record is pending or completed, and when its record has failed,
    until removed. ``refresh`` is called once an output is removed; by
    default it lists the outputs again. ``plots`` holds, for the spec of a
    record, the plots by the name of the view that shows their figure.
    """

    def __init__(
        self,
        client: Client,
        listing: Listing,
        refresh: Callable[[], object] | None = None,
        plots: Plots | None = None,
    ) -> None:
        self._client = client
        self._refresh = refresh or (lambda: self.refresh(Listing(client)))
        self._plots = plots or {}
        for spec, named in self._plots.items():
            if taken := sorted(named.keys() & {_PLOT, _PROVENANCE}):
                raise ValueError(f'{spec}: {taken} are views of every record')
        self._removed: set[str] = set()
        self._fields: dict[str, Any] = {}  # of the output selected, if readable
        self._offering = False
        self.table = Table[tuple[str, str]](  # by record id and output name
            ['Type', 'Output', 'Status'],
            self._show,
            groupby=['Type'],
            hidden_columns=['Type'],
            widths={'Status': 90},
        )
        self.detail = pn.pane.Markdown('', sizing_mode='stretch_width')
        self.views = pn.widgets.RadioButtonGroup(options=[_PLOT, _PROVENANCE])
        self.views.param.watch(lambda _: self._pick(), 'value')
        self.remove_button = pn.widgets.Button(
            label='Remove',
            disabled=True,
            description="Drop the value of each of the record's outputs",
        )
        self.remove_button.on_click(lambda _: self.remove())
        self.graph = Graph(client)
        self.view = pn.Column(sizing_mode='stretch_width')
        self.refresh(listing)

    @property
    def selected(self) -> tuple[Record, str] | None:
        """The record and the name of the output selected, if one is."""
        if (selected := self.table.selected) is None:
            return None
        record, name = selected
        return self._records[record], name

    def select(self, record: Record, name: str) -> None:
        """Select the output ``name`` of ``record``."""
        self.table.select((record.id, name))

    def refresh(self, listing: Listing) -> None:
        """
        List the outputs again. The output selected stays so if listed, and
        is shown again, which draws its plots again, only if its status
        changed. A provenance graph is drawn again in any case.
        """
        self._listing = listing
        self._records = {r.id: r for r in listing.records}
        self.table.set(
            {
                (r.id, n): {
                    'Type': listing.type_name(r, n),
                    'Output': listing.output_label(r.ref(n)),
                    'Status': str(listing.status[r.id]),
                }
                for r in reversed(listing.records)
                if r.id not in self._removed
                for n in r.outputs
                if n in listing.readable[r.id] or listing.status[r.id] is Status.FAILED
            }
        )
        if self.views.value == _PROVENANCE:
            self.graph.refresh(listing)

    def _show(self) -> None:
        """Show the output selected, and its view; nothing if none is."""
        selected = self.selected
        self.remove_button.disabled = selected is None
        self._fields = {}
        self.detail.object = ''
        if selected is not None:
            self.detail.object = self._describe(*selected)
            self._offer_views(selected[0])
        self._show_view()

    def _describe(self, record: Record, name: str) -> str:
        """
        The text about an output; reads what it is once its record has
        completed, and keeps its fields to draw.
        """
        listing = self._listing
        info = listing.specs[record.id].outputs.model_fields[name]
        status = listing.status[record.id]
        lines = [f'### {listing.output_label(record.ref(name))}: {status}']
        if about := _about(info):
            lines.append(about)
        lines.append(f'**Record** {listing.labels[record.id]}')
        if status is Status.FAILED:
            lines.append(f'**Failure** {self._client.failure(record)}')
        elif status is Status.COMPLETED:
            try:
                self._fields = self._client.describe(record, name)
            except NotReadable as error:
                lines.append(f'**Value** not readable: {error}')
            else:
                lines.append(f'**Value**\n\n{_summary(self._fields)}')
        return '\n\n'.join(lines)

    def _offer_views(self, record: Record) -> None:
        """
        Offer the plots configured for the record's spec between the plot and
        the provenance graph; pick the plot if the pick is not offered.
        """
        options = [_PLOT, *self._plots.get(record.spec, {}), _PROVENANCE]
        value = self.views.value if self.views.value in options else _PLOT
        self._offering = True  # the caller shows the view
        try:
            self.views.param.update(options=options, value=value)
        finally:
            self._offering = False

    def _pick(self) -> None:
        if not self._offering:
            self._show_view()

    def _show_view(self) -> None:
        """
        The view picked of the output selected: the provenance graph, the plot
        configured, or its arrays drawn, once its record has completed and its
        value is readable.
        """
        self.view.objects = []
        if (selected := self.selected) is None:
            return
        record, name = selected
        if self.views.value == _PROVENANCE:
            self.graph.show(record, self._listing)
            self.view.objects = [self.graph.panel()]
        elif self.views.value != _PLOT:
            self.view.objects = [self._configured(record, self.views.value)]
        elif self._fields:
            self.view.objects = self._plot(record, name)

    def _configured(self, record: Record, view: str) -> Any:
        """A plot configured for the spec of ``record``; the failure as text."""
        if self._listing.status[record.id] is not Status.COMPLETED:
            return pn.pane.Markdown('')
        try:
            plot = self._plots[record.spec][view]
            model = self._listing.specs[record.id].outputs
            values = self._client.output(record)
            outputs = model.model_construct(
                **{n: values.get(n) for n in model.model_fields}
            )
            inputs = read_inputs(plot, self._client, record)
            return _figure_pane(plot.function(outputs, **inputs))
        except Exception as error:  # a plot is code of the workflow's author
            return pn.pane.Markdown(f'{view} failed: `{type(error).__name__}: {error}`')

    def _plot(self, record: Record, name: str) -> list[Any]:
        """
        A figure for each array field of the output; a field of more than two
        dimensions gets a slider for each but the last two.
        """
        label = self._listing.output_label(record.ref(name))
        group = self._listing.is_group(record, name)
        arrays = {
            (f'{label} {n}' if group else label): (n if group else None, info)
            for n, info in self._fields.items()
            if isinstance(info, ArrayInfo)
        }
        cuts = [
            _Cut(self._client, record, name, field, title, info.sizes)
            for title, (field, info) in arrays.items()
            if slider_dims(info.sizes)
        ]
        views: list[Any] = [cut.view for cut in cuts]
        plain = {t: f for t, (f, i) in arrays.items() if not slider_dims(i.sizes)}
        if plain:
            # The cut of a field is read with the rest of the output, whole.
            value = cuts[0].value if cuts else self._client.output(record, name)
            if value is not None:
                views = [_figure(t, _field(value, f)) for t, f in plain.items()] + views
        return views

    def remove(self) -> None:
        """Release the record of the output selected, and stop listing it."""
        if (selected := self.selected) is None:
            return
        record, _ = selected
        self._client.release(record)
        self._removed.add(record.id)
        self._refresh()

    def panel(self) -> Any:
        return with_sidebar(
            [self.table.widget, self.remove_button, self.detail],
            [self.views, self.view],
        )


class _Cut:
    """
    An array field of more than two dimensions: a slider for each dimension
    but the last two, and the plot of the cut they pick. Moving a slider reads
    the cut and draws it, once the slider is released.

    ``value`` is the output as last read.
    """

    def __init__(
        self,
        client: Client,
        record: Record,
        name: str,
        field: str | None,
        title: str,
        sizes: Mapping[str, int],
    ) -> None:
        self._client = client
        self._record = record
        self._name = name
        self._field = field
        self._title = title
        self.value: Any = None
        self.sliders = {
            dim: pn.widgets.IntSlider(label=dim, start=0, end=sizes[dim] - 1, value=0)
            for dim in slider_dims(sizes)
        }
        self.view = pn.Column(*self.sliders.values(), pn.pane.Markdown(''))
        for slider in self.sliders.values():
            slider.param.watch(lambda _: self.draw(), 'value_throttled')
        self.draw()

    def draw(self) -> None:
        """Read the cut at the positions of the sliders, and draw it."""
        select = {dim: slider.value for dim, slider in self.sliders.items()}
        where = ', '.join(f'{dim}={i}' for dim, i in select.items())
        try:
            self.value = self._client.output(self._record, self._name, select=select)
        except NotReadable as error:
            shown = pn.pane.Markdown(f'{self._title}: not readable: {error}')
        else:
            array = _field(self.value, self._field)
            shown = _figure(f'{self._title} ({where})', array)
        self.view.objects = [*self.sliders.values(), shown]


def _field(value: Any, field: str | None) -> Any:
    """The array ``field`` of a group, or the array itself if no field is named."""
    return value if field is None else getattr(value, field)


def _about(info: FieldInfo) -> str | None:
    """
    The description of an output, or the first paragraph of the docstring of
    its type if that is a group.
    """
    if info.description:
        return info.description
    tp = field_types.value_type(info.annotation)
    if field_types.is_group(tp) and tp.__doc__:
        return inspect.cleandoc(tp.__doc__).split('\n\n')[0]
    return None


def _summary(fields: Mapping[str, Any]) -> str:
    """The fields of an output, each in brief."""
    return '\n'.join(f'- {n}: {_brief(v)}' for n, v in fields.items())


def _brief(value: Any) -> str:
    """An array by its sizes, unit, and coordinates; any other value as it is."""
    if isinstance(value, ArrayInfo):
        unit = '' if value.unit is None else f' [{value.unit}]'
        sizes = ', '.join(f'{d}: {n}' for d, n in value.sizes.items())
        names = f', coordinates {", ".join(value.coords)}' if value.coords else ''
        return f'array ({sizes}){unit}{names}'
    if hasattr(value, 'dims'):  # 0-D
        unit = '' if getattr(value, 'unit', None) is None else f' [{value.unit}]'
        return f'{value.value}{unit}'
    return repr(value)


def _figure_pane(figure: Figure) -> Any:
    """A Panel pane of a plopp figure, scaled to the width it has, up to a limit."""
    return pn.pane.Matplotlib(
        figure.fig, tight=True, sizing_mode='stretch_width', max_width=900
    )


def _figure(title: str, array: Any) -> Any:
    return _figure_pane(pp.plot(array, title=title))

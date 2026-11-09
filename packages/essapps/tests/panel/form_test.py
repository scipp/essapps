# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What the widgets of a form send to the browser and receive from it, and that
a change reaches the field from the widget and the widget from the field. The
fields are tested in ``tests/fields``.
"""

from typing import Any

import panel as pn
import pytest
from pydantic import BaseModel, Field

from ess.apps import Client, Listing, SubmitError
from ess.apps.panel import Form
from ess.spec import FileRef, WorkflowSpec

from .conftest import GIVEN_TIMES, TIMES_AND_LISTS

type Spec = WorkflowSpec[Any, Any]


def form_of(spec: Spec, client: Client) -> Form:
    return Form(
        spec.params,
        Listing(client),
        title=spec.title,
        description=spec.description,
    )


@pytest.fixture
def options(client: Client, options_spec: Spec) -> Form:
    return form_of(options_spec, client)


@pytest.fixture
def subtract(client: Client, subtract_spec: Spec) -> Form:
    return form_of(subtract_spec, client)


@pytest.fixture
def stack(client: Client, stack_spec: Spec, run: FileRef) -> Form:
    return form_of(stack_spec, client)


@pytest.fixture
def times_and_lists(client: Client) -> Form:
    return form_of(TIMES_AND_LISTS, client)


def widget(form: Form, label: str) -> Any:
    """The input labelled ``label``, as the user finds it on the page."""
    (found,) = (w for w in form.panel().select(pn.widgets.Widget) if w.label == label)
    return found


def section(form: Form, title: str) -> Any:
    """The tab titled ``title``, or the card of a field of a group so titled."""
    (tabs,) = form.panel().select(pn.Tabs)
    (found,) = [
        *(t for t in tabs.objects if t.name == title),
        *(c for c in form.panel().select(pn.Card) if c.title == title),
    ]
    return found


def table(form: Form, title: str) -> Any:
    """The table of the section ``title``."""
    (found,) = section(form, title).select(pn.widgets.Tabulator)
    return found


def click(form: Form, title: str, label: str) -> None:
    """Click the button labelled ``label`` in the section ``title``."""
    widget_in(form, title, label).clicks += 1


def widget_in(form: Form, title: str, label: str) -> Any:
    """The input labelled ``label`` in the section ``title``."""
    (found,) = (
        w for w in section(form, title).select(pn.widgets.Widget) if w.label == label
    )
    return found


def way(form: Form, title: str) -> Any:
    """
    The radio buttons of the ways of the section ``title``, not those of a
    field in it.
    """
    return section(form, title).select(pn.widgets.RadioBoxGroup)[0]


def shown(widget: Any) -> Any:
    """The value of ``widget`` as Panel sends it to the browser."""
    # Panel has no public hook for what goes to the browser and back.
    return widget._process_param_change({'value': widget.value})['value']


def sent(widget: Any, value: Any) -> None:
    """Set ``widget`` as Panel does when the browser sends ``value``."""
    widget.param.update(**widget._process_property_change({'value': value}))


def message(form: Form, title: str) -> Any:
    """The message of the table of the section ``title``."""
    (found,) = section(form, title).select(pn.pane.Markdown)
    return found


def missing(form: Form) -> set[tuple[tuple[str | int, ...], str]]:
    """Each place where the form has nothing given, with what to do there."""
    with pytest.raises(SubmitError) as refused:
        form.params
    return {(e.loc, e.msg) for e in refused.value.errors}


def test_values_of_each_type_start_at_their_default_or_with_nothing_given(
    options: Form,
) -> None:
    assert missing(options) == {
        (('settings', 'flag'), 'give a value'),
        (('settings', 'count'), 'give a value'),
    }
    assert widget(options, 'Verbose').value is False
    assert widget(options, 'Name').value == 'run'
    assert (
        widget(options, 'Note').value == ''
    )  # an empty text with a default is a value
    assert widget(options, 'Mode').value == 'fast'
    assert shown(widget(options, 'Start (UTC)')) == '2026-01-01 00:00:00'


def test_a_time_edited_in_the_browser_keeps_its_zone(
    client: Client, times_and_lists: Form
) -> None:
    times = {**GIVEN_TIMES, 'at': '2026-03-04T05:06:07Z'}
    times_and_lists.set({'times': times, 'lists': {'banks': []}}, Listing(client))
    picker = widget(times_and_lists, 'At (UTC)')
    assert shown(picker) == '2026-03-04 05:06:07'
    sent(picker, '2026-03-04 06:06:07')
    assert times_and_lists.params['times'] == {**times, 'at': '2026-03-04T06:06:07Z'}


@pytest.mark.parametrize(
    ('title', 'label', 'kind'),
    [
        ('Settings', 'Count', pn.widgets.IntInput),
        ('Settings', 'Name', pn.widgets.TextInput),
        ('Settings', 'Mode', pn.widgets.Select),
        ('Settings', 'Start (UTC)', pn.widgets.DatetimePicker),
    ],
)
def test_each_kind_of_scalar_has_its_widget(
    options: Form, title: str, label: str, kind: type
) -> None:
    assert isinstance(widget_in(options, title, label), kind)


def test_a_float_has_a_float_input_with_the_description_of_its_field(
    client: Client,
) -> None:
    class Model(BaseModel):
        thickness: float = Field(default=1.0, description='In mm.')

    found = widget(Form(Model, Listing(client)), 'Thickness')

    assert isinstance(found, pn.widgets.FloatInput)
    assert found.description == 'In mm.'


def test_a_choice_in_a_group_shows_in_a_card_titled_by_its_field(
    client: Client, nested_spec: Spec
) -> None:
    cards = form_of(nested_spec, client).panel().select(pn.Card)

    assert 'Window' in [card.title for card in cards]


def test_a_file_set_stays_picked_once_shown_in_a_browser(
    client: Client, reduce_spec: Spec
) -> None:
    form = form_of(reduce_spec, client)
    form.panel().get_root()  # a rendered Select moves a value its options lack
    first, second = FileRef(file='uuid:1'), FileRef(file='uuid:2')

    form.set({'run': first}, Listing(client))
    form.set({'run': second}, Listing(client))

    assert form.held()[0]['run'] == second


def test_the_way_chosen_is_the_way_shown(client: Client, subtract: Form) -> None:
    subtract.set(
        {'background': {'scale': 1.0}, 'centre': {'x': 2.0, 'y': 0.0}}, Listing(client)
    )
    x = widget_in(subtract, 'Centre', 'X')
    (given,) = (c for c in section(subtract, 'Centre').select(pn.Column) if x in c)
    assert given.visible is True

    way(subtract, 'Centre').value = 'None'

    assert given.visible is False


def test_the_selector_of_one_way_is_hidden(subtract: Form) -> None:
    assert way(subtract, 'Background').visible is False
    assert way(subtract, 'Centre').visible is True


def test_an_input_and_its_field_follow_each_other(
    client: Client, options: Form
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}}, Listing(client))
    assert widget(options, 'Count').value == 1

    widget(options, 'Count').value = 2
    assert options.params['settings']['count'] == 2

    options.set({'settings': {'flag': False, 'count': 5}}, Listing(client))
    assert widget(options, 'Count').value == 5


def test_adding_an_element_shows_an_input_and_setting_the_field_shows_its_value(
    client: Client, options: Form
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}}, Listing(client))
    widget(options, 'Add to Banks').clicks += 1
    widget(options, 'Banks [0]').value = 3
    assert options.params['settings']['banks'] == [3]

    options.set({'settings': {'banks': [4, 5]}}, Listing(client))
    assert widget(options, 'Banks [1]').value == 5


def test_the_buttons_of_the_elements_change_the_field(
    client: Client, options: Form
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}}, Listing(client))
    widget(options, 'Add to Banks').clicks += 1
    assert missing(options) >= {(('settings', 'banks', 0), 'give a value')}
    widget(options, 'Remove from Banks').clicks += 1
    assert options.params['settings']['banks'] == []


def test_the_buttons_of_a_table_change_its_field(
    client: Client, stack: Form, run: FileRef
) -> None:
    stack.set({'runs': [run]}, Listing(client))
    click(stack, 'Shots', 'Add as new row')
    assert 'choose a file' in message(stack, 'Shots').object
    assert widget_in(stack, 'Shots', 'Update row').disabled is True

    widget_in(stack, 'Shots', 'Run').value = run
    widget_in(stack, 'Shots', 'Thickness').value = 2.0
    click(stack, 'Shots', 'Add as new row')
    assert stack.params['shots'] == [{'run': run, 'thickness': 2.0}]
    assert table(stack, 'Shots').selection == [0]
    assert widget_in(stack, 'Shots', 'Update row').disabled is False

    widget_in(stack, 'Shots', 'Thickness').value = 3.0
    click(stack, 'Shots', 'Discard')
    assert widget_in(stack, 'Shots', 'Thickness').value == 2.0

    click(stack, 'Shots', 'Remove')
    assert stack.params['shots'] == []


def test_a_selection_refused_goes_back_in_the_table(
    client: Client, stack: Form, run: FileRef
) -> None:
    shots = [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}]
    stack.set({'runs': [run], 'shots': shots}, Listing(client))
    table(stack, 'Shots').selection = [0]
    widget_in(stack, 'Shots', 'Thickness').value = 9.0

    table(stack, 'Shots').selection = [1]

    assert table(stack, 'Shots').selection == [0]
    assert 'holds changes' in message(stack, 'Shots').object


def test_a_way_and_its_field_follow_each_other(client: Client, subtract: Form) -> None:
    centre = way(subtract, 'Centre')
    assert centre.value is None

    centre.value = 'None'
    assert subtract.params['centre'] is None

    subtract.set(
        {'background': {'scale': 1.0}, 'centre': {'x': 2.0, 'y': 0.0}}, Listing(client)
    )
    assert centre.value == 'Given'


def test_a_table_and_its_field_follow_each_other(
    client: Client, stack: Form, run: FileRef
) -> None:
    shots = [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}]
    stack.set({'runs': [run], 'shots': shots}, Listing(client))
    assert table(stack, 'Shots').value['Thickness'].tolist() == ['2.0', '3.0']

    table(stack, 'Shots').selection = [1]
    assert widget_in(stack, 'Shots', 'Thickness').value == 3.0

    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    click(stack, 'Shots', 'Update row')
    assert [s['thickness'] for s in stack.params['shots']] == [2.0, 4.0]
    assert table(stack, 'Shots').value['Thickness'].tolist() == ['2.0', '4.0']

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


@pytest.mark.parametrize(
    ('given', 'label', 'edited'),
    [
        ('2026-03-04T05:06:07Z', 'At (UTC)', '2026-03-04T06:06:07Z'),
        ('2026-03-04T05:06:07+02:00', 'At (UTC+02:00)', '2026-03-04T06:06:07+02:00'),
        ('2026-03-04T05:06:07', 'At (no time zone)', '2026-03-04T06:06:07'),
        ('2026-03-04T05:06:07.123Z', 'At (UTC)', '2026-03-04T06:06:07Z'),
    ],
)
def test_a_time_edited_in_the_browser_keeps_its_zone(
    client: Client, times_and_lists: Form, given: str, label: str, edited: str
) -> None:
    times = {**GIVEN_TIMES, 'at': given}
    times_and_lists.set({'times': times, 'lists': {'banks': []}}, Listing(client))
    picker = widget(times_and_lists, label)
    assert shown(picker) == '2026-03-04 05:06:07'
    sent(picker, '2026-03-04 06:06:07')
    assert times_and_lists.params['times'] == {**times, 'at': edited}


def test_a_time_unchanged_in_the_browser_keeps_its_fraction_of_a_second(
    client: Client,
    times_and_lists: Form,
) -> None:
    times = {**GIVEN_TIMES, 'at': '2026-03-04T05:06:07.123Z'}
    times_and_lists.set({'times': times, 'lists': {'banks': []}}, Listing(client))
    sent(widget(times_and_lists, 'At (UTC)'), '2026-03-04 05:06:07')
    assert times_and_lists.params['times'] == {
        **times,
        'at': '2026-03-04T05:06:07.123000Z',
    }


def test_a_time_without_a_value_is_in_the_zone_of_its_type(
    client: Client,
    times_and_lists: Form,
) -> None:
    times_and_lists.set({'lists': {'banks': []}}, Listing(client))
    sent(widget(times_and_lists, 'At (no time zone)'), '2026-03-04 05:06:07')
    sent(widget(times_and_lists, 'Naive (no time zone)'), '2026-03-04 05:06:07')
    sent(widget(times_and_lists, 'Aware (UTC)'), '2026-03-04 05:06:07')
    assert times_and_lists.params['times'] == GIVEN_TIMES


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

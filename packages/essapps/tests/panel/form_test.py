# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import panel as pn
import pytest
from pydantic import AwareDatetime, BaseModel, Field, NaiveDatetime

from ess.apps import Client, Listing, Record, SubmitError
from ess.apps.panel import Form
from ess.apps.testing import FakeFiles
from ess.spec import File, FileRef, OutputRef, Referenced, RunFile, WorkflowSpec

type Spec = WorkflowSpec[Any, Any]


LokiMask = Annotated[Path, File(kind='mask', instrument='loki')]
"""A mask of the LoKI instrument."""


class LokiMaskParams(BaseModel):
    mask: LokiMask


class Value(BaseModel):
    value: float


LOKI_MASK: Spec = WorkflowSpec(
    name='loki-mask',
    version=1,
    title='LoKI mask',
    description='The counts of a LoKI mask.',
    params=LokiMaskParams,
    outputs=Value,
)


class Measured(BaseModel):
    run: RunFile
    thickness: float = 1.0
    mask: LokiMask | None = None


class MeasuredParams(BaseModel):
    measured: Measured


MEASURED: Spec = WorkflowSpec(
    name='measured',
    version=1,
    title='Measured',
    description='A run with its settings and its mask.',
    params=MeasuredParams,
    outputs=Value,
)


class Times(BaseModel):
    at: datetime
    naive: NaiveDatetime
    aware: AwareDatetime


class Lists(BaseModel):
    banks: list[int]
    angles: list[float] = [1.0, 2.0]
    names: list[str] = []


class TimesAndLists(BaseModel):
    times: Times
    lists: Lists


TIMES_AND_LISTS: Spec = WorkflowSpec(
    name='times-and-lists',
    version=1,
    title='Times and lists',
    description='Times in each zone, and lists of scalars.',
    params=TimesAndLists,
    outputs=Value,
)


class Left(BaseModel):
    kind: Literal['left'] = 'left'


class Right(BaseModel):
    kind: Literal['right'] = 'right'


class SidesParams(BaseModel):
    sides: list[Annotated[Left | Right, Field(discriminator='kind')]]


SIDES: Spec = WorkflowSpec(
    name='sides',
    version=1,
    title='Sides',
    description='A list of a union of groups.',
    params=SidesParams,
    outputs=Value,
)


@pytest.fixture
def form(client: Client, reduce_spec: Spec) -> Form:
    return Form(reduce_spec, client)


@pytest.fixture
def options(client: Client, options_spec: Spec) -> Form:
    return Form(options_spec, client)


@pytest.fixture
def subtract(client: Client, subtract_spec: Spec) -> Form:
    return Form(subtract_spec, client)


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


def rows(form: Form, title: str) -> list[dict[str, str]]:
    """The rows shown in the table of the section ``title``, each by column."""
    return table(form, title).value.to_dict('records')


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


def picker(form: Form, title: str) -> Any:
    """The picker of a file, or of a record, of the section ``title``."""
    (found,) = section(form, title).select(pn.widgets.Select)
    return found


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


def test_params_are_the_defaults_and_what_is_given(form: Form, run: FileRef) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}})
    assert form.params == {
        'reduction': {'bins': 2, 'norm': 'monitor'},
        'run': run,
        'sample': {'thickness': 2.0},
        'beam_centre': {'x': 0.0, 'y': 0.0},
        'detector': {'kind': 'front', 'distance': 1.0},
        'mask': None,
    }


def test_an_input_without_a_default_starts_with_nothing_given(form: Form) -> None:
    assert missing(form) == {
        (('run',), 'choose a file'),
        (('sample', 'thickness'), 'give a value'),
    }


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


def test_values_of_each_type_round_trip_through_a_record(
    client: Client, options: Form, options_spec: Spec
) -> None:
    params = {
        'settings': {
            'flag': True,
            'verbose': True,
            'name': 'x',
            'note': '',
            'count': 3,
            'mode': 'slow',
            'start': '2026-03-04T05:06:07Z',
            'banks': [3, 1],
        },
        'direct_beam': None,
    }
    options.set(params)
    record = client.compute(options_spec, params=options.params)
    assert client.failure(record) is None
    assert record.request.params == params


def test_a_list_of_scalars_is_a_list_of_inputs(options: Form) -> None:
    options.set({'settings': {'flag': False, 'count': 1, 'banks': [3, 1]}})
    assert widget(options, 'Banks [1]').value == 1
    widget(options, 'Remove from Banks').clicks += 1
    assert options.params['settings']['banks'] == [3]
    widget(options, 'Add to Banks').clicks += 1
    assert missing(options) == {(('settings', 'banks', 1), 'give a value')}
    widget(options, 'Banks [1]').value = 4
    assert options.params['settings']['banks'] == [3, 4]


def test_a_cleared_input_of_a_value_with_a_default_is_not_given_the_default(
    form: Form, options: Form, run: FileRef
) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}})
    widget(form, 'Bins').value = None
    assert missing(form) == {(('reduction', 'bins'), 'give a value')}

    options.set({'settings': {'flag': False, 'count': 1}, 'direct_beam': None})
    widget(options, 'Name').value = ''  # a cleared text with a default is ''
    assert options.params['settings']['name'] == ''


def test_a_choice_without_a_default_starts_unset_and_the_backend_refuses_it(
    client: Client, options: Form, options_spec: Spec
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}})
    assert 'direct_beam' not in options.params
    with pytest.raises(SubmitError) as refused:
        client.submit(options_spec, params=options.params)
    assert [e.loc for e in refused.value.errors] == [('direct_beam',)]


def test_a_file_field_typed_x_or_none_offers_a_file_and_none(
    options: Form,
) -> None:
    assert options.ways('direct_beam') == ['A file', 'None']
    options.set({'settings': {'flag': False, 'count': 1}, 'direct_beam': None})
    assert options.params['direct_beam'] is None


def test_a_group_typed_x_or_none_without_a_default_starts_unset(
    client: Client, subtract: Form, subtract_spec: Spec
) -> None:
    assert way(subtract, 'Centre').options == ['Given', 'From a record', 'None']
    assert way(subtract, 'Centre').value is None
    assert subtract.params == {'background': {'scale': 1.0}}
    with pytest.raises(SubmitError) as refused:
        client.submit(subtract_spec, params=subtract.params)
    assert [e.loc for e in refused.value.errors] == [('centre',)]


def test_a_place_written_inline_or_by_reference_without_a_default_starts_unset(
    client: Client, find_spec: Spec
) -> None:
    found = find_spec.outputs.model_fields['centre'].annotation

    class CentreParams(BaseModel):
        centre: found | Referenced[found]  # type: ignore[valid-type]

    spec: Spec = WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='A centre, given or from a record.',
        params=CentreParams,
        outputs=Value,
    )
    form = Form(spec, client)
    assert form.ways('centre') == ['Given', 'From a record']
    assert way(form, 'Centre').value is None
    assert form.params == {}


def test_a_group_typed_x_or_none_is_given_each_way(
    client: Client,
    subtract: Form,
    subtract_spec: Spec,
    find_spec: Spec,
    run: FileRef,
) -> None:
    found = client.compute(find_spec, params={'run': run})
    subtract.refresh()
    assert way(subtract, 'Centre').options == ['Given', 'From a record', 'None']

    for centre in (None, {'x': 2.0, 'y': 0.0}, found.ref('centre')):
        params = {'background': {'scale': 1.0}, 'centre': centre}
        subtract.set(params)
        assert subtract.params == params
        record = client.compute(subtract_spec, params=subtract.params)
        assert client.failure(record) is None
        assert record.request.params == params


def test_a_union_offers_its_alternatives_and_no_record(
    client: Client, form: Form, find_spec: Spec, run: FileRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.refresh()
    assert form.ways('detector') == ['Front', 'Rear bank']


def test_a_union_marked_referenced_offers_one_choice_of_alternatives_and_record(
    client: Client, find_spec: Spec, run: FileRef
) -> None:
    centre = find_spec.outputs.model_fields['centre'].annotation

    class SideParams(BaseModel):
        side: (
            Annotated[Left | Right, Field(discriminator='kind')] | Referenced[centre]  # type: ignore[valid-type]
        )

    spec: Spec = WorkflowSpec(
        name='side',
        version=1,
        title='Side',
        description='A side, or the centre of a record.',
        params=SideParams,
        outputs=Value,
    )
    found = client.compute(find_spec, params={'run': run})
    side = Form(spec, client)
    assert side.ways('side') == ['Left', 'Right', 'From a record']

    way(side, 'Side').value = 'From a record'
    picker(side, 'Side').value = found.ref('centre')
    assert side.params == {'side': found.ref('centre')}
    way(side, 'Side').value = 'Right'
    assert side.params == {'side': {'kind': 'right'}}


def test_set_refuses_a_tag_of_no_alternative(form: Form) -> None:
    with pytest.raises(ValueError, match='side: not an alternative'):
        form.set({'detector': {'kind': 'side'}})


def test_a_record_is_offered_and_lists_each_fitting_output_while_readable(
    client: Client, form: Form, find_spec: Spec, reduce_spec: Spec, run: FileRef
) -> None:
    assert form.ways('beam_centre') == ['Given', 'From a record']
    nothing = {'(choose a record)'}
    assert set(picker(form, 'Beam Centre').options) == nothing

    client.compute(reduce_spec, params={'run': run, 'sample': {'thickness': 2.0}})
    form.refresh()
    assert set(picker(form, 'Beam Centre').options) == nothing  # does not fit

    found = client.compute(find_spec, params={'run': run})
    form.refresh()
    assert form.ways('beam_centre') == ['Given', 'From a record']
    assert found.ref('centre') in picker(form, 'Beam Centre').options.values()

    client.release(found)
    form.refresh()
    assert set(picker(form, 'Beam Centre').options) == nothing


def test_the_way_of_a_record_with_no_record_picked_is_not_given_the_default(
    client: Client, form: Form, find_spec: Spec, run: FileRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.set({'run': run, 'sample': {'thickness': 2.0}})
    way(form, 'Beam Centre').value = 'From a record'
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_a_picked_record_that_is_released_is_not_given_the_default(
    client: Client, form: Form, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    form.set(
        {
            'run': run,
            'sample': {'thickness': 2.0},
            'beam_centre': found.ref('centre'),
        }
    )
    assert form.params['beam_centre'] == found.ref('centre')

    client.release(found)
    form.refresh()
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_with_a_reference_the_client_may_not_read_picks_no_record(
    client: Client, form: Form, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    client.release(found)
    form.set(
        {
            'run': run,
            'sample': {'thickness': 2.0},
            'beam_centre': found.ref('centre'),
        }
    )
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_shows_params_as_a_record_holds_them(
    client: Client,
    files: FakeFiles,
    form: Form,
    find_spec: Spec,
    reduce_spec: Spec,
    run: FileRef,
) -> None:
    found = client.compute(find_spec, params={'run': run})
    mask = files.measure(201, {'counts': 2.0}, kind='mask')
    record = client.compute(
        reduce_spec,
        params={
            'reduction': {'bins': 4, 'norm': 'none'},
            'run': run,
            'sample': {'thickness': 2.0},
            'beam_centre': found.ref('centre'),
            'detector': {'kind': 'rear', 'tilt': 0.5},
            'mask': mask,
        },
    )
    assert client.output(record, 'total').value == 2.0
    form.set(record.request.params)
    assert form.params == record.request.params
    assert form.ways('beam_centre') == ['Given', 'From a record']


def test_the_file_picker_lists_a_file_measured_after_the_form_was_made(
    files: FakeFiles, form: Form, run: FileRef
) -> None:
    form.refresh()
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
    ]

    files.measure(102, {'counts': 1.0, 'x': 0.0}, role='sample')
    form.refresh()
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
        'run 102 (sample)',
    ]


def test_a_file_picker_lists_the_files_that_fit_its_marker(
    files: FakeFiles, form: Form, run: FileRef
) -> None:
    files.measure(201, {'counts': 1.0}, kind='mask')
    form.refresh()
    assert form.ways('mask') == ['A file', 'None']
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
    ]
    assert list(picker(form, 'Mask').options) == ['(choose a file)', 'run 201']


def test_a_file_picker_lists_the_files_that_fit_each_key_of_its_metadata(
    client: Client, files: FakeFiles
) -> None:
    files.measure(201, {'counts': 1.0}, kind='mask', instrument='dream')
    files.measure(202, {'counts': 1.0}, kind='mask', instrument='loki')
    form = Form(LOKI_MASK, client)
    assert list(picker(form, 'Mask').options) == ['(choose a file)', 'run 202']


def test_a_file_that_does_not_fit_stays_picked_when_set(
    files: FakeFiles, form: Form, run: FileRef
) -> None:
    mask = files.measure(201, {'counts': 1.0}, kind='mask')
    form.set({'run': mask, 'sample': {'thickness': 2.0}})
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
        str(mask),
    ]
    assert form.params['run'] == mask


def test_a_picked_file_that_no_longer_fits_stays_picked(
    files: FakeFiles, form: Form, run: FileRef
) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}})
    files.correct(run, kind='mask')
    form.refresh()
    assert list(picker(form, 'Run').options) == ['(choose a file)', str(run)]
    assert form.params['run'] == run


def test_a_listing_labels_each_record_and_the_outputs_that_fit(
    client: Client, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    listing = Listing(client)
    at = f'{found.created.astimezone():%H:%M}'
    assert listing.labels == {found.id: f'Find centre ({at})'}
    centre = find_spec.outputs.model_fields['centre'].annotation
    assert listing.fitting(centre) == {f'BeamCentre ({at})': found.ref('centre')}
    assert listing.files(File()) == {'run 101 (sample)': run}


def test_a_file_without_a_run_is_labelled_by_its_identity(
    client: Client, files: FakeFiles, run: FileRef
) -> None:
    files.correct(run, run=None)
    assert Listing(client).files(File()) == {f'{run} (sample)': run}


@pytest.fixture
def times_and_lists(client: Client) -> Form:
    return Form(TIMES_AND_LISTS, client)


GIVEN_TIMES = {
    'at': '2026-03-04T05:06:07',
    'naive': '2026-03-04T05:06:07',
    'aware': '2026-03-04T05:06:07Z',
}


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
    times_and_lists: Form, given: str, label: str, edited: str
) -> None:
    times = {**GIVEN_TIMES, 'at': given}
    times_and_lists.set({'times': times, 'lists': {'banks': []}})
    picker = widget(times_and_lists, label)
    assert shown(picker) == '2026-03-04 05:06:07'
    sent(picker, '2026-03-04 06:06:07')
    assert times_and_lists.params['times'] == {**times, 'at': edited}


def test_a_time_unchanged_in_the_browser_keeps_its_fraction_of_a_second(
    times_and_lists: Form,
) -> None:
    times = {**GIVEN_TIMES, 'at': '2026-03-04T05:06:07.123Z'}
    times_and_lists.set({'times': times, 'lists': {'banks': []}})
    sent(widget(times_and_lists, 'At (UTC)'), '2026-03-04 05:06:07')
    assert times_and_lists.params['times'] == {
        **times,
        'at': '2026-03-04T05:06:07.123000Z',
    }


def test_a_time_without_a_value_is_in_the_zone_of_its_type(
    times_and_lists: Form,
) -> None:
    times_and_lists.set({'lists': {'banks': []}})
    sent(widget(times_and_lists, 'At (no time zone)'), '2026-03-04 05:06:07')
    sent(widget(times_and_lists, 'Naive (no time zone)'), '2026-03-04 05:06:07')
    sent(widget(times_and_lists, 'Aware (UTC)'), '2026-03-04 05:06:07')
    assert times_and_lists.params['times'] == GIVEN_TIMES


def test_a_list_without_a_default_starts_unset(times_and_lists: Form) -> None:
    times_and_lists.set({'times': GIVEN_TIMES})
    assert missing(times_and_lists) == {
        (('lists', 'banks'), 'add an element, or remove for none')
    }
    widget(times_and_lists, 'Remove from Banks').clicks += 1
    assert times_and_lists.params['lists']['banks'] == []
    widget(times_and_lists, 'Add to Banks').clicks += 1
    assert missing(times_and_lists) == {(('lists', 'banks', 0), 'give a value')}


def test_a_list_with_a_default_starts_at_it(times_and_lists: Form) -> None:
    times_and_lists.set({'times': GIVEN_TIMES, 'lists': {'banks': []}})
    assert times_and_lists.params['lists']['angles'] == [1.0, 2.0]


def test_an_empty_text_in_a_list_of_texts_is_a_text(times_and_lists: Form) -> None:
    params = {
        'times': GIVEN_TIMES,
        'lists': {'banks': [1], 'angles': [1.0], 'names': ['', 'a']},
    }
    times_and_lists.set(params)
    assert times_and_lists.params == params
    widget(times_and_lists, 'Add to Names').clicks += 1
    assert times_and_lists.params['lists']['names'] == ['', 'a', '']


@pytest.fixture
def measured(client: Client, files: FakeFiles, run: FileRef) -> Form:
    files.measure(202, {'counts': 1.0}, kind='mask', instrument='loki')
    return Form(MEASURED, client)


def test_a_file_at_a_field_of_a_group_is_a_picker_and_x_or_none_a_choice(
    measured: Form, run: FileRef
) -> None:
    assert measured.ways('measured') == ['Given']
    assert list(widget(measured, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
    ]
    assert way(measured, 'Mask').options == ['A file', 'None']
    assert list(picker(measured, 'Mask').options) == ['(choose a file)', 'run 202']
    assert missing(measured) == {(('measured', 'run'), 'choose a file')}
    widget(measured, 'Run').value = run
    assert measured.params == {'measured': {'run': run, 'thickness': 1.0, 'mask': None}}


def test_a_group_holding_a_file_is_set_as_a_record_holds_it(
    measured: Form, run: FileRef
) -> None:
    held = {'run': run.model_dump(by_alias=True), 'thickness': 2.0, 'mask': None}
    measured.set({'measured': held})
    assert measured.params == {'measured': {'run': run, 'thickness': 2.0, 'mask': None}}


@pytest.fixture
def stack(client: Client, stack_spec: Spec, run: FileRef) -> Form:
    return Form(stack_spec, client)


def test_a_table_starts_unset_and_a_list_with_a_default_at_it(stack: Form) -> None:
    assert missing(stack) == {
        (('runs',), 'add a row, or remove for none'),
        (('shots',), 'add a row, or remove for none'),
    }
    assert rows(stack, 'Centres') == [{'X': '1.0', 'Y': '0.0', 'Record': ''}]
    click(stack, 'Runs', 'Remove')
    click(stack, 'Shots', 'Remove')
    assert stack.params == {
        'runs': [],
        'shots': [],
        'centres': [{'x': 1.0, 'y': 0.0}],
    }


def test_a_table_without_rows_is_refused_by_its_length(
    client: Client, stack: Form, stack_spec: Spec
) -> None:
    click(stack, 'Runs', 'Remove')
    click(stack, 'Shots', 'Remove')
    with pytest.raises(SubmitError) as refused:
        client.submit(stack_spec, params=stack.params)
    assert [e.loc for e in refused.value.errors] == [('runs',)]


def test_a_row_of_a_file_field_is_a_file_picked_in_the_editor(
    stack: Form, files: FakeFiles, run: FileRef
) -> None:
    other = files.measure(102, {'counts': 4.0, 'x': 0.0}, role='sample')
    stack.refresh()
    for picked in (run, other):
        picker(stack, 'Runs').value = picked
        click(stack, 'Runs', 'Add as new row')
    click(stack, 'Shots', 'Remove')
    assert stack.params['runs'] == [run, other]
    assert rows(stack, 'Runs') == [
        {'File': 'run 101 (sample)'},
        {'File': 'run 102 (sample)'},
    ]


def test_a_row_is_added_from_the_editor_only_if_nothing_is_missing(
    stack: Form, run: FileRef
) -> None:
    click(stack, 'Shots', 'Add as new row')
    assert (('shots',), 'add a row, or remove for none') in missing(stack)
    widget_in(stack, 'Shots', 'Run').value = run
    widget_in(stack, 'Shots', 'Thickness').value = 2.0
    click(stack, 'Shots', 'Add as new row')
    widget_in(stack, 'Shots', 'Thickness').value = 3.0
    click(stack, 'Shots', 'Add as new row')
    click(stack, 'Runs', 'Remove')
    assert stack.params['shots'] == [
        {'run': run, 'thickness': 2.0},
        {'run': run, 'thickness': 3.0},
    ]
    assert rows(stack, 'Shots')[1] == {'Run': 'run 101 (sample)', 'Thickness': '3.0'}


def test_a_row_selected_is_edited_and_updated(stack: Form, run: FileRef) -> None:
    stack.set(
        {
            'runs': [run],
            'shots': [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}],
        }
    )
    table(stack, 'Shots').selection = [0]
    assert widget_in(stack, 'Shots', 'Thickness').value == 2.0
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    assert missing(stack) == {
        (('shots', 0), 'the editor holds changes: update the row, or deselect it')
    }
    click(stack, 'Shots', 'Update row')
    assert [s['thickness'] for s in stack.params['shots']] == [4.0, 3.0]


def test_a_row_the_editor_cannot_show_is_kept_when_selected(
    stack: Form, run: FileRef
) -> None:
    unread = OutputRef(record='f00d', output='centre')
    stack.set({'runs': [run], 'shots': [], 'centres': [unread]})
    table(stack, 'Centres').selection = [0]
    assert stack.params['centres'] == [unread]


def test_a_row_is_not_updated_while_a_value_is_missing_in_the_editor(
    stack: Form, run: FileRef
) -> None:
    stack.set({'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]})
    table(stack, 'Shots').selection = [0]
    widget_in(stack, 'Shots', 'Thickness').value = None
    click(stack, 'Shots', 'Update row')
    assert rows(stack, 'Shots') == [{'Run': 'run 101 (sample)', 'Thickness': '2.0'}]


def test_remove_removes_the_rows_selected(stack: Form, run: FileRef) -> None:
    centres = [{'x': float(x), 'y': 0.0} for x in range(3)]
    stack.set({'runs': [run], 'shots': [], 'centres': centres})
    table(stack, 'Centres').selection = [0, 2]
    click(stack, 'Centres', 'Remove')
    assert stack.params['centres'] == [centres[1]]


def test_a_row_may_be_an_output_of_a_record_that_fits(
    client: Client, stack: Form, find_spec: Spec, run: FileRef
) -> None:
    assert way(stack, 'Centres').options == ['Given', 'From a record']
    with pytest.raises(TypeError, match='centres: not a choice of ways'):
        stack.ways('centres')
    found = client.compute(find_spec, params={'run': run})
    stack.refresh()
    way(stack, 'Centres').value = 'From a record'
    picker(stack, 'Centres').value = found.ref('centre')
    click(stack, 'Centres', 'Add as new row')
    click(stack, 'Runs', 'Remove')
    click(stack, 'Shots', 'Remove')
    assert stack.params['centres'] == [{'x': 1.0, 'y': 0.0}, found.ref('centre')]
    label = f'BeamCentre ({found.created.astimezone():%H:%M})'
    assert rows(stack, 'Centres')[1] == {'X': '', 'Y': '', 'Record': label}


def test_a_row_is_shown_under_the_file_or_the_record_it_is_given_as(
    client: Client, find_spec: Spec, run: FileRef
) -> None:
    centre = find_spec.outputs.model_fields['centre'].annotation

    class RunsParams(BaseModel):
        runs: list[RunFile | Referenced[centre]]  # type: ignore[valid-type]

    spec: Spec = WorkflowSpec(
        name='runs',
        version=1,
        title='Runs',
        description='Runs, each a file or the centre of a record.',
        params=RunsParams,
        outputs=Value,
    )
    found = client.compute(find_spec, params={'run': run})
    form = Form(spec, client)
    form.set({'runs': [run, found.ref('centre')]})
    label = f'BeamCentre ({found.created.astimezone():%H:%M})'
    assert rows(form, 'Runs') == [
        {'File': 'run 101 (sample)', 'Record': ''},
        {'File': '', 'Record': label},
    ]


def test_the_editor_offers_no_record_of_a_row_shown_before(
    client: Client, stack: Form, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    centres = [found.ref('centre'), {'x': 2.0, 'y': 0.0}]
    stack.set({'runs': [run], 'shots': [], 'centres': centres})
    way(stack, 'Centres').value = 'From a record'
    click(stack, 'Centres', 'Add as new row')
    assert stack.params['centres'] == centres


def test_tables_round_trip_through_a_record(
    client: Client,
    files: FakeFiles,
    stack: Form,
    stack_spec: Spec,
    find_spec: Spec,
    run: FileRef,
) -> None:
    other = files.measure(102, {'counts': 4.0, 'x': 0.0}, role='sample')
    found = client.compute(find_spec, params={'run': run})
    record = client.compute(
        stack_spec,
        params={
            'runs': [run, other],
            'shots': [{'run': other, 'thickness': 2.0}],
            'centres': [found.ref('centre'), {'x': 2.0, 'y': 0.0}],
        },
    )
    assert client.output(record, 'total').value == 6.0 + 4.0 + 2.0 + 1.5 + 2.0
    stack.set(record.request.params)
    assert stack.params == record.request.params
    assert rows(stack, 'Shots') == [{'Run': 'run 102 (sample)', 'Thickness': '2.0'}]


def test_a_list_of_a_union_has_a_column_for_the_tag(client: Client) -> None:
    sides = Form(SIDES, client)
    click(sides, 'Sides', 'Add as new row')  # no way chosen: no row
    assert missing(sides) == {(('sides',), 'add a row, or remove for none')}
    for side in ('Right', 'Left'):
        way(sides, 'Sides').value = side
        click(sides, 'Sides', 'Add as new row')
    assert sides.params == {'sides': [{'kind': 'right'}, {'kind': 'left'}]}
    assert rows(sides, 'Sides') == [{'Kind': 'Right'}, {'Kind': 'Left'}]


@pytest.fixture
def nested(client: Client, nested_spec: Spec) -> Form:
    return Form(nested_spec, client)


@pytest.fixture
def found(client: Client, find_spec: Spec, run: FileRef) -> Record:
    return client.compute(find_spec, params={'run': run})


@pytest.fixture
def filled(nested: Form, found: Record, run: FileRef) -> Form:
    """The nested form with a value at each field without a default."""
    nested.set(
        {
            'bins': 2,
            'specimen': {
                'run': run,
                'window': {'start': 0.5},
                'centre': found.ref('centre'),
            },
        }
    )
    return nested


def test_fields_at_any_depth_start_at_their_default_or_with_nothing_given(
    nested: Form,
) -> None:
    assert missing(nested) == {
        (('bins',), 'give a value'),
        (('specimen', 'run'), 'choose a file'),
        (('specimen', 'window', 'start'), 'give a value'),
        (('specimen', 'centre'), 'choose a record'),
    }


def test_a_scalar_parameter_is_an_input_in_its_tab(filled: Form) -> None:
    widget_in(filled, 'Bins', 'Bins').value = 3
    assert filled.params['bins'] == 3
    with pytest.raises(TypeError, match='bins: not a choice of ways'):
        filled.ways('bins')


def test_a_group_at_a_field_is_a_card_of_its_fields(filled: Form) -> None:
    assert not way(filled, 'Window').visible
    assert way(filled, 'Window').options == ['Given']
    widget_in(filled, 'Frame', 'Start').value = 2.0
    specimen = filled.params['specimen']
    assert specimen['window'] == {'start': 0.5, 'stop': 1.0}
    assert specimen['frame'] == {'start': 2.0, 'stop': 1.0}


def test_a_union_at_a_field_offers_its_alternatives(filled: Form) -> None:
    assert way(filled, 'Shape').options == ['Disc', 'Slab']
    assert filled.params['specimen']['shape'] == {'kind': 'disc', 'radius': 1.0}
    way(filled, 'Shape').value = 'Slab'
    assert filled.params['specimen']['shape'] == {'kind': 'slab', 'width': 1.0}


def test_x_or_none_at_a_field_is_a_choice_with_none(filled: Form) -> None:
    for title in ('Scale', 'Crop'):
        assert way(filled, title).options == ['Given', 'None']
        way(filled, title).value = 'Given'
    assert way(filled, 'Mask').options == ['A file', 'None']
    assert missing(filled) == {
        (('specimen', 'scale'), 'give a value'),
        (('specimen', 'crop', 'start'), 'give a value'),
    }
    widget_in(filled, 'Scale', 'Scale').value = 2.0
    widget_in(filled, 'Crop', 'Start').value = 0.0
    assert filled.params['specimen']['scale'] == 2.0
    assert filled.params['specimen']['crop'] == {'start': 0.0, 'stop': 1.0}


def test_a_record_at_a_field_is_listed_when_the_form_is_refreshed(
    client: Client, nested: Form, find_spec: Spec, run: FileRef
) -> None:
    assert way(nested, 'Centre').options == ['From a record']
    assert set(picker(nested, 'Centre').options) == {'(choose a record)'}
    found = client.compute(find_spec, params={'run': run})
    nested.refresh()
    picker(nested, 'Centre').value = found.ref('centre')
    assert (('specimen', 'centre'), 'choose a record') not in missing(nested)


def test_a_list_of_groups_at_a_field_is_a_table(filled: Form, run: FileRef) -> None:
    assert rows(filled, 'Shots') == []
    widget_in(filled, 'Shots', 'Run').value = run
    click(filled, 'Shots', 'Add as new row')
    assert filled.params['specimen']['shots'] == [{'run': run, 'thickness': 1.0}]
    assert rows(filled, 'Shots') == [{'Run': 'run 101 (sample)', 'Thickness': '1.0'}]


def test_a_list_of_a_union_at_a_field_has_a_column_per_field_of_each_alternative(
    filled: Form,
) -> None:
    for shape in ('Slab', 'Disc'):
        way(filled, 'Shapes').value = shape
        click(filled, 'Shapes', 'Add as new row')
    assert filled.params['specimen']['shapes'] == [
        {'kind': 'slab', 'width': 1.0},
        {'kind': 'disc', 'radius': 1.0},
    ]
    assert rows(filled, 'Shapes') == [
        {'Kind': 'Slab', 'Radius': '', 'Width': '1.0'},
        {'Kind': 'Disc', 'Radius': '1.0', 'Width': ''},
    ]


def test_values_at_any_depth_round_trip_through_a_record(
    client: Client,
    files: FakeFiles,
    nested: Form,
    nested_spec: Spec,
    found: Record,
    run: FileRef,
) -> None:
    mask = files.measure(201, {'counts': 2.0}, kind='mask')
    params = {
        'bins': 3,
        'specimen': {
            'run': run,
            'window': {'start': 0.5, 'stop': 2.0},
            'frame': {'start': 1.0, 'stop': 1.0},
            'shape': {'kind': 'slab', 'width': 2.0},
            'scale': 2.0,
            'crop': {'start': 0.0, 'stop': 1.0},
            'mask': mask,
            'centre': found.ref('centre'),
            'shots': [{'run': run, 'thickness': 2.0}],
            'shapes': [{'kind': 'disc', 'radius': 1.0}, {'kind': 'slab', 'width': 3.0}],
        },
    }
    record = client.compute(nested_spec, params=params)
    assert client.output(record, 'total').value == 3 * 1.5
    nested.set(record.request.params)
    assert nested.params == record.request.params

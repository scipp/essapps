# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from ess.apps import Client, Listing, Record, SubmitError
from ess.apps.fields import (
    ChoiceField,
    Fields,
    FileField,
    GroupField,
    Input,
    RecordField,
    RowsField,
    ScalarsField,
)
from ess.apps.testing import FakeFiles
from ess.spec import File, FileRef, OutputRef, Referenced, RunFile, WorkflowSpec

from ..panel.conftest import GIVEN_TIMES, TIMES_AND_LISTS, Value

type Spec = WorkflowSpec[Any, Any]


LokiMask = Annotated[Path, File(kind='mask', instrument='loki')]
"""A mask of the LoKI instrument."""


class LokiMaskParams(BaseModel):
    mask: LokiMask


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

HOLDS_CHANGES = 'the editor holds changes: add or update a row, or discard them'


def form_of(spec: Spec, client: Client) -> Fields:
    return Fields(spec.params, Listing(client), title=spec.title)


@pytest.fixture
def form(client: Client, reduce_spec: Spec) -> Fields:
    return form_of(reduce_spec, client)


@pytest.fixture
def options(client: Client, options_spec: Spec) -> Fields:
    return form_of(options_spec, client)


@pytest.fixture
def subtract(client: Client, subtract_spec: Spec) -> Fields:
    return form_of(subtract_spec, client)


def children(field: Fields | Any) -> list[Any]:
    """The fields directly inside ``field``."""
    if isinstance(field, Fields | GroupField):
        return list(field.fields.values())
    if isinstance(field, ChoiceField):
        return list(field.ways.values())
    if isinstance(field, ScalarsField):
        return list(field.elements)
    if isinstance(field, RowsField):
        return [field.editor]
    return []


def walk(field: Fields | Any) -> Iterator[Any]:
    """``field`` and all fields inside it, each before the fields inside it."""
    yield field
    for child in children(field):
        yield from walk(child)


def titled(field: Fields | Any) -> Iterator[tuple[str, Any]]:
    """Each field of a form or a group inside ``field`` with its title."""
    if isinstance(field, Fields | GroupField):
        for name, child in field.fields.items():
            yield field.titles[name], child
    for child in children(field):
        yield from titled(child)


def widget(form: Fields, label: str) -> Input:
    """The input labelled ``label``."""
    (found,) = (f for f in walk(form) if isinstance(f, Input) and f.label == label)
    return found


def section(form: Fields, title: str) -> Any:
    """The field titled ``title``, with the fields inside it."""
    (found,) = [f for t, f in titled(form) if t == title]
    return found


def table(form: Fields, title: str) -> RowsField:
    """The table of the section ``title``."""
    found = section(form, title)
    assert isinstance(found, RowsField)
    return found


def rows(form: Fields, title: str) -> list[dict[str, str]]:
    """The rows shown in the table of the section ``title``, each by column."""
    columns = table(form, title).columns
    count = len(next(iter(columns.values()), []))
    return [{c: texts[i] for c, texts in columns.items()} for i in range(count)]


def widget_in(form: Fields, title: str, label: str) -> Input:
    """The input labelled ``label`` in the section ``title``."""
    (found,) = (
        f
        for f in walk(section(form, title))
        if isinstance(f, Input) and f.label == label
    )
    return found


def way(form: Fields, title: str) -> ChoiceField:
    """
    The ways of the section ``title``, not those of a field in it.
    """
    return next(f for f in walk(section(form, title)) if isinstance(f, ChoiceField))


def picker(form: Fields, title: str) -> Input:
    """The picker of a file, or of a record, of the section ``title``."""
    (found,) = (
        f for f in walk(section(form, title)) if isinstance(f, FileField | RecordField)
    )
    return found


def missing(form: Fields) -> set[tuple[tuple[str | int, ...], str]]:
    """Each place where the form has nothing given, with what to do there."""
    with pytest.raises(SubmitError) as refused:
        form.params
    return {(e.loc, e.msg) for e in refused.value.errors}


@pytest.fixture
def times_and_lists(client: Client) -> Fields:
    return form_of(TIMES_AND_LISTS, client)


def test_params_are_the_defaults_and_what_is_given(
    client: Client, form: Fields, run: FileRef
) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}}, Listing(client))
    assert form.params == {
        'reduction': {'bins': 2, 'norm': 'monitor'},
        'run': run,
        'sample': {'thickness': 2.0},
        'beam_centre': {'x': 0.0, 'y': 0.0},
        'detector': {'kind': 'front', 'distance': 1.0},
        'mask': None,
    }


def test_an_input_without_a_default_starts_with_nothing_given(form: Fields) -> None:
    assert missing(form) == {
        (('run',), 'choose a file'),
        (('sample', 'thickness'), 'give a value'),
    }


def test_values_of_each_type_round_trip_through_a_record(
    client: Client, options: Fields, options_spec: Spec
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
    options.set(params, Listing(client))
    record = client.compute(options_spec, params=options.params)
    assert client.failure(record) is None
    assert record.request.params == params


def test_a_list_of_scalars_is_a_list_of_inputs(client: Client, options: Fields) -> None:
    options.set(
        {'settings': {'flag': False, 'count': 1, 'banks': [3, 1]}}, Listing(client)
    )
    assert widget(options, 'Banks [1]').value == 1
    section(options, 'Banks').remove()
    assert options.params['settings']['banks'] == [3]
    section(options, 'Banks').add()
    assert missing(options) == {(('settings', 'banks', 1), 'give a value')}
    widget(options, 'Banks [1]').value = 4
    assert options.params['settings']['banks'] == [3, 4]


def test_a_cleared_input_of_a_value_with_a_default_is_not_given_the_default(
    client: Client, form: Fields, options: Fields, run: FileRef
) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}}, Listing(client))
    widget(form, 'Bins').value = None
    assert missing(form) == {(('reduction', 'bins'), 'give a value')}

    options.set(
        {'settings': {'flag': False, 'count': 1}, 'direct_beam': None}, Listing(client)
    )
    widget(options, 'Name').value = ''  # a cleared text with a default is ''
    assert options.params['settings']['name'] == ''


def test_a_choice_without_a_default_starts_unset_and_the_workspace_refuses_it(
    client: Client, options: Fields, options_spec: Spec
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}}, Listing(client))
    assert 'direct_beam' not in options.params
    with pytest.raises(SubmitError) as refused:
        client.submit(options_spec, params=options.params)
    assert [e.loc for e in refused.value.errors] == [('direct_beam',)]


def test_a_file_field_typed_x_or_none_offers_a_file_and_none(
    client: Client,
    options: Fields,
) -> None:
    assert options.ways('direct_beam') == ['A file', 'None']
    options.set(
        {'settings': {'flag': False, 'count': 1}, 'direct_beam': None}, Listing(client)
    )
    assert options.params['direct_beam'] is None


def test_a_group_typed_x_or_none_without_a_default_starts_unset(
    client: Client, subtract: Fields, subtract_spec: Spec
) -> None:
    assert way(subtract, 'Centre').offered == ['Given', 'From a record', 'None']
    assert way(subtract, 'Centre').chosen is None
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
    form = form_of(spec, client)
    assert form.ways('centre') == ['Given', 'From a record']
    assert way(form, 'Centre').chosen is None
    assert form.params == {}


def test_a_group_typed_x_or_none_is_given_each_way(
    client: Client,
    subtract: Fields,
    subtract_spec: Spec,
    find_spec: Spec,
    run: FileRef,
) -> None:
    found = client.compute(find_spec, params={'run': run})
    subtract.refresh(Listing(client))
    assert way(subtract, 'Centre').offered == ['Given', 'From a record', 'None']

    for centre in (None, {'x': 2.0, 'y': 0.0}, found.ref('centre')):
        params = {'background': {'scale': 1.0}, 'centre': centre}
        subtract.set(params, Listing(client))
        assert subtract.params == params
        record = client.compute(subtract_spec, params=subtract.params)
        assert client.failure(record) is None
        assert record.request.params == params


def test_a_union_offers_its_alternatives_and_no_record(
    client: Client, form: Fields, find_spec: Spec, run: FileRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.refresh(Listing(client))
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
    side = form_of(spec, client)
    assert side.ways('side') == ['Left', 'Right', 'From a record']

    way(side, 'Side').chosen = 'From a record'
    picker(side, 'Side').value = found.ref('centre')
    assert side.params == {'side': found.ref('centre')}
    way(side, 'Side').chosen = 'Right'
    assert side.params == {'side': {'kind': 'right'}}


def test_set_refuses_a_tag_of_no_alternative(client: Client, form: Fields) -> None:
    with pytest.raises(ValueError, match='side: not an alternative'):
        form.set({'detector': {'kind': 'side'}}, Listing(client))


def test_a_record_is_offered_and_lists_each_fitting_output_while_readable(
    client: Client, form: Fields, find_spec: Spec, reduce_spec: Spec, run: FileRef
) -> None:
    assert form.ways('beam_centre') == ['Given', 'From a record']
    nothing = {'(choose a record)'}
    assert set(picker(form, 'Beam Centre').options) == nothing

    client.compute(reduce_spec, params={'run': run, 'sample': {'thickness': 2.0}})
    form.refresh(Listing(client))
    assert set(picker(form, 'Beam Centre').options) == nothing  # does not fit

    found = client.compute(find_spec, params={'run': run})
    form.refresh(Listing(client))
    assert form.ways('beam_centre') == ['Given', 'From a record']
    assert found.ref('centre') in picker(form, 'Beam Centre').options.values()

    client.release(found)
    form.refresh(Listing(client))
    assert set(picker(form, 'Beam Centre').options) == nothing


def test_the_way_of_a_record_with_no_record_picked_is_not_given_the_default(
    client: Client, form: Fields, find_spec: Spec, run: FileRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.set({'run': run, 'sample': {'thickness': 2.0}}, Listing(client))
    way(form, 'Beam Centre').chosen = 'From a record'
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_a_picked_record_that_is_released_is_not_given_the_default(
    client: Client, form: Fields, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    form.set(
        {
            'run': run,
            'sample': {'thickness': 2.0},
            'beam_centre': found.ref('centre'),
        },
        Listing(client),
    )
    assert form.params['beam_centre'] == found.ref('centre')

    client.release(found)
    form.refresh(Listing(client))
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_with_a_reference_the_client_may_not_read_picks_no_record(
    client: Client, form: Fields, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    client.release(found)
    form.set(
        {
            'run': run,
            'sample': {'thickness': 2.0},
            'beam_centre': found.ref('centre'),
        },
        Listing(client),
    )
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_shows_params_as_a_record_holds_them(
    client: Client,
    files: FakeFiles,
    form: Fields,
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
    form.set(record.request.params, Listing(client))
    assert form.params == record.request.params
    assert form.ways('beam_centre') == ['Given', 'From a record']


def test_the_file_picker_lists_a_file_measured_after_the_form_was_made(
    client: Client, files: FakeFiles, form: Fields, run: FileRef
) -> None:
    form.refresh(Listing(client))
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
    ]

    files.measure(102, {'counts': 1.0, 'x': 0.0}, role='sample')
    form.refresh(Listing(client))
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
        'run 102 (sample)',
    ]


def test_a_file_picker_lists_the_files_that_fit_its_marker(
    client: Client, files: FakeFiles, form: Fields, run: FileRef
) -> None:
    files.measure(201, {'counts': 1.0}, kind='mask')
    form.refresh(Listing(client))
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
    form = form_of(LOKI_MASK, client)
    assert list(picker(form, 'Mask').options) == ['(choose a file)', 'run 202']


def test_a_file_that_does_not_fit_stays_picked_when_set(
    client: Client, files: FakeFiles, form: Fields, run: FileRef
) -> None:
    mask = files.measure(201, {'counts': 1.0}, kind='mask')
    form.set({'run': mask, 'sample': {'thickness': 2.0}}, Listing(client))
    assert list(picker(form, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
        str(mask),
    ]
    assert form.params['run'] == mask


def test_a_picked_file_that_no_longer_fits_stays_picked(
    client: Client, files: FakeFiles, form: Fields, run: FileRef
) -> None:
    form.set({'run': run, 'sample': {'thickness': 2.0}}, Listing(client))
    files.correct(run, kind='mask')
    form.refresh(Listing(client))
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


def test_a_list_without_a_default_starts_unset(
    client: Client, times_and_lists: Fields
) -> None:
    times_and_lists.set({'times': GIVEN_TIMES}, Listing(client))
    assert missing(times_and_lists) == {
        (('lists', 'banks'), 'add an element, or remove for none')
    }
    section(times_and_lists, 'Banks').remove()
    assert times_and_lists.params['lists']['banks'] == []
    section(times_and_lists, 'Banks').add()
    assert missing(times_and_lists) == {(('lists', 'banks', 0), 'give a value')}


def test_a_list_with_a_default_starts_at_it(
    client: Client, times_and_lists: Fields
) -> None:
    times_and_lists.set({'times': GIVEN_TIMES, 'lists': {'banks': []}}, Listing(client))
    assert times_and_lists.params['lists']['angles'] == [1.0, 2.0]


def test_an_empty_text_in_a_list_of_texts_is_a_text(
    client: Client, times_and_lists: Fields
) -> None:
    params = {
        'times': GIVEN_TIMES,
        'lists': {'banks': [1], 'angles': [1.0], 'names': ['', 'a']},
    }
    times_and_lists.set(params, Listing(client))
    assert times_and_lists.params == params
    section(times_and_lists, 'Names').add()
    assert times_and_lists.params['lists']['names'] == ['', 'a', '']


@pytest.fixture
def measured(client: Client, files: FakeFiles, run: FileRef) -> Fields:
    files.measure(202, {'counts': 1.0}, kind='mask', instrument='loki')
    return form_of(MEASURED, client)


def test_a_file_at_a_field_of_a_group_is_a_picker_and_x_or_none_a_choice(
    measured: Fields, run: FileRef
) -> None:
    assert measured.ways('measured') == ['Given']
    assert list(widget(measured, 'Run').options) == [
        '(choose a file)',
        'run 101 (sample)',
    ]
    assert way(measured, 'Mask').offered == ['A file', 'None']
    assert list(picker(measured, 'Mask').options) == ['(choose a file)', 'run 202']
    assert missing(measured) == {(('measured', 'run'), 'choose a file')}
    widget(measured, 'Run').value = run
    assert measured.params == {'measured': {'run': run, 'thickness': 1.0, 'mask': None}}


def test_a_group_holding_a_file_is_set_as_a_record_holds_it(
    client: Client, measured: Fields, run: FileRef
) -> None:
    held = {'run': run.model_dump(by_alias=True), 'thickness': 2.0, 'mask': None}
    measured.set({'measured': held}, Listing(client))
    assert measured.params == {'measured': {'run': run, 'thickness': 2.0, 'mask': None}}


@pytest.fixture
def stack(client: Client, stack_spec: Spec, run: FileRef) -> Fields:
    return form_of(stack_spec, client)


def test_a_table_starts_unset_and_a_list_with_a_default_at_it(stack: Fields) -> None:
    assert missing(stack) == {
        (('runs',), 'add a row, or remove for none'),
        (('shots',), 'add a row, or remove for none'),
    }
    assert rows(stack, 'Centres') == [{'X': '1.0', 'Y': '0.0', 'Record': ''}]
    table(stack, 'Runs').remove_rows()
    table(stack, 'Shots').remove_rows()
    assert stack.params == {
        'runs': [],
        'shots': [],
        'centres': [{'x': 1.0, 'y': 0.0}],
    }


def test_a_table_without_rows_is_refused_by_its_length(
    client: Client, stack: Fields, stack_spec: Spec
) -> None:
    table(stack, 'Runs').remove_rows()
    table(stack, 'Shots').remove_rows()
    with pytest.raises(SubmitError) as refused:
        client.submit(stack_spec, params=stack.params)
    assert [e.loc for e in refused.value.errors] == [('runs',)]


def test_a_row_of_a_file_field_is_a_file_picked_in_the_editor(
    client: Client, stack: Fields, files: FakeFiles, run: FileRef
) -> None:
    other = files.measure(102, {'counts': 4.0, 'x': 0.0}, role='sample')
    stack.refresh(Listing(client))
    for picked in (run, other):
        picker(stack, 'Runs').value = picked
        table(stack, 'Runs').add_row()
    table(stack, 'Shots').remove_rows()
    assert stack.params['runs'] == [run, other]
    assert rows(stack, 'Runs') == [
        {'File': 'run 101 (sample)'},
        {'File': 'run 102 (sample)'},
    ]


def test_a_row_is_added_from_the_editor_only_if_nothing_is_missing(
    stack: Fields, run: FileRef
) -> None:
    table(stack, 'Shots').add_row()
    assert (('shots',), 'add a row, or remove for none') in missing(stack)
    widget_in(stack, 'Shots', 'Run').value = run
    widget_in(stack, 'Shots', 'Thickness').value = 2.0
    table(stack, 'Shots').add_row()
    widget_in(stack, 'Shots', 'Thickness').value = 3.0
    table(stack, 'Shots').add_row()
    table(stack, 'Runs').remove_rows()
    assert stack.params['shots'] == [
        {'run': run, 'thickness': 2.0},
        {'run': run, 'thickness': 3.0},
    ]
    assert rows(stack, 'Shots')[1] == {'Run': 'run 101 (sample)', 'Thickness': '3.0'}


def test_a_row_selected_is_edited_and_updated(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {
            'runs': [run],
            'shots': [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}],
        },
        Listing(client),
    )
    table(stack, 'Shots').select([0])
    assert widget_in(stack, 'Shots', 'Thickness').value == 2.0
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    assert missing(stack) == {(('shots', 0), HOLDS_CHANGES)}
    table(stack, 'Shots').update_row()
    assert [s['thickness'] for s in stack.params['shots']] == [4.0, 3.0]


def test_the_selection_stays_while_the_editor_holds_changes(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {
            'runs': [run],
            'shots': [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}],
        },
        Listing(client),
    )
    selection = table(stack, 'Shots')
    thickness = widget_in(stack, 'Shots', 'Thickness')
    selection.select([0])
    thickness.value = 4.0
    for refused in ([1], [], [0, 1]):
        selection.select(refused)
        assert selection.selection == [0]
        assert thickness.value == 4.0
    table(stack, 'Shots').discard()
    assert thickness.value == 2.0
    selection.select([1])
    assert thickness.value == 3.0
    assert [s['thickness'] for s in stack.params['shots']] == [2.0, 3.0]


def test_an_editor_changed_with_no_row_selected_is_refused_until_discarded(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}, Listing(client)
    )
    thickness = widget_in(stack, 'Shots', 'Thickness')
    empty = thickness.value
    thickness.value = 4.0
    assert missing(stack) == {(('shots',), HOLDS_CHANGES)}
    table(stack, 'Shots').select([0])
    assert table(stack, 'Shots').selection == []
    table(stack, 'Shots').discard()
    assert thickness.value == empty
    assert [s['thickness'] for s in stack.params['shots']] == [2.0]


def test_deselecting_a_row_empties_the_editor(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}, Listing(client)
    )
    thickness = widget_in(stack, 'Shots', 'Thickness')
    empty = thickness.value
    table(stack, 'Shots').select([0])
    table(stack, 'Shots').select([])
    assert thickness.value == empty
    assert [s['thickness'] for s in stack.params['shots']] == [2.0]


def test_a_row_edited_may_be_added_as_a_new_row_instead(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}, Listing(client)
    )
    table(stack, 'Shots').select([0])
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    table(stack, 'Shots').add_row()
    assert table(stack, 'Shots').selection == [1]
    assert [s['thickness'] for s in stack.params['shots']] == [2.0, 4.0]


def test_removing_the_row_selected_removes_the_changes_to_it(
    client: Client, stack: Fields, run: FileRef
) -> None:
    shots = [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}]
    stack.set({'runs': [run], 'shots': shots}, Listing(client))
    table(stack, 'Shots').select([0])
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    table(stack, 'Shots').remove_rows()
    assert [s['thickness'] for s in stack.params['shots']] == [3.0]


def test_setting_the_form_drops_changes_in_the_editor(
    client: Client, stack: Fields, run: FileRef
) -> None:
    params = {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}
    stack.set(params, Listing(client))
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    stack.set(params, Listing(client))
    assert [s['thickness'] for s in stack.params['shots']] == [2.0]


def test_a_refresh_keeps_changes_in_the_editor(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}, Listing(client)
    )
    table(stack, 'Shots').select([0])
    widget_in(stack, 'Shots', 'Thickness').value = 4.0
    stack.refresh(Listing(client))
    assert widget_in(stack, 'Shots', 'Thickness').value == 4.0
    assert missing(stack) == {(('shots', 0), HOLDS_CHANGES)}


def test_a_record_released_while_its_row_is_selected_is_no_change_in_the_editor(
    client: Client, stack: Fields, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    centre = found.ref('centre')
    stack.set(
        {'runs': [run], 'shots': [], 'centres': [centre, {'x': 2.0, 'y': 0.0}]},
        Listing(client),
    )
    table(stack, 'Centres').select([0])
    client.release(found)
    stack.refresh(Listing(client))
    assert stack.params['centres'][0] == centre
    table(stack, 'Centres').select([1])
    assert table(stack, 'Centres').selection == [1]


def test_changes_in_the_editor_of_a_table_in_a_row_are_changes_of_the_row(
    client: Client,
) -> None:
    class Inner(BaseModel):
        x: float

    class Outer(BaseModel):
        inners: list[Inner]

    class OuterParams(BaseModel):
        outers: list[Outer]

    spec: Spec = WorkflowSpec(
        name='outer',
        version=1,
        title='Outer',
        description='Rows that hold rows.',
        params=OuterParams,
        outputs=Value,
    )
    form = form_of(spec, client)
    form.set({'outers': [{'inners': [{'x': 1.0}]}, {'inners': []}]}, Listing(client))
    outer = table(form, 'Outers')
    inner = table(form, 'Inners')
    outer.select([0])
    inner.select([0])
    widget_in(form, 'Outers', 'X').value = 2.0
    outer.select([1])
    assert outer.selection == [0]
    assert missing(form) == {(('outers', 0), HOLDS_CHANGES)}
    table(form, 'Inners').discard()
    outer.select([1])
    assert outer.selection == [1]
    assert form.params == {'outers': [{'inners': [{'x': 1.0}]}, {'inners': []}]}


def test_a_row_the_editor_cannot_show_is_kept_when_selected(
    client: Client, stack: Fields, run: FileRef
) -> None:
    unread = OutputRef(record='f00d', output='centre')
    stack.set({'runs': [run], 'shots': [], 'centres': [unread]}, Listing(client))
    table(stack, 'Centres').select([0])
    assert stack.params['centres'] == [unread]


def test_a_row_is_not_updated_while_a_value_is_missing_in_the_editor(
    client: Client, stack: Fields, run: FileRef
) -> None:
    stack.set(
        {'runs': [run], 'shots': [{'run': run, 'thickness': 2.0}]}, Listing(client)
    )
    table(stack, 'Shots').select([0])
    widget_in(stack, 'Shots', 'Thickness').value = None
    table(stack, 'Shots').update_row()
    assert rows(stack, 'Shots') == [{'Run': 'run 101 (sample)', 'Thickness': '2.0'}]


def test_remove_removes_the_rows_selected(
    client: Client, stack: Fields, run: FileRef
) -> None:
    centres = [{'x': float(x), 'y': 0.0} for x in range(3)]
    stack.set({'runs': [run], 'shots': [], 'centres': centres}, Listing(client))
    table(stack, 'Centres').select([0, 2])
    table(stack, 'Centres').remove_rows()
    assert stack.params['centres'] == [centres[1]]


def test_a_row_may_be_an_output_of_a_record_that_fits(
    client: Client, stack: Fields, find_spec: Spec, run: FileRef
) -> None:
    assert way(stack, 'Centres').offered == ['Given', 'From a record']
    with pytest.raises(TypeError, match='centres: not a choice of ways'):
        stack.ways('centres')
    found = client.compute(find_spec, params={'run': run})
    stack.refresh(Listing(client))
    way(stack, 'Centres').chosen = 'From a record'
    picker(stack, 'Centres').value = found.ref('centre')
    table(stack, 'Centres').add_row()
    table(stack, 'Runs').remove_rows()
    table(stack, 'Shots').remove_rows()
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
    form = form_of(spec, client)
    form.set({'runs': [run, found.ref('centre')]}, Listing(client))
    label = f'BeamCentre ({found.created.astimezone():%H:%M})'
    assert rows(form, 'Runs') == [
        {'File': 'run 101 (sample)', 'Record': ''},
        {'File': '', 'Record': label},
    ]


def test_the_editor_offers_no_record_of_a_row_shown_before(
    client: Client, stack: Fields, find_spec: Spec, run: FileRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    centres = [found.ref('centre'), {'x': 2.0, 'y': 0.0}]
    stack.set({'runs': [run], 'shots': [], 'centres': centres}, Listing(client))
    way(stack, 'Centres').chosen = 'From a record'
    table(stack, 'Centres').add_row()
    table(stack, 'Centres').discard()
    assert stack.params['centres'] == centres


def test_tables_round_trip_through_a_record(
    client: Client,
    files: FakeFiles,
    stack: Fields,
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
    stack.set(record.request.params, Listing(client))
    assert stack.params == record.request.params
    assert rows(stack, 'Shots') == [{'Run': 'run 102 (sample)', 'Thickness': '2.0'}]


def test_a_list_of_a_union_has_a_column_for_the_tag(client: Client) -> None:
    sides = form_of(SIDES, client)
    table(sides, 'Sides').add_row()  # no way chosen: no row
    assert missing(sides) == {(('sides',), 'add a row, or remove for none')}
    for side in ('Right', 'Left'):
        way(sides, 'Sides').chosen = side
        table(sides, 'Sides').add_row()
    assert sides.params == {'sides': [{'kind': 'right'}, {'kind': 'left'}]}
    assert rows(sides, 'Sides') == [{'Kind': 'Right'}, {'Kind': 'Left'}]


@pytest.fixture
def nested(client: Client, nested_spec: Spec) -> Fields:
    return form_of(nested_spec, client)


@pytest.fixture
def found(client: Client, find_spec: Spec, run: FileRef) -> Record:
    return client.compute(find_spec, params={'run': run})


@pytest.fixture
def filled(client: Client, nested: Fields, found: Record, run: FileRef) -> Fields:
    """The nested form with a value at each field without a default."""
    nested.set(
        {
            'bins': 2,
            'specimen': {
                'run': run,
                'window': {'start': 0.5},
                'centre': found.ref('centre'),
            },
        },
        Listing(client),
    )
    return nested


def test_fields_at_any_depth_start_at_their_default_or_with_nothing_given(
    nested: Fields,
) -> None:
    assert missing(nested) == {
        (('bins',), 'give a value'),
        (('specimen', 'run'), 'choose a file'),
        (('specimen', 'window', 'start'), 'give a value'),
        (('specimen', 'centre'), 'choose a record'),
    }


def test_a_scalar_parameter_is_an_input_in_its_tab(filled: Fields) -> None:
    widget_in(filled, 'Bins', 'Bins').value = 3
    assert filled.params['bins'] == 3
    with pytest.raises(TypeError, match='bins: not a choice of ways'):
        filled.ways('bins')


def test_a_group_at_a_field_is_a_card_of_its_fields(filled: Fields) -> None:
    assert not way(filled, 'Window').selector_visible
    assert way(filled, 'Window').offered == ['Given']
    widget_in(filled, 'Frame', 'Start').value = 2.0
    specimen = filled.params['specimen']
    assert specimen['window'] == {'start': 0.5, 'stop': 1.0}
    assert specimen['frame'] == {'start': 2.0, 'stop': 1.0}


def test_a_union_at_a_field_offers_its_alternatives(filled: Fields) -> None:
    assert way(filled, 'Shape').offered == ['Disc', 'Slab']
    assert filled.params['specimen']['shape'] == {'kind': 'disc', 'radius': 1.0}
    way(filled, 'Shape').chosen = 'Slab'
    assert filled.params['specimen']['shape'] == {'kind': 'slab', 'width': 1.0}


def test_x_or_none_at_a_field_is_a_choice_with_none(filled: Fields) -> None:
    for title in ('Scale', 'Crop'):
        assert way(filled, title).offered == ['Given', 'None']
        way(filled, title).chosen = 'Given'
    assert way(filled, 'Mask').offered == ['A file', 'None']
    assert missing(filled) == {
        (('specimen', 'scale'), 'give a value'),
        (('specimen', 'crop', 'start'), 'give a value'),
    }
    widget_in(filled, 'Scale', 'Scale').value = 2.0
    widget_in(filled, 'Crop', 'Start').value = 0.0
    assert filled.params['specimen']['scale'] == 2.0
    assert filled.params['specimen']['crop'] == {'start': 0.0, 'stop': 1.0}


def test_a_record_at_a_field_is_listed_when_the_form_is_refreshed(
    client: Client, nested: Fields, find_spec: Spec, run: FileRef
) -> None:
    assert way(nested, 'Centre').offered == ['From a record']
    assert set(picker(nested, 'Centre').options) == {'(choose a record)'}
    found = client.compute(find_spec, params={'run': run})
    nested.refresh(Listing(client))
    picker(nested, 'Centre').value = found.ref('centre')
    assert (('specimen', 'centre'), 'choose a record') not in missing(nested)


def test_a_list_of_groups_at_a_field_is_a_table(filled: Fields, run: FileRef) -> None:
    assert rows(filled, 'Shots') == []
    widget_in(filled, 'Shots', 'Run').value = run
    table(filled, 'Shots').add_row()
    assert filled.params['specimen']['shots'] == [{'run': run, 'thickness': 1.0}]
    assert rows(filled, 'Shots') == [{'Run': 'run 101 (sample)', 'Thickness': '1.0'}]


def test_a_list_of_a_union_at_a_field_has_a_column_per_field_of_each_alternative(
    filled: Fields,
) -> None:
    for shape in ('Slab', 'Disc'):
        way(filled, 'Shapes').chosen = shape
        table(filled, 'Shapes').add_row()
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
    nested: Fields,
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
    nested.set(record.request.params, Listing(client))
    assert nested.params == record.request.params


def test_watch_tells_of_a_changed_input_and_not_of_set_or_refresh(
    client: Client, reduce_spec: Spec
) -> None:
    form = form_of(reduce_spec, client)
    calls: list[None] = []
    form.watch(lambda: calls.append(None))

    form.set({'reduction': {'bins': 7}}, Listing(client))
    form.refresh(Listing(client))
    assert calls == []

    widget(form, 'Bins').value = 8
    assert len(calls) == 1


def test_watch_tells_of_an_element_added_and_of_its_value(options: Fields) -> None:
    calls: list[None] = []
    options.watch(lambda: calls.append(None))

    section(options, 'Banks').add()
    assert len(calls) == 1

    widget(options, 'Banks [0]').value = 3
    assert len(calls) == 2


def test_watch_tells_of_a_row_written_and_removed(stack: Fields, run: FileRef) -> None:
    calls: list[None] = []
    widget_in(stack, 'Shots', 'Run').value = run
    stack.watch(lambda: calls.append(None))

    table(stack, 'Shots').add_row()
    assert calls
    calls.clear()

    table(stack, 'Shots').remove_rows()
    assert calls


def test_a_form_can_be_limited_to_some_parameters(
    client: Client, reduce_spec: Spec
) -> None:
    fields = ('sample',)
    settings = [n for n in reduce_spec.params.model_fields if n not in fields]

    row = Fields(reduce_spec.params, Listing(client), fields=fields)
    rest = Fields(reduce_spec.params, Listing(client), fields=settings)

    assert set(row.held()[0]) | {e.loc[0] for e in row.held()[1]} == {'sample'}
    assert 'sample' not in rest.held()[0]


def test_a_form_refuses_a_field_the_model_lacks(
    client: Client, reduce_spec: Spec
) -> None:
    with pytest.raises(KeyError, match='nope'):
        Fields(reduce_spec.params, Listing(client), fields=('nope',))


def test_a_limited_form_ignores_the_other_parameters_of_a_record(
    client: Client, reduce_spec: Spec
) -> None:
    row = Fields(reduce_spec.params, Listing(client), fields=('sample',))

    row.set({'sample': {'thickness': 2.0}, 'run': 'not shown'}, Listing(client))

    assert row.params['sample']['thickness'] == 2.0


def test_held_leaves_out_what_has_something_missing(
    client: Client, reduce_spec: Spec
) -> None:
    form = form_of(reduce_spec, client)

    given, missing = form.held()

    assert 'run' not in given
    assert ('run',) in [e.loc for e in missing]
    with pytest.raises(SubmitError):
        form.params


def picked(hour: int) -> datetime:
    """What a time picker gives: no zone, to the second."""
    return datetime(2026, 3, 4, hour, 6, 7)  # noqa: DTZ001


@pytest.mark.parametrize(
    ('given', 'label', 'edited'),
    [
        ('2026-03-04T05:06:07Z', 'At (UTC)', '2026-03-04T06:06:07Z'),
        ('2026-03-04T05:06:07+02:00', 'At (UTC+02:00)', '2026-03-04T06:06:07+02:00'),
        ('2026-03-04T05:06:07', 'At (no time zone)', '2026-03-04T06:06:07'),
        ('2026-03-04T05:06:07.123Z', 'At (UTC)', '2026-03-04T06:06:07Z'),
    ],
)
def test_a_time_edited_keeps_its_zone(
    client: Client, times_and_lists: Fields, given: str, label: str, edited: str
) -> None:
    times = {**GIVEN_TIMES, 'at': given}
    times_and_lists.set({'times': times, 'lists': {'banks': []}}, Listing(client))
    time = widget(times_and_lists, label)
    assert time.value.replace(microsecond=0) == picked(5)
    time.value = picked(6)
    assert times_and_lists.params['times'] == {**times, 'at': edited}


def test_a_time_unchanged_keeps_its_fraction_of_a_second(
    client: Client, times_and_lists: Fields
) -> None:
    times = {**GIVEN_TIMES, 'at': '2026-03-04T05:06:07.123Z'}
    times_and_lists.set({'times': times, 'lists': {'banks': []}}, Listing(client))
    widget(times_and_lists, 'At (UTC)').value = picked(5)
    assert times_and_lists.params['times'] == {
        **times,
        'at': '2026-03-04T05:06:07.123000Z',
    }


def test_a_time_without_a_value_is_in_the_zone_of_its_type(
    client: Client, times_and_lists: Fields
) -> None:
    times_and_lists.set({'lists': {'banks': []}}, Listing(client))
    widget(times_and_lists, 'At (no time zone)').value = picked(5)
    widget(times_and_lists, 'Naive (no time zone)').value = picked(5)
    widget(times_and_lists, 'Aware (UTC)').value = picked(5)
    assert times_and_lists.params['times'] == GIVEN_TIMES


def test_a_value_of_another_type_is_refused_and_the_last_stays(
    client: Client, options: Fields
) -> None:
    options.set({'settings': {'flag': False, 'count': 1}}, Listing(client))

    with pytest.raises(ValueError, match='abc'):
        options.set({'settings': {'flag': False, 'count': 'abc'}}, Listing(client))

    assert widget(options, 'Count').value == 1


def test_a_value_a_picker_does_not_list_leaves_it_unset(
    client: Client, options: Fields
) -> None:
    options.set(
        {'settings': {'flag': False, 'count': 1, 'mode': 'nope'}}, Listing(client)
    )

    assert (('settings', 'mode'), 'give a value') in missing(options)


def test_a_value_that_no_way_writes_is_refused(
    client: Client, subtract: Fields
) -> None:
    with pytest.raises(ValueError, match='not one of'):
        subtract.set(
            {'background': {'scale': 1.0}, 'centre': FileRef(file='uuid:1')},
            Listing(client),
        )


def test_update_with_no_row_selected_adds_nothing(stack: Fields, run: FileRef) -> None:
    table(stack, 'Shots').remove_rows()
    widget_in(stack, 'Shots', 'Run').value = run

    table(stack, 'Shots').update_row()

    assert rows(stack, 'Shots') == []


def test_watch_stays_silent_when_nothing_held_changes(
    client: Client, stack: Fields, run: FileRef
) -> None:
    shots = [{'run': run, 'thickness': 2.0}, {'run': run, 'thickness': 3.0}]
    stack.set({'runs': [run], 'shots': shots}, Listing(client))
    calls: list[None] = []
    stack.watch(lambda: calls.append(None))

    table(stack, 'Shots').select([1])
    table(stack, 'Shots').discard()
    table(stack, 'Shots').select([])
    table(stack, 'Shots').add_row()  # refused: the editor has nothing given

    assert calls == []


def test_the_fields_import_no_widget_library() -> None:
    code = 'import ess.apps.fields, sys; print(" ".join(sys.modules))'
    done = subprocess.run(  # noqa: S603
        [sys.executable, '-c', code], capture_output=True, text=True, check=True
    )
    assert not set(done.stdout.split()) & {'panel', 'bokeh'}

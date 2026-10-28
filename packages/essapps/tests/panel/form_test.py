# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from typing import Any

import panel as pn
import pytest
from pydantic import BaseModel

from ess.apps import Client, SubmitError
from ess.apps.panel import Form, Listing
from ess.apps.testing import FakeDatasets
from ess.spec import DatasetModel, DatasetRef, WorkflowSpec

type Spec = WorkflowSpec[Any, Any]


class Raw(DatasetModel):
    """Any raw dataset."""


class LokiMask(DatasetModel, kind='mask', instrument='loki'):
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


def card(form: Form, title: str) -> Any:
    """The box titled ``title``."""
    (found,) = (c for c in form.panel().select(pn.Card) if c.title == title)
    return found


def way(form: Form, title: str) -> Any:
    """The radio buttons of the ways of the box titled ``title``."""
    return card(form, title).objects[0]


def picker(form: Form, title: str) -> Any:
    """The dataset picker of the box titled ``title``."""
    (found,) = card(form, title).select(pn.widgets.Select)
    return found


def missing(form: Form) -> set[tuple[tuple[str | int, ...], str]]:
    """Each place where the form has nothing given, with what to do there."""
    with pytest.raises(SubmitError) as refused:
        form.params
    return {(e.loc, e.msg) for e in refused.value.errors}


def test_params_are_the_defaults_and_what_is_given(form: Form, run: DatasetRef) -> None:
    form.set({'sample': {'file': run, 'thickness': 2.0}})
    assert form.params == {
        'reduction': {'bins': 2, 'norm': 'monitor'},
        'sample': {'file': run, 'thickness': 2.0},
        'beam_centre': {'x': 0.0, 'y': 0.0},
        'detector': {'kind': 'front', 'distance': 1.0},
        'mask': None,
    }


def test_an_input_without_a_default_starts_with_nothing_given(form: Form) -> None:
    assert missing(form) == {
        (('sample', 'file'), 'choose a dataset'),
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
        },
        'direct_beam': None,
    }
    options.set(params)
    record = client.compute(options_spec, params=options.params)
    assert client.failure(record) is None
    assert record.request.params == params


def test_a_cleared_input_of_a_value_with_a_default_is_not_given_the_default(
    form: Form, options: Form, run: DatasetRef
) -> None:
    form.set({'sample': {'file': run, 'thickness': 2.0}})
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


def test_a_dataset_model_typed_x_or_none_offers_a_dataset_and_none(
    options: Form,
) -> None:
    assert options.ways('direct_beam') == ['A dataset', 'None']
    options.set({'settings': {'flag': False, 'count': 1}, 'direct_beam': None})
    assert options.params['direct_beam'] is None


def test_a_group_typed_x_or_none_in_a_group_without_a_default_starts_unset(
    client: Client, subtract: Form, subtract_spec: Spec
) -> None:
    assert way(subtract, 'Centre').options == ['Given', 'None']
    assert way(subtract, 'Centre').value is None
    assert subtract.params == {'background': {'scale': 1.0}}
    with pytest.raises(SubmitError) as refused:
        client.submit(subtract_spec, params=subtract.params)
    assert [e.loc for e in refused.value.errors] == [('background', 'centre')]


def test_a_group_typed_x_or_none_in_a_group_is_given_each_way(
    client: Client,
    subtract: Form,
    subtract_spec: Spec,
    find_spec: Spec,
    run: DatasetRef,
) -> None:
    found = client.compute(find_spec, params={'run': run})
    subtract.refresh()
    assert way(subtract, 'Centre').options == ['Given', 'From a record', 'None']

    for centre in (None, {'x': 2.0, 'y': 0.0}, found.ref('centre')):
        params = {'background': {'scale': 1.0, 'centre': centre}}
        subtract.set(params)
        assert subtract.params == params
        record = client.compute(subtract_spec, params=subtract.params)
        assert client.failure(record) is None
        assert record.request.params == params


def test_a_union_offers_its_alternatives_and_no_record(
    client: Client, form: Form, find_spec: Spec, run: DatasetRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.refresh()
    assert form.ways('detector') == ['Front', 'Rear bank']


def test_set_refuses_a_tag_of_no_alternative(form: Form) -> None:
    with pytest.raises(ValueError, match='side: not an alternative'):
        form.set({'detector': {'kind': 'side'}})


def test_a_group_offers_a_record_while_a_fitting_output_is_readable(
    client: Client, form: Form, find_spec: Spec, reduce_spec: Spec, run: DatasetRef
) -> None:
    assert form.ways('beam_centre') == ['Given']

    client.compute(reduce_spec, params={'sample': {'file': run, 'thickness': 2.0}})
    form.refresh()
    assert form.ways('beam_centre') == ['Given']  # its output does not fit

    found = client.compute(find_spec, params={'run': run})
    form.refresh()
    assert form.ways('beam_centre') == ['Given', 'From a record']

    client.release(found)
    form.refresh()
    assert form.ways('beam_centre') == ['Given']


def test_a_group_that_holds_a_file_offers_no_record(form: Form) -> None:
    assert form.ways('sample') == ['Given']


def test_the_way_of_a_record_with_no_record_picked_is_not_given_the_default(
    client: Client, form: Form, find_spec: Spec, run: DatasetRef
) -> None:
    client.compute(find_spec, params={'run': run})
    form.set({'sample': {'file': run, 'thickness': 2.0}})
    way(form, 'Beam Centre').value = 'From a record'
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_a_picked_record_that_is_released_is_not_given_the_default(
    client: Client, form: Form, find_spec: Spec, run: DatasetRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    form.set(
        {'sample': {'file': run, 'thickness': 2.0}, 'beam_centre': found.ref('centre')}
    )
    assert form.params['beam_centre'] == found.ref('centre')

    client.release(found)
    form.refresh()
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_with_a_reference_the_client_may_not_read_picks_no_record(
    client: Client, form: Form, find_spec: Spec, run: DatasetRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    client.release(found)
    form.set(
        {'sample': {'file': run, 'thickness': 2.0}, 'beam_centre': found.ref('centre')}
    )
    assert missing(form) == {(('beam_centre',), 'choose a record')}


def test_set_shows_params_as_a_record_holds_them(
    client: Client,
    datasets: FakeDatasets,
    form: Form,
    find_spec: Spec,
    reduce_spec: Spec,
    run: DatasetRef,
) -> None:
    found = client.compute(find_spec, params={'run': run})
    mask = datasets.measure(201, {'counts': 2.0}, kind='mask')
    record = client.compute(
        reduce_spec,
        params={
            'reduction': {'bins': 4, 'norm': 'none'},
            'sample': {'file': run, 'thickness': 2.0},
            'beam_centre': found.ref('centre'),
            'detector': {'kind': 'rear', 'tilt': 0.5},
            'mask': mask,
        },
    )
    assert client.output(record, 'total').value == 2.0
    form.set(record.request.params)
    assert form.params == record.request.params
    assert form.ways('beam_centre') == ['Given', 'From a record']


def test_the_dataset_picker_lists_a_dataset_measured_after_the_form_was_made(
    datasets: FakeDatasets, form: Form, run: DatasetRef
) -> None:
    form.refresh()
    assert list(picker(form, 'File').options) == [
        '(choose a dataset)',
        'run 101 (sample)',
    ]

    datasets.measure(102, {'counts': 1.0, 'x': 0.0}, role='sample')
    form.refresh()
    assert list(picker(form, 'File').options) == [
        '(choose a dataset)',
        'run 101 (sample)',
        'run 102 (sample)',
    ]


def test_a_dataset_picker_lists_the_datasets_that_fit_its_model(
    datasets: FakeDatasets, form: Form, run: DatasetRef
) -> None:
    datasets.measure(201, {'counts': 1.0}, kind='mask')
    form.refresh()
    assert form.ways('mask') == ['A dataset', 'None']
    assert list(picker(form, 'File').options) == [
        '(choose a dataset)',
        'run 101 (sample)',
    ]
    assert list(picker(form, 'Mask').options) == ['(choose a dataset)', 'run 201']


def test_a_dataset_picker_lists_the_datasets_that_fit_each_key_of_its_metadata(
    client: Client, datasets: FakeDatasets
) -> None:
    datasets.measure(201, {'counts': 1.0}, kind='mask', instrument='dream')
    datasets.measure(202, {'counts': 1.0}, kind='mask', instrument='loki')
    form = Form(LOKI_MASK, client)
    assert list(picker(form, 'Mask').options) == ['(choose a dataset)', 'run 202']


def test_a_dataset_that_does_not_fit_stays_picked_when_set(
    datasets: FakeDatasets, form: Form, run: DatasetRef
) -> None:
    mask = datasets.measure(201, {'counts': 1.0}, kind='mask')
    form.set({'sample': {'file': mask, 'thickness': 2.0}})
    assert picker(form, 'File').options == {
        '(choose a dataset)': None,
        'run 101 (sample)': run,
        str(mask): mask,
    }
    assert form.params['sample'] == {'file': mask, 'thickness': 2.0}


def test_a_picked_dataset_that_no_longer_fits_stays_picked(
    datasets: FakeDatasets, form: Form, run: DatasetRef
) -> None:
    form.set({'sample': {'file': run, 'thickness': 2.0}})
    datasets.correct(run, kind='mask')
    form.refresh()
    assert list(picker(form, 'File').options) == ['(choose a dataset)', str(run)]
    assert form.params['sample'] == {'file': run, 'thickness': 2.0}


def test_a_listing_labels_each_record_and_the_outputs_that_fit(
    client: Client, find_spec: Spec, run: DatasetRef
) -> None:
    found = client.compute(find_spec, params={'run': run})
    listing = Listing(client)
    label = f'{found.id[:6]} Find centre, from run 101'
    assert listing.labels == {found.id: label}
    centre = find_spec.outputs.model_fields['centre'].annotation
    assert listing.fitting(centre) == {f'{label}: centre': found.ref('centre')}
    assert listing.datasets(Raw) == {'run 101 (sample)': run}


def test_a_dataset_without_a_run_is_labelled_by_its_identity(
    client: Client, datasets: FakeDatasets, run: DatasetRef
) -> None:
    datasets.correct(run, run=None)
    assert Listing(client).datasets(Raw) == {f'{run} (sample)': run}

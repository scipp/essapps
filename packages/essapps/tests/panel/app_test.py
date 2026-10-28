# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import pytest

from ess.apps import Client
from ess.apps.panel import App, Listing
from ess.spec import FileRef


@pytest.fixture
def app(client: Client) -> App:
    return App(client)


def test_the_app_has_a_form_per_spec_and_shows_the_one_picked(app: App) -> None:
    assert list(app.forms) == ['Find centre', 'Reduce', 'Options', 'Subtract']
    assert app.form is app.forms['Find centre']
    app.picker.value = 'Reduce'
    assert app.form is app.forms['Reduce']
    app.panel()  # builds, with the warnings of Panel as errors


def test_a_place_with_nothing_given_is_shown_and_nothing_is_submitted(
    app: App, client: Client
) -> None:
    app.picker.value = 'Reduce'

    assert app.compute() is None
    assert app.message.object == (
        'Refused:\n\n- run: choose a file\n- sample.thickness: give a value'
    )
    assert client.records() == []


def test_a_refused_request_shows_each_reason_by_its_place(app: App) -> None:
    app.picker.value = 'Options'
    app.form.set({'settings': {'flag': True, 'count': 1}})

    assert app.compute() is None
    assert app.message.object == (
        'Refused:\n\n- direct_beam: a required parameter, not given'
    )


def test_a_failed_record_shows_why(app: App, run: FileRef) -> None:
    app.picker.value = 'Reduce'
    app.form.set({'run': run, 'sample': {'thickness': 0.0}})

    assert app.compute() is not None
    assert app.message.object == 'Failed: ZeroDivisionError: float division by zero'


def test_a_computed_record_is_shown_and_offered_by_the_forms(
    app: App, client: Client, run: FileRef
) -> None:
    app.form.set({'run': run})

    record = app.compute()
    assert record is not None
    label = Listing(client).labels[record.id]
    assert app.message.object == f'Computed {label}.'
    assert label in app.results.object
    assert app.forms['Reduce'].ways('beam_centre') == ['Given', 'From a record']

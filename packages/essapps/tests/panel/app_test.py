# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import panel as pn
import pytest

from ess.apps import Client, Record
from ess.apps.panel import App, Listing
from ess.apps.testing import CountingClient
from ess.spec import FileRef


class CountingApp(App):
    """An app that counts how often it lists the changed records again."""

    refreshes = 0

    def refresh(self) -> Listing:
        self.refreshes += 1
        return super().refresh()


@pytest.fixture
def app(client: Client) -> App:
    return App(client)


def finish(app: App, record: Record | None) -> None:
    """Wait for ``record``, then poll, as the app's periodic callback does."""
    assert record is not None
    app.client.wait(record)
    app.poll()


def test_the_app_has_a_form_per_spec_and_shows_the_one_picked(app: App) -> None:
    assert list(app.forms) == [
        'Find centre',
        'Reduce',
        'Options',
        'Subtract',
        'Stack',
        'Nested',
    ]
    assert app.form is app.forms['Find centre']
    app.picker.value = 'Reduce'
    assert app.form is app.forms['Reduce']
    app.panel()  # builds, with the warnings of Panel as errors


def test_a_place_with_nothing_given_is_shown_and_nothing_is_submitted(
    app: App, client: Client
) -> None:
    app.picker.value = 'Reduce'

    assert app.submit() is None
    assert app.message.object == (
        'Refused:\n\n- run: choose a file\n- sample.thickness: give a value'
    )
    assert client.records() == []


def test_a_refused_request_shows_each_reason_by_its_place(app: App) -> None:
    app.picker.value = 'Options'
    app.form.set({'settings': {'flag': True, 'count': 1}})

    assert app.submit() is None
    assert app.message.object == (
        'Refused:\n\n- direct_beam: a required parameter, not given'
    )


def test_a_failed_record_shows_why(app: App, run: FileRef) -> None:
    app.picker.value = 'Reduce'
    app.form.set({'run': run, 'sample': {'thickness': 0.0}})

    finish(app, app.submit())
    assert app.message.object == 'Failed: ZeroDivisionError: float division by zero'


def test_a_computed_record_is_listed_and_offered_by_the_forms(
    app: App, client: Client, run: FileRef
) -> None:
    app.form.set({'run': run})

    record = app.submit()
    finish(app, record)
    assert record is not None
    label = Listing(client).labels[record.id]
    assert app.message.object == f'Computed {label}.'
    assert app.records.table.widget.value['Record'].tolist() == [label]
    assert app.forms['Reduce'].ways('beam_centre') == ['Given', 'From a record']


def test_a_record_of_another_client_appears_on_a_poll(
    app: App, client: Client, other: Client, run: FileRef
) -> None:
    record = other.submit(app.form.spec, params={'run': run})

    app.poll()

    label = Listing(client).labels[record.id]
    assert app.records.table.widget.value['Record'].tolist() == [label]


def test_a_poll_without_a_change_reads_no_records_and_lists_nothing_again(
    client: Client, run: FileRef
) -> None:
    counting = CountingClient(client)
    app = CountingApp(counting)  # type: ignore[arg-type]
    app.form.set({'run': run})
    finish(app, app.submit())
    counting.reads.clear()
    refreshes = app.refreshes

    app.poll()

    assert dict(counting.reads) == {}
    assert app.refreshes == refreshes


def test_a_refresh_after_one_change_reads_only_that_record(
    client: Client, other: Client, run: FileRef
) -> None:
    counting = CountingClient(client)
    app = App(counting)  # type: ignore[arg-type]
    other.compute(app.form.spec, params={'run': run})
    other.compute(app.form.spec, params={'run': run})
    app.refresh()
    counting.reads.clear()
    other.submit(app.form.spec, params={'run': run})

    app.refresh()

    assert dict(counting.reads) == {'records': 1, 'status': 1, 'readable': 1}


def test_a_released_output_is_no_longer_offered(
    app: App, client: Client, run: FileRef
) -> None:
    app.form.set({'run': run})
    found = client.compute(app.form.spec, params={'run': run})
    app.refresh()

    def offered() -> list[object]:
        pickers = app.forms['Reduce'].panel().select(pn.widgets.Select)
        return [v for p in pickers for v in p.options.values()]

    assert found.ref('centre') in offered()

    app.outputs.select(found, 'centre')
    app.outputs.remove()

    assert found.ref('centre') not in offered()

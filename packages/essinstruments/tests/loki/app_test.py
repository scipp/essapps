# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The application through its widgets, on the files of esssans' tutorial, in the
order a person uses it: masks, beam centre, a batch, and one run tweaked.

The reductions of the numerator of I(Q) are counted by a binding that wraps the
one of LoKI, so that a test tells how often the can's was computed.
"""

from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import panel as pn
import pytest
import scipp as sc
from ess.instruments.loki.app import LokiApp
from ess.instruments.loki.batch import Row, latest_results, rows
from ess.instruments.loki.bindings import (
    beam_centre_binding,
    iofq_binding,
    masks_binding,
)
from ess.instruments.loki.plots import PLOTS
from ess.instruments.loki.specs import IOFQ
from ess.sans.normalization import reduce_q
from ess.sans.types import (
    BackgroundRun,
    NormalizedQ,
    Numerator,
    ProcessedWavelengthBands,
    ReducedQ,
    SampleRun,
)

from ess.apps import Client, FolderFiles, Listing, Record, SubmitError, file, local
from ess.spec import FileRef
from ess.spec.pipeline import PipelineBinding

pn.extension('tabulator')

NAMES = ['60339', 'ISIS polymer', 'AgBeh', 'porous silica']
"""The rows of the table below: the first has no transmission run."""


def counting_iofq(reductions: Counter[str]) -> PipelineBinding[Any, Any]:
    """LoKI's binding of I(Q), counting how often the numerators are reduced."""

    def reduce_sample(
        data: NormalizedQ[SampleRun, Numerator], bands: ProcessedWavelengthBands
    ) -> ReducedQ[SampleRun, Numerator]:
        reductions['sample'] += 1
        return reduce_q(data, bands)

    def reduce_can(
        data: NormalizedQ[BackgroundRun, Numerator], bands: ProcessedWavelengthBands
    ) -> ReducedQ[BackgroundRun, Numerator]:
        reductions['can'] += 1
        return reduce_q(data, bands)

    def make_pipeline(fixed: Mapping[str, Any]) -> Any:
        pipeline = iofq_binding.pipeline(fixed)
        pipeline.insert(reduce_sample)
        pipeline.insert(reduce_can)
        return pipeline

    return PipelineBinding(IOFQ, make_pipeline, keys=iofq_binding.keys)


class ClientLog:
    """
    Delegates to a client, and logs the stages it opens and releases. It
    refuses the requests of the sample runs in ``refuse``, each with its reason.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self.opened: list[Any] = []
        self.released: list[Any] = []
        self.refuse: dict[FileRef, str] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def stage(self, template: Any) -> Any:
        stage = self._client.stage(template)
        self.opened.append(stage)
        return stage

    def release(self, what: Any) -> None:
        self.released.append(what)
        self._client.release(what)

    def submit(self, what: Any, *, params: Any = None, **kwargs: Any) -> Any:
        if params is not None and (reason := self.refuse.get(params.get('sample'))):
            raise SubmitError.at(('sample',), reason)
        return self._client.submit(what, params=params, **kwargs)


@dataclass
class Session:
    app: LokiApp
    client: ClientLog
    reductions: Counter[str]
    masks: Record
    centre: Record
    settings: dict[str, Any]

    @property
    def batch(self) -> Any:
        return self.app.batch

    def run(self, name: str) -> FileRef:
        """The sample run of the row ``name``."""
        return self.row(name).sample

    def row(self, name: str) -> Row:
        (row,) = (r for r in rows(self.client.files) if r.name == name)
        return row

    def finish(self) -> None:
        """Wait for every record, then poll, as the app's periodic callback does."""
        self.client.wait(self.client.records())
        self.app.app.poll()

    def statuses(self) -> dict[str, str]:
        """The status shown for each row."""
        table = self.batch.table.widget.value
        return dict(zip(table['Sample'], table['I(Q)'], strict=True))

    def result(self, name: str) -> Record:
        return latest_results(Listing(self.client))[self.run(name)]

    def press(self, button: Any) -> None:
        button.clicks += 1
        self.finish()

    def select(self, name: str) -> None:
        self.batch.table.select(self.run(name))


@pytest.fixture
def tutorial(sample: Path, mask_file: Path, direct_beam: Path) -> FolderFiles:
    def run(number: int, role: str, name: str | None = None) -> dict[str, object]:
        row = {'file': f'{number}-2022-02-28_2215.nxs', 'run': number, 'role': role}
        return row | ({} if name is None else {'sample': name})

    table = [
        run(60339, 'sample'),
        run(60395, 'sample', 'ISIS polymer'),
        run(60394, 'transmission', 'ISIS polymer'),
        run(60387, 'sample', 'AgBeh'),
        run(60386, 'transmission', 'AgBeh'),
        run(60385, 'sample', 'porous silica'),
        run(60384, 'transmission', 'porous silica'),
        run(60393, 'background'),
        run(60392, 'transmission'),
        {'file': mask_file.name, 'kind': 'mask'},
        {'file': direct_beam.name, 'kind': 'calibration', 'role': 'direct_beam'},
    ]
    return FolderFiles(sample.parent, table)


@pytest.fixture
def session(
    tutorial: FolderFiles, mask_file: Path, direct_beam: Path
) -> Iterator[Session]:
    """The app with the masks and the beam centre of run 60339 computed."""
    reductions: Counter[str] = Counter()
    bind = [masks_binding, beam_centre_binding, counting_iofq(reductions)]
    with local(proposal='p1', files=tutorial, bind=bind) as local_client:
        client = ClientLog(local_client)
        app = LokiApp(client)  # type: ignore[arg-type]
        generic = app.app

        def submit(title: str, params: dict[str, Any]) -> Record:
            generic.picker.value = title
            generic.form.set(params)
            record = generic.submit()
            assert record is not None
            client.wait(record)
            generic.poll()
            return record

        masks = submit(
            'Pixel masks',
            {'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
        )
        centre = submit(
            'Beam centre', {'sample': file(run=60339), 'masks': masks.ref('masks')}
        )
        settings = {
            'can': file(run=60393),
            'can_transmission': file(run=60392),
            'empty_beam': file(run=60392),
            'direct_beam': file(path=direct_beam),
            'masks': masks.ref('masks'),
            'beam_centre': centre.ref('centre'),
            'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
            'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
        }
        yield Session(app, client, reductions, masks, centre, settings)


def choice(form: Any, label: str) -> Any:
    """The picker of a form labelled ``label``."""
    (found,) = (w for w in form.panel().select(pn.widgets.Select) if w.name == label)
    return found


def test_the_batch_lists_a_row_for_each_sample_run(session: Session) -> None:
    table = session.batch.table.widget.value
    assert table.to_dict('records') == [
        {'Sample': '60339', 'Run': 'run 60339', 'Transmission': 'none', 'I(Q)': ''},
        {
            'Sample': 'ISIS polymer',
            'Run': 'run 60395',
            'Transmission': 'run 60394',
            'I(Q)': '',
        },
        {
            'Sample': 'AgBeh',
            'Run': 'run 60387',
            'Transmission': 'run 60386',
            'I(Q)': '',
        },
        {
            'Sample': 'porous silica',
            'Run': 'run 60385',
            'Transmission': 'run 60384',
            'I(Q)': '',
        },
    ]


def test_masks_then_centre_then_a_batch_then_one_run_tweaked(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)

    session.press(batch.reduce_button)

    assert session.statuses() == {
        '60339': '',
        'ISIS polymer': 'completed',
        'AgBeh': 'completed',
        'porous silica': 'completed',
    }
    assert batch.message.object == 'Submitted 3 runs.'
    first = session.result('AgBeh')
    session.select('AgBeh')
    session.press(batch.edit_button)
    assert batch.tweak.params == first.request.params

    choice(batch.tweak, 'Gravity').value = False
    session.press(batch.submit_button)

    second = session.result('AgBeh')
    assert second.id != first.id
    assert second.request.params['corrections']['gravity'] is False
    assert session.statuses()['AgBeh'] == 'completed'
    assert not sc.identical(
        session.client.output(first, 'iofq').intensity,
        session.client.output(second, 'iofq').intensity,
    )
    other = session.result('ISIS polymer')
    assert other.request.params['corrections']['gravity'] is True


def test_the_plot_shows_the_latest_result_of_each_row_by_its_name(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)

    (pane,) = batch.view.objects
    (axes,) = pane.object.axes
    assert sorted(t.get_text() for t in axes.get_legend().get_texts()) == [
        'AgBeh',
        'ISIS polymer',
        'porous silica',
    ]
    assert axes.get_yscale() == 'log'


def test_the_plot_is_drawn_again_only_if_the_results_or_the_axis_changed(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)
    (drawn,) = batch.view.objects

    session.app.app.poll()
    batch.refresh(Listing(session.client))
    assert batch.view.objects == [drawn]

    batch.log.value = False
    (linear,) = batch.view.objects
    assert linear is not drawn
    assert linear.object.axes[0].get_yscale() == 'linear'

    session.select('AgBeh')
    session.press(batch.edit_button)
    session.press(batch.submit_button)
    (changed,) = batch.view.objects
    assert changed is not linear


def test_a_result_that_cannot_be_read_is_shown_as_text(session: Session) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)
    session.client.release(session.result('AgBeh'))

    batch.log.value = False

    (text,) = batch.view.objects
    assert isinstance(text, pn.pane.Markdown)
    assert text.object.startswith('Cannot read I(Q)')


def test_the_can_is_reduced_once_for_the_whole_batch(session: Session) -> None:
    batch = session.batch
    batch.settings.set(session.settings)

    session.press(batch.reduce_button)

    assert session.reductions == {'sample': 3, 'can': 1}
    session.select('AgBeh')
    session.press(batch.edit_button)
    session.press(batch.submit_button)  # by itself, so it reduces the can again
    assert session.reductions == {'sample': 4, 'can': 2}


def test_a_result_released_and_listed_leaves_the_other_curves_on_the_plot(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)
    session.client.release(session.result('AgBeh'))

    batch.refresh(Listing(session.client))

    (figure,) = batch.view.objects
    assert isinstance(figure, pn.pane.Matplotlib)
    legend = figure.object.axes[0].get_legend()
    assert {text.get_text() for text in legend.get_texts()} == {
        'ISIS polymer',
        'porous silica',
    }


def test_the_stage_is_kept_for_unchanged_settings_and_replaced_for_changed_ones(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)
    (first,) = session.client.opened
    assert session.reductions['can'] == 1

    session.press(batch.reduce_button)

    assert session.client.opened == [first]
    assert session.client.released == []
    assert session.reductions['can'] == 1

    batch.settings.set(
        {**session.settings, 'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 50}}
    )
    session.press(batch.reduce_button)

    assert session.client.released == [first]
    assert len(session.client.opened) == 2
    assert session.reductions['can'] == 2
    assert session.result('AgBeh').request.params['q']['num_bins'] == 50


def test_a_row_the_backend_refuses_shows_why_and_nothing_further_is_submitted(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.client.refuse[session.run('AgBeh')] = 'not today'

    session.press(batch.reduce_button)

    assert batch.message.object == 'Refused:\n\n- AgBeh: sample: not today'
    assert session.statuses() == {
        '60339': '',
        'ISIS polymer': 'completed',
        'AgBeh': '',
        'porous silica': '',
    }


def test_after_a_refused_row_the_next_batch_opens_a_new_stage(session: Session) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.client.refuse[session.run('AgBeh')] = 'not today'
    session.press(batch.reduce_button)
    (first,) = session.client.opened
    assert session.client.released == [first]
    session.client.refuse.clear()

    session.press(batch.reduce_button)

    assert len(session.client.opened) == 2
    assert session.statuses()['AgBeh'] == 'completed'


def test_settings_with_nothing_given_are_refused_and_nothing_is_submitted(
    session: Session,
) -> None:
    session.press(session.batch.reduce_button)

    assert session.batch.message.object.startswith('Refused:\n\n- can: choose a file')
    assert session.client.opened == []
    assert session.statuses() == dict.fromkeys(NAMES, '')


def test_editing_a_row_without_a_result_shows_the_settings_and_its_files(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    assert batch.edit_button.disabled

    session.select('AgBeh')
    assert not batch.edit_button.disabled
    session.press(batch.edit_button)

    row = session.row('AgBeh')
    assert batch.tweak.params == {
        **batch.settings.params,
        'sample': row.sample,
        'sample_transmission': row.transmission,
    }


def test_editing_a_row_without_a_transmission_run_leaves_that_file_to_choose(
    session: Session,
) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.select('60339')

    session.press(batch.edit_button)

    with pytest.raises(SubmitError, match='sample_transmission: choose a file'):
        batch.tweak.params
    session.press(batch.submit_button)
    assert batch.message.object.startswith('Refused:')
    assert session.statuses() == dict.fromkeys(NAMES, '')


def test_the_plots_of_loki_draw_the_outputs_of_real_records(session: Session) -> None:
    batch = session.batch
    batch.settings.set(session.settings)
    session.press(batch.reduce_button)
    outputs = session.app.app.outputs
    iofq = session.result('AgBeh')
    drawn = {}
    for record, name, view in [
        (session.masks, 'image', 'Masks on image'),
        (session.centre, 'image', 'Centre on image'),
        (iofq, 'iofq', 'I(Q)'),
        (iofq, 'transmission', 'Transmission'),
    ]:
        outputs.select(record, name)
        outputs.views.value = view
        (pane,) = outputs.view.objects
        assert isinstance(pane, pn.pane.Matplotlib), pane.object
        drawn[view] = pane.object.axes[0]

    assert any(line.get_marker() == '+' for line in drawn['Centre on image'].lines)
    assert drawn['I(Q)'].get_yscale() == 'log'
    legend = drawn['Transmission'].get_legend()
    assert [t.get_text() for t in legend.get_texts()] == ['sample', 'can']
    assert set(PLOTS) == {r.spec for r in (session.masks, session.centre, iofq)}

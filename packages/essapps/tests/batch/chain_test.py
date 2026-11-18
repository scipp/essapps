# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A batch with a chain of steps: a detector loaded from each sample run, a centre
found on it, and an I(Q) that reads the centre, as LoKI's three specs do.
"""

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from pydantic import BaseModel

from ess.apps import Client, Workspace, file
from ess.apps.batch import Batch, FileInfo, FolderDefinitions, Row
from ess.apps.batch.auto import Automatic
from ess.apps.errors import SubmitError
from ess.apps.testing import FakeFiles, read_file
from ess.spec import Referenced, RunFile, WorkflowSpec, function_binding


class Image(BaseModel):
    value: float


class Centre(BaseModel):
    x: float


class Total(BaseModel):
    value: float


class DetectorParams(BaseModel):
    run: RunFile


class DetectorOutputs(BaseModel):
    detector: Image


class CentreParams(BaseModel):
    detector: Referenced[Image]
    masks: float = 0.0


class CentreOutputs(BaseModel):
    centre: Centre


class IofqParams(BaseModel):
    sample: RunFile
    sample_transmission: RunFile
    beam_centre: Centre | Referenced[Centre]
    q: int = 10


class IofqOutputs(BaseModel):
    total: Total


class NormParams(BaseModel):
    total: Referenced[Total]
    thickness: float


class NormOutputs(BaseModel):
    normalised: float


def _spec(name: str, params: type, outputs: type) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description='A toy.',
        params=params,
        outputs=outputs,
    )


DETECTOR = _spec('detector', DetectorParams, DetectorOutputs)
CENTRE = _spec('centre', CentreParams, CentreOutputs)
IOFQ = _spec('iofq', IofqParams, IofqOutputs)
NORM = _spec('norm', NormParams, NormOutputs)

NO_CENTRE = 13.0
"""A file content for which the centre is not found."""


def _detector(params: DetectorParams) -> DetectorOutputs:
    value = read_file(params.run)
    if value < 0:
        raise ValueError('bad file')
    return DetectorOutputs(detector=Image(value=value))


def _centre(params: CentreParams) -> CentreOutputs:
    if params.detector.value == NO_CENTRE:
        raise ValueError('no centre found')
    return CentreOutputs(centre=Centre(x=params.detector.value + params.masks))


def _iofq(params: IofqParams) -> IofqOutputs:
    value = read_file(params.sample) + params.beam_centre.x + params.q
    return IofqOutputs(total=Total(value=value))


def _norm(params: NormParams) -> NormOutputs:
    return NormOutputs(normalised=params.total.value / params.thickness)


def pair(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, with the last transmission run of its sample."""
    files = sorted(files, key=lambda f: f.metadata['run'])
    partner = {
        f.metadata['sample']: f.ref
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = []
    for f in files:
        if f.metadata.get('role') == 'sample':
            values = {'sample': f.ref}
            if f.metadata['sample'] in partner:
                values['sample_transmission'] = partner[f.metadata['sample']]
            rows.append(Row(str(f.metadata['run']), values))
    return rows


@pytest.fixture
def workspace(fake: FakeFiles) -> Iterator[Workspace]:
    bind = [
        function_binding(DETECTOR, _detector),
        function_binding(CENTRE, _centre),
        function_binding(IOFQ, _iofq),
        function_binding(NORM, _norm),
    ]
    workspace = Workspace(proposal='p1', files=fake, bind=bind)
    yield workspace
    workspace.close()


@pytest.fixture
def definitions(tmp_path: Path) -> FolderDefinitions:
    return FolderDefinitions(tmp_path / 'folder')


IOFQ_TAKES = {
    'sample_transmission': 'sample_transmission',  # a new column: no step took it
    'beam_centre': 'centre.centre',
}
"""I(Q) takes ``sample`` by its name, a column of the detector."""


def _chain(
    client: Client, definitions: FolderDefinitions | None = None, *, iofq: bool = True
) -> Batch:
    batch = Batch(
        client,
        DETECTOR,
        columns=('run',),
        takes={'run': 'sample'},
        label='b7',
        source=pair,
        definitions=definitions,
    )
    batch.add(CENTRE, takes={'detector': 'detector.detector'})
    if iofq:
        batch.add(IOFQ, takes=IOFQ_TAKES)
    return batch


@pytest.fixture
def chain(client: Client, definitions: FolderDefinitions) -> Iterator[Batch]:
    batch = _chain(client, definitions)
    yield batch
    batch.close()


@pytest.fixture
def follow(chain: Batch) -> Automatic:
    return Automatic(chain, at={'sample', 'sample_transmission'})


def measure(fake: FakeFiles, run: int, value: float, *, partner: bool = True) -> None:
    """A sample run of sample ``w``, and its transmission run unless told not to."""
    fake.measure(run, value, role='sample', sample='w')
    if partner:
        fake.measure(run + 1000, 1.0, role='transmission', sample='w')


def texts(batch: Batch, key: str) -> list[str]:
    """The status of the row of ``key`` at each step."""
    return [s[key].text for s in batch.states([key]).values()]


def settle(client: Client) -> None:
    client.wait(client.records())


def test_a_step_adds_a_column_per_output_and_the_input_columns_it_names(
    chain: Batch,
) -> None:
    assert chain.columns == ('sample', 'sample_transmission')
    assert list(chain.outputs) == [
        'detector.detector',
        'centre.centre',
        'iofq.total',
    ]
    assert chain.step('iofq').takes == {
        'sample': 'sample',
        'sample_transmission': 'sample_transmission',
        'beam_centre': 'centre.centre',
    }


def test_the_first_step_keeps_the_label_and_each_added_one_has_its_own(
    chain: Batch,
) -> None:
    assert [s.label for s in chain.steps] == ['b7', 'b7/centre', 'b7/iofq']


def test_one_pass_of_follow_submits_the_whole_chain_of_a_new_row(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    records = follow.step()
    assert [r.label for r in client.records()] == ['b7', 'b7/centre', 'b7/iofq']
    assert records['1'].label == 'b7/iofq'
    settle(client)
    assert texts(chain, '1') == ['completed', 'completed', 'completed']


def test_a_row_without_a_partner_is_incomplete_only_at_the_step_that_takes_it(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0, partner=False)
    follow.step()
    settle(client)
    assert texts(chain, '1') == ['completed', 'completed', 'incomplete']


def test_a_failed_step_empties_its_output_and_the_later_steps_wait_for_it(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, -1.0)
    follow.step()
    settle(client)
    assert texts(chain, '1') == [
        'failed',
        'waits for detector.detector',
        'waits for centre.centre',
    ]


def test_a_step_that_fails_by_itself_is_named_next_to_the_step_that_waits(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, NO_CENTRE)
    follow.step()
    settle(client)
    assert texts(chain, '1') == ['completed', 'failed', 'waits for centre.centre']


def test_follow_leaves_a_failed_step_and_reduce_left_runs_the_row_to_the_end(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, -1.0)
    follow.step()
    settle(client)
    assert follow.step() == {}
    measure(fake, 1, 5.0)  # the file is repaired
    chain.reduce_left(['1'])
    settle(client)
    assert texts(chain, '1') == ['completed', 'completed', 'completed']


def test_a_cell_set_by_hand_empties_the_outputs_of_the_steps_after_it(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.rows = chain.rows.edit('1', {'sample': fake.measure(50, 7.0, role='x')})
    # I(Q) can no longer be reduced with the centre of the other file
    assert texts(chain, '1') == [
        'outdated',
        'waits for detector.detector',
        'waits for centre.centre',
    ]


def test_reduce_left_reduces_a_row_changed_by_hand_to_the_end_of_the_chain(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.rows = chain.rows.edit('1', {'sample': fake.measure(50, 7.0, role='x')})
    chain.reduce_left()
    settle(client)
    assert texts(chain, '1') == ['completed', 'completed', 'completed']
    (iofq,) = client.records(label='b7/iofq')[-1:]
    assert client.output(iofq, 'total').value == 7.0 + 7.0 + 10


def test_follow_leaves_every_step_of_a_row_changed_by_hand(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.rows = chain.rows.edit('1', {'sample': fake.measure(50, 7.0, role='x')})
    assert follow.step() == {}
    assert [s['1'].changed_by_hand for s in chain.states().values()] == [
        True,
        False,
        False,
    ]


def test_a_row_added_by_hand_is_left_to_a_person_until_reduced(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    run = fake.measure(50, 7.0, role='x')
    chain.rows = chain.rows.add(
        Row('mine', {'sample': run, 'sample_transmission': run})
    )
    assert follow.step() == {}
    chain.reduce_left(['mine'])
    settle(client)
    assert follow.step() == {}
    assert texts(chain, 'mine') == ['completed', 'completed', 'completed']


def test_a_change_of_settings_keeps_the_results_in_use(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.step('centre').settings = {'masks': 1.0}
    assert follow.step() == {}
    states = chain.states(['1'])
    assert states['centre']['1'].outdated
    assert texts(chain, '1') == ['completed', 'outdated', 'completed']


def test_reducing_again_at_a_step_reduces_the_steps_that_take_its_output(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.step('centre').settings = {'masks': 1.0}
    records = chain.reduce_left(again={'centre'})
    assert records['1'].label == 'b7/iofq'
    settle(client)
    assert follow.step() == {}
    assert client.output(records['1'], 'total').value == 1.0 + 2.0 + 10


def test_a_later_partner_outdates_only_the_step_that_takes_it(
    chain: Batch, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    chain.scan()
    chain.reduce_left()
    settle(client)
    fake.measure(2001, 1.0, role='transmission', sample='w')
    chain.scan()
    assert texts(chain, '1') == ['completed', 'completed', 'outdated']
    assert chain.states(['1'])['iofq']['1'].changed == {'sample_transmission'}


def samples(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, without a partner."""
    return [Row(k.key, {'sample': k.values['sample']}) for k in pair(files)]


def test_a_step_added_to_a_batch_with_results_is_the_only_one_follow_reduces(
    client: Client, fake: FakeFiles
) -> None:
    batch = Batch(
        client,
        DETECTOR,
        columns=('run',),
        takes={'run': 'sample'},
        label='b7',
        source=samples,
    )
    loop = Automatic(batch, at={'sample'})
    measure(fake, 1, 1.0)
    loop.step()
    settle(client)
    batch.add(CENTRE, takes={'detector': 'detector.detector'})
    assert texts(batch, '1') == ['completed', 'not reduced']
    loop.step()
    assert [r.label for r in client.records()] == ['b7', 'b7/centre']


def test_a_value_of_the_source_that_no_step_takes_is_not_a_column(
    client: Client, fake: FakeFiles
) -> None:
    batch = _chain(client, iofq=False)
    measure(fake, 1, 1.0)
    batch.scan()
    (problem,) = batch.states(['1'])['detector']['1'].problems
    assert (problem.loc, problem.msg) == (('sample_transmission',), 'not a column')


def test_a_column_that_a_person_fills_is_left_to_the_person(
    chain: Batch, follow: Automatic, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    follow.step()
    settle(client)
    chain.add(NORM, takes={'total': 'iofq.total', 'thickness': 'thickness'})
    assert chain.columns == ('sample', 'sample_transmission', 'thickness')
    assert texts(chain, '1')[-1] == 'incomplete'
    chain.rows = chain.rows.edit('1', {'thickness': 2.0})
    assert follow.step() == {}
    records = chain.reduce_left()
    assert client.output(records['1'], 'normalised') == (1.0 + 1.0 + 10) / 2.0


def test_a_refused_cell_is_named_by_its_column(chain: Batch, fake: FakeFiles) -> None:
    measure(fake, 1, 1.0)
    chain.scan()
    chain.rows = chain.rows.edit('1', {'sample': file(run=999)})
    (problem,) = chain.states(['1'])['detector']['1'].problems
    assert problem.loc == ('sample',)


def test_a_step_whose_output_another_takes_cannot_be_removed(chain: Batch) -> None:
    with pytest.raises(ValueError, match='iofq'):
        chain.remove('centre')


def test_removing_a_step_removes_the_columns_only_it_took_with_their_edits(
    chain: Batch, fake: FakeFiles
) -> None:
    measure(fake, 1, 1.0)
    chain.scan()
    chain.add(NORM, takes={'total': 'iofq.total', 'thickness': 'thickness'})
    chain.rows = chain.rows.edit('1', {'thickness': 2.0})
    assert chain.remove('norm') == ['thickness', 'norm.normalised']
    assert chain.columns == ('sample', 'sample_transmission')
    assert dict(chain.rows.edits) == {}


def test_a_batch_made_again_on_its_definitions_reads_the_same_states(
    chain: Batch,
    follow: Automatic,
    fake: FakeFiles,
    client: Client,
    definitions: FolderDefinitions,
) -> None:
    measure(fake, 1, 1.0)
    measure(fake, 2, 2.0)
    follow.step()
    settle(client)
    chain.step('iofq').settings = {'q': 20}
    chain.rows = chain.rows.edit('2', {'sample': fake.measure(50, 7.0, role='x')})
    before = chain.states()
    again = _chain(client, definitions)
    again.scan()
    assert again.step('iofq').settings == {'q': 20}
    assert again.states() == before


def test_the_one_step_accessors_refuse_a_batch_of_several(chain: Batch) -> None:
    with pytest.raises(ValueError, match='several steps'):
        chain.state()
    with pytest.raises(ValueError, match='several steps'):
        chain.settings


def test_settings_refused_at_a_later_step_raise_after_the_earlier_ones_submit(
    chain: Batch, fake: FakeFiles, client: Client
) -> None:
    measure(fake, 1, 1.0)
    chain.scan()
    chain.step('iofq').settings = {'q': 'many'}
    with pytest.raises(SubmitError, match='q'):
        chain.reduce_left()
    assert [r.label for r in client.records()] == ['b7', 'b7/centre']

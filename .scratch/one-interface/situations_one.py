"""
Candidate B: the situations of one-interface.md on the spike ``one.py``.
Lines marked DIFFERS behave differently from the branch
(``situations_branch.py``).

Run: .scratch/venv-container/bin/python .scratch/one-interface/situations_one.py
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
from common import Proposal, say, stories, table, texts

from ess.apps import Template
from ess.apps.batch import Row, RowTable, frames, summary
from one import Automatic, Batch, RowState, arrived, ready, remaining

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER: dict[str, Any] = {'binning': {'bins': 3}}


def table_batch(p: Proposal, client: Any, label: str = 'scan') -> Batch:
    return Batch(
        client, table.PLAIN, columns=table.COLUMNS, label=label, source=table.pair,
        persist=True, resolve=p.resolve(table.PLAIN),
    )  # fmt: skip


def reduce_table(
    p: Proposal, client: Any, template: Template, rows: pd.DataFrame, label: str,
    only_remaining: bool,
) -> list[str]:  # fmt: skip
    """``frames.reduce_table`` on the spike: --remaining is ``remaining``."""
    spec = client.spec(template.spec)
    batch = Batch(
        client, spec, columns=template.blanks, label=label, persist=True,
        resolve=p.resolve(spec),
    )  # fmt: skip
    batch.rows = batch.rows.rescan(frames.rows_of(rows, spec.params, template.blanks))
    states = batch.state(template.params)
    keys = remaining(states) if only_remaining else list(states)
    client.wait(batch.reduce(template.params, keys))
    return keys


def person() -> None:
    p = Proposal()
    client = p.open()
    refs = {n: p.sample(n, name) for n, name in [(1, 'water'), (2, 'sds'), (3, 'dmso')]}
    p.files.corrupt(refs[2])
    batch = table_batch(p, client)

    # a person reduces a table once
    batch.scan()
    client.wait(batch.reduce(SETTINGS, ready(batch.state())))
    assert texts(batch.state(SETTINGS)) == {
        '1': 'completed', '2': 'failed', '3': 'completed'}  # fmt: skip
    say('reduce once', texts(batch.state(SETTINGS)))

    # reduces the remaining rows after a failure
    p.files.repair(refs[2])
    again = batch.reduce(SETTINGS, remaining(batch.state(SETTINGS)))
    client.wait(again)
    assert list(again) == ['2']
    say('remaining after a failure', 'submitted', list(again))

    # changes a setting and reduces the stale rows
    state = batch.state(OTHER)
    assert remaining(state) == ['1', '2', '3']
    assert state['1'].changed == {'binning'}
    say('changed setting', texts(state), 'changed:', set(state['1'].changed))

    # ... and after a restart, before reducing
    client = p.restart()
    batch = table_batch(p, client)
    batch.scan()
    assert remaining(batch.state(OTHER)) == ['1', '2', '3']  # DIFFERS: still stale
    say('  after a restart', texts(batch.state(OTHER)))
    # the same from the command line: --remaining is the page's "reduce remaining"
    rows = pd.DataFrame(
        {'key': ['1', '2', '3'], 'sample': ['1', '2', '3'],
         'transmission': ['1001', '1002', '1003'], 'thickness': ['1', '1', '1']}
    )  # fmt: skip
    template = Template(table.PLAIN, params=OTHER, blanks=table.COLUMNS)
    ran = reduce_table(p, client, template, rows, 'scan', only_remaining=True)
    assert ran == ['1', '2', '3']  # DIFFERS: the stale rows ran
    assert reduce_table(p, client, template, rows, 'scan', only_remaining=True) == []
    say('  command, --remaining', 'submitted', ran, 'then []')

    # tweaks one row: an edit at a parameter that is not a column
    batch.rows = batch.rows.edit('3', {'binning': {'bins': 8}})
    (tweaked,) = batch.reduce(OTHER, ['3']).values()
    client.wait(tweaked)
    state = batch.state(OTHER)
    assert tweaked.request.params['binning'] == {'bins': 8}
    assert remaining(state) == []  # the row's result is of its own values
    say('tweak row 3', texts(state), 'own values:', dict(batch.rows.edits))

    # edits a cell by hand
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    state = batch.state(OTHER)
    assert remaining(state) == ['1']  # DIFFERS: whoever submitted row 1
    assert state['1'].changed == {'thickness'}
    client.wait(batch.reduce(OTHER, ['1']))
    say('edit a cell', 'row 1 changed:', set(state['1'].changed), 'and reduced')

    # after a restart, what a person set by hand is gone, as on the branch
    by_hand = batch.rows
    client = p.restart()
    batch = table_batch(p, client)
    batch.scan()
    state = batch.state(OTHER)
    assert {k: set(s.changed) for k, s in state.items()} == {
        '1': {'thickness'}, '2': set(), '3': {'binning'}}  # fmt: skip
    say('  after a restart', texts(state), '<- the table lost the edits; the records say so')
    # ... unless it is stored: the edits and the settings are all there is to store
    batch.rows = RowTable(
        added=by_hand.added, edits=by_hand.edits, removed=by_hand.removed
    )
    batch.scan()
    assert remaining(batch.state(OTHER)) == []
    say('  with the edits loaded', texts(batch.state(OTHER)))
    p.close()


def per_run(files: Any) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files]


def iofq_batch(
    p: Proposal, client: Any, spec: Any = stories.IOFQ, label: str = 'iofq'
) -> Batch:
    return Batch(
        client, spec, columns=('sample',), label=label, source=per_run, persist=True,
        resolve=p.resolve(spec),
    )  # fmt: skip


def angles(files: Any) -> list[Row]:
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        if f.metadata.get('role') == 'sample':
            runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(sample, {'runs': refs}) for sample, refs in runs.items()]


def automatic() -> None:
    # E1: a series grows
    p = Proposal()
    client = p.open()
    reference = p.measure(1, [1.0, 1.0], role='reference')
    batch = Batch(
        client, stories.STITCH, columns=('runs',), label='reflectivity',
        source=angles, persist=True, resolve=p.resolve(stories.STITCH),
    )  # fmt: skip
    auto = Automatic(batch, runs=arrived('runs'), settings=lambda: {'reference': reference})
    r2 = p.measure(2, [1.0, 2.0], role='sample', sample='si')
    client.wait(auto.step())
    r4 = p.measure(4, [4.0, 8.0], role='sample', sample='si')
    r3 = p.measure(3, [1.0, 1.0], role='sample', sample='si')
    p.measure(2, [1.0, 2.0], role='sample', sample='si')
    auto.step()
    latest = client.latest('reflectivity', member='si')
    assert len(client.records(label='reflectivity')) == 2
    assert latest.request.params['runs'] == [r2, r3, r4]
    say('E1 series grows', '2 records, the newest holds runs 2, 3, 4')
    p.close()

    # E2: goes quiet
    p = Proposal()
    client = p.open([stories.IOFQ_V2])
    auto = Automatic(
        iofq_batch(p, client), runs=arrived('sample'),
        settings=lambda: {'normalization': None},
    )  # fmt: skip
    p.measure(1, [1.0, 1.0], role='sample')
    assert auto.step() == {}
    assert auto.refused == {'1': 'unknown spec sans-iofq/v1'}
    say('E2 goes quiet', auto.refused)
    p.close()

    # E4: settings improved during a beamtime
    p = Proposal()
    client = p.open()
    settings: dict[str, Any] = {'normalization': None}
    batch = iofq_batch(p, client)
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: settings)
    for run in (1, 2):
        p.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')
    client.wait(auto.step())
    settings = {'reduction': {'threshold': 1.5}, 'normalization': None}
    p.measure(3, [1.0, 2.0, 3.0, 4.0], role='sample')
    done = auto.step()
    client.wait(done)
    assert list(done) == ['3']
    say('E4 settings improved', 'submitted', list(done))

    # watching the automatic reduction on a page: what a page shows is this
    shown = batch.state(settings)
    assert texts(shown) == {'1': 'stale', '2': 'stale', '3': 'completed'}
    # "the results made before the improvement can be found" is the state
    assert [k for k, s in shown.items() if s.changed] == ['1', '2']
    say('page of the automatic batch', summary(shown))  # type: ignore[arg-type]

    # a restart of the automatic reduction
    client = p.restart()
    batch = iofq_batch(p, client)
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: settings)
    assert auto.step() == {}
    p.measure(4, [1.0, 2.0, 3.0, 4.0], role='sample')
    done = auto.step()
    client.wait(done)
    assert list(done) == ['4']
    say('restart', 'first step submitted [], then', list(done))
    shown = batch.state(settings)
    assert texts(shown) == {  # DIFFERS: still stale after the restart
        '1': 'stale', '2': 'stale', '3': 'completed', '4': 'completed'}  # fmt: skip
    say('  page after the restart', summary(shown))  # type: ignore[arg-type]

    # a person catches up on the automatic batch: the page's "reduce remaining"
    caught_up = batch.reduce(settings, remaining(shown))
    client.wait(caught_up)
    assert list(caught_up) == ['1', '2']
    assert auto.step() == {}  # and the loop reduces none of them again
    say('  a person reduces remaining', list(caught_up), 'and the loop then: []')

    # a scientist takes the automatic configuration as the start of a batch
    mine = Proposal(submitter='scientist')  # their own folder: another history
    for run in (1, 2, 3, 4):
        mine.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')
    own = mine.open()
    theirs = iofq_batch(mine, own, label='mine')  # the same function and settings
    theirs.scan()
    theirs.rows = theirs.rows.remove('2')
    records = theirs.reduce(settings, ready(theirs.state()))
    own.wait(records)
    assert list(records) == ['1', '3', '4']
    say('scientist starts from it', 'reduced', list(records), 'in their folder')
    mine.close()

    # "reduce everything again from run 3 with the new version"
    client = p.restart([stories.IOFQ, stories.IOFQ_V2])
    v2 = iofq_batch(p, client, stories.IOFQ_V2)
    v2_settings = {'normalization': None}
    auto = Automatic(v2, runs=arrived('sample'), settings=lambda: v2_settings)
    assert auto.step() == {}  # the loop reduces no old row for a new version
    state = v2.state()
    assert texts(state) == dict.fromkeys('1234', 'outdated')  # DIFFERS
    old = client.output(state['1'].record, 'iofq')  # the old result stays readable
    # (a) a person, by keys, as on the branch
    again = v2.reduce(v2_settings, ['3'])
    client.wait(again)
    assert list(again) == ['3']

    # (b) or one more term of what the loop runs, read at its next start
    def from_run_4(states: Mapping[str, RowState]) -> list[str]:
        new = set(arrived('sample')(states))
        return [
            k
            for k in ready(states)
            if k in new or (states[k].outdated and int(k) >= 4)
        ]

    auto = Automatic(v2, runs=from_run_4, settings=lambda: v2_settings)
    done = auto.step()
    client.wait(done)
    assert list(done) == ['4']
    assert auto.step() == {}
    assert texts(v2.state()) == {
        '1': 'outdated', '2': 'outdated', '3': 'completed', '4': 'completed'}  # fmt: skip
    say('again from run 3, version 2', texts(v2.state()), '; old result of row 1:',
        old.values.tolist())  # fmt: skip
    p.close()


if __name__ == '__main__':
    person()
    automatic()

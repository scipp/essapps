"""
Candidate A: the situations of one-interface.md on the branch as committed.
Each assert states what the branch does, also where that is not what a person
would want (marked NOTE).

Run: .scratch/venv-container/bin/python .scratch/one-interface/situations_branch.py
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from common import Proposal, say, stories, table, texts

from ess.apps import Template
from ess.apps.batch import Batch, Row, frames, ready, remaining, summary
from ess.apps.batch.auto import Automatic

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER: dict[str, Any] = {'binning': {'bins': 3}}


def table_batch(client: Any, label: str = 'scan') -> Batch:
    return Batch(
        client, table.PLAIN, columns=table.COLUMNS, label=label, source=table.pair,
        persist=True,
    )  # fmt: skip


def person() -> None:
    p = Proposal()
    client = p.open()
    refs = {n: p.sample(n, name) for n, name in [(1, 'water'), (2, 'sds'), (3, 'dmso')]}
    p.files.corrupt(refs[2])
    batch = table_batch(client)

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
    assert remaining(batch.state(OTHER)) == ['1', '2', '3']
    say('changed setting', texts(batch.state(OTHER)))
    # the same from the command line: --remaining leaves out every completed row
    rows = pd.DataFrame(
        {'key': ['1', '2', '3'], 'sample': ['1', '2', '3'],
         'transmission': ['1001', '1002', '1003'], 'thickness': ['1', '1', '1']}
    )  # fmt: skip
    template = Template(table.PLAIN, params=OTHER, blanks=table.COLUMNS)
    before = len(client.records(label='scan'))
    frames.reduce_table(
        client, template, rows, label='scan', persist=True, remaining=True
    )
    assert len(client.records(label='scan')) == before  # NOTE: nothing ran
    say('  command, --remaining', 'submitted 0 of 3 stale rows')

    # ... and after a restart, before reducing
    client = p.restart()
    batch = table_batch(client)
    batch.scan()
    assert remaining(batch.state(OTHER)) == []  # NOTE: ran with bins=2, shown completed
    say('  after a restart', texts(batch.state(OTHER)))

    # tweaks one row
    cells = batch.rows['3'].values
    client.wait(batch.tweak('3', {'binning': {'bins': 8}, **cells}))
    state = batch.state(SETTINGS)
    assert state['3'].tweaked
    assert remaining(state) == []
    say('tweak row 3', texts(state), 'tweaked:', [k for k in state if state[k].tweaked])
    client = p.restart()
    batch = table_batch(client)
    batch.scan()
    state = batch.state(SETTINGS)
    assert not state['3'].tweaked  # NOTE: forgotten (#191)
    assert remaining(state) == []
    say('  after a restart', texts(state), 'tweaked: []')

    # edits a cell by hand
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    assert remaining(batch.state(SETTINGS)) == []  # NOTE: this Batch did not submit row 1
    client.wait(batch.reduce(SETTINGS, ['1']))
    batch.rows = batch.rows.edit('1', {'thickness': 4.0})
    assert remaining(batch.state(SETTINGS)) == ['1']
    say('edit a cell', 'stale only if this Batch object submitted the row')
    p.close()


def per_run(files: Any) -> list[Row]:
    return [Row(str(f.metadata['run']), {'sample': f.ref}) for f in files]


def iofq_batch(client: Any, spec: Any = stories.IOFQ, label: str = 'iofq') -> Batch:
    return Batch(
        client, spec, columns=('sample',), label=label, source=per_run, persist=True
    )


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
        source=angles, persist=True,
    )  # fmt: skip
    auto = Automatic(batch, at='runs', settings=lambda: {'reference': reference})
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
        iofq_batch(client), at='sample', settings=lambda: {'normalization': None}
    )
    p.measure(1, [1.0, 1.0], role='sample')
    assert auto.step() == {}
    assert auto.refused == {'1': 'unknown spec sans-iofq/v1'}
    say('E2 goes quiet', auto.refused)
    p.close()

    # E4: settings improved during a beamtime
    p = Proposal()
    client = p.open()
    settings: dict[str, Any] = {'normalization': None}
    batch = iofq_batch(client)
    auto = Automatic(batch, at='sample', settings=lambda: settings)
    for run in (1, 2):
        p.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')
    client.wait(auto.step())
    settings = {'reduction': {'threshold': 1.5}, 'normalization': None}
    p.measure(3, [1.0, 2.0, 3.0, 4.0], role='sample')
    done = auto.step()
    client.wait(done)
    assert list(done) == ['3']
    say('E4 settings improved', 'submitted', list(done))

    # watching the automatic reduction on a page: what BatchView shows is this
    shown = batch.state(settings)
    assert texts(shown) == {'1': 'stale', '2': 'stale', '3': 'completed'}
    say('page of the automatic batch', summary(shown))

    # a restart of the automatic reduction
    client = p.restart()
    batch = iofq_batch(client)
    auto = Automatic(batch, at='sample', settings=lambda: settings)
    assert auto.step() == {}
    p.measure(4, [1.0, 2.0, 3.0, 4.0], role='sample')
    done = auto.step()
    client.wait(done)
    assert list(done) == ['4']
    say('restart', 'first step submitted [], then', list(done))
    shown = batch.state(settings)
    assert texts(shown) == {  # NOTE: runs 1 and 2 ran with the old settings
        '1': 'completed', '2': 'completed', '3': 'completed', '4': 'completed'}  # fmt: skip
    say('  page after the restart', summary(shown))

    # a scientist takes the automatic configuration as the start of a batch
    mine = Proposal(submitter='scientist')  # their own folder: another history
    for run in (1, 2, 3, 4):
        mine.measure(run, [1.0, 2.0, 3.0, 4.0], role='sample')
    own = mine.open()
    theirs = iofq_batch(own, label='mine')  # the same function and settings
    theirs.scan()
    theirs.rows = theirs.rows.remove('2')
    client_records = theirs.reduce(settings, ready(theirs.state()))
    own.wait(client_records)
    assert list(client_records) == ['1', '3', '4']
    say('scientist starts from it', 'reduced', list(client_records), 'in their folder')
    mine.close()

    # "reduce everything again from run 3 with the new version"
    client = p.restart([stories.IOFQ, stories.IOFQ_V2])
    v2 = iofq_batch(client, stories.IOFQ_V2)
    auto = Automatic(v2, at='sample', settings=lambda: {'normalization': None})
    assert auto.step() == {}  # the loop reduces no old row for a new version
    assert texts(v2.state()) == dict.fromkeys('1234', 'not reduced')  # NOTE
    again = v2.reduce({'normalization': None}, ['3', '4'])  # a person, by keys
    client.wait(again)
    assert auto.step() == {}
    assert texts(v2.state()) == {
        '1': 'not reduced', '2': 'not reduced', '3': 'completed', '4': 'completed'}  # fmt: skip
    say('again from run 3, version 2', texts(v2.state()))
    p.close()


if __name__ == '__main__':
    person()
    automatic()

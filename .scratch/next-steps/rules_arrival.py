"""
Story: a transmission run is reduced as it arrives, and a sample waits for the
transmission run of its sample, then is reduced with the transmission's output
(the chain of automatic-reduction.md, with a transmission in place of the centre).

Two batches: T reduces each transmission run (CONTRIBUTE), S each sample run
(CENTRED_MULTI) with a reference to T's result of its sample. S's source reads
T's results through a closure: MISSING 3, a source that sees another batch's
results.
"""

from rules_common import Batch, Proposal, auto_step, naive_step, show, toys

from ess.apps.batch import FileInfo, Row

p = Proposal()
client = p.open()


def transmissions(files: list[FileInfo]) -> list[Row]:
    return [
        Row(str(f.metadata['run']), {'sample': f.ref}, {'name': f.metadata['sample']})
        for f in files
        if f.metadata.get('role') == 'transmission'
    ]


T = Batch(client, toys.CONTRIBUTE, columns=('sample',), label='auto/transmission',
          source=transmissions, persist=True)


def samples(files: list[FileInfo]) -> list[Row]:
    """A row per sample run; its transmission is T's result for the sample's name."""
    done = {T.rows[k].info['name']: s.record for k, s in T.state().items() if s.record}
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        values = {'sample': [f.ref]}
        if (record := done.get(f.metadata['sample'])) is not None:
            values['transmission'] = record.ref('transmission')
        rows.append(Row(str(f.metadata['run']), values, {'name': f.metadata['sample']}))
    return rows


S = Batch(client, toys.CENTRED_MULTI, columns=('sample', 'transmission'),
          label='auto/iofq', source=samples, persist=True)
can = p.measure(5, [1.0, 1.0], role='can')
settings_T: dict = {}
settings_S = {'centre': {'x': 0.0}, 'can': [can]}


def step() -> tuple:
    t, t_refused = auto_step(T, settings_T, at='sample')
    s, s_refused = auto_step(S, settings_S, at='sample')
    return t, s, t_refused | s_refused


print('-- step 1: sample 10 arrives before its transmission run')
p.measure(10, [4.0, 6.0], role='sample', sample='w')
t, s, refused = step()
print('submitted', list(t), list(s), 'refused', refused)
row = S.state()['10']
print('S row 10:', row.text, [e.msg for e in row.problems])

print('-- step 2: transmission run 11 arrives')
p.measure(11, [1.0, 3.0], role='transmission', sample='w')
t, s, refused = step()
print('T', show(client, t), 'S', show(client, s))
print('I(Q) of 10:', client.output(s['10'], 'centred').values.tolist())

print('-- step 3: nothing new')
t, s, refused = step()
print('submitted', list(t), list(s))

print('-- a transmission run that fails: sample 20, transmission 21 corrupt')
p.measure(20, [4.0, 6.0], role='sample', sample='x')
bad = p.measure(21, [1.0, 3.0], role='transmission', sample='x')
p.files.corrupt(bad)
t, s, refused = step()
print('T', show(client, t), 'S', list(s))
t, s, refused = step()
row = S.state()['20']
print('next step: S row 20:', row.text, [e.msg for e in row.problems], 'submitted', list(t), list(s))
p.files.repair(bad)
print('a person reduces T row 21 again:', show(client, T.reduce(settings_T, ['21'])))
t, s, refused = step()
print('next step: S', show(client, s))

print('-- the obvious loop on the same batches, after another sample of w')
p.measure(30, [8.0, 8.0], role='sample', sample='w')
print('T', naive_step(T, settings_T))
print('S', show(client, naive_step(S, settings_S)))
p.close()

"""
Story S7 as automatic reduction: each sample is reduced with the can measured
before it. Then a source that pairs with the *last* can, as LoKI's
pair_by_sample does with transmission runs: a later can changes the cells of
earlier rows. The obvious loop then reduces those rows again; the loop with
"handled at sample" does not.
"""

from rules_common import Batch, Proposal, auto_step, naive_step, show, toys

from ess.apps.batch import FileInfo, Row

settings = {'normalization': None}


def can_before(files: list[FileInfo]) -> list[Row]:
    cans = [f for f in files if f.metadata.get('role') == 'background']
    rows = []
    for f in files:
        if f.metadata.get('role') != 'sample':
            continue
        before = [c for c in cans if c.metadata['run'] < f.metadata['run']]
        values = {'sample': f.ref} | ({'can': before[-1].ref} if before else {})
        rows.append(Row(str(f.metadata['run']), values))
    return rows


def last_can(files: list[FileInfo]) -> list[Row]:
    cans = [f for f in files if f.metadata.get('role') == 'background']
    return [
        Row(str(f.metadata['run']), {'sample': f.ref} | ({'can': cans[-1].ref} if cans else {}))
        for f in files
        if f.metadata.get('role') == 'sample'
    ]


def can_of(record) -> int:
    return p.files.metadata(record.request.params['can'])['run']


for name, source in [('can before', can_before), ('last can', last_can)]:
    for loop in ('obvious', 'handled'):
        label = f'{name}/{loop}'
        p = Proposal()
        client = p.open()
        batch = Batch(client, toys.IOFQ_CAN, columns=('sample', 'can'), label=label,
                      source=source, persist=True)
        p.measure(1, [1.0, 1.0], role='background')
        p.measure(2, [5.0, 6.0], role='sample')

        def step():
            if loop == 'obvious':
                return naive_step(batch, settings)
            return auto_step(batch, settings, at='sample')[0]

        first = step()
        show(client, first)  # wait, so that no row is still running at step 2
        p.measure(3, [2.0, 2.0], role='background')
        p.measure(4, [6.0, 9.0], role='sample')
        second = step()
        show(client, second)
        print(f'{label:22}', 'step 1:', {k: can_of(r) for k, r in first.items()},
              'step 2:', {k: can_of(r) for k, r in second.items()})
        p.close()

"""
Story E4: the template is improved during a beamtime (a setting changes while the
loop runs). New runs use it; run 1 is not reduced again; the results made before
can be found. Then E2 (an upgrade removed the spec's version) and one row that
the workspace refuses among good ones.
"""

from rules_common import Batch, Proposal, auto_step, naive_step, show, toys

from ess.apps.batch import FileInfo, Row


def per_run(files: list[FileInfo]) -> list[Row]:
    """A row per sample run; its binning comes from the file, as a column."""
    return [
        Row(str(f.metadata['run']),
            {'sample': f.ref, 'reduction': {'bins': f.metadata.get('bins', 2)}})
        for f in files if f.metadata.get('role') == 'sample'
    ]


def make(client, label='iofq'):
    return Batch(client, toys.IOFQ, columns=('sample', 'reduction'), label=label,
                 source=per_run, persist=True)


base = {'normalization': None, 'beam_centre': {'x': 0.0}}
improved = base | {'beam_centre': {'x': 1.0}}

print('-- E4: the centre is improved after run 1')
for loop in ('obvious', 'handled'):
    p = Proposal()
    client = p.open()
    batch = make(client)

    def step(settings):
        if loop == 'obvious':
            return naive_step(batch, settings)
        return auto_step(batch, settings, at='sample')[0]

    p.measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    before = step(base)
    show(client, before)
    p.measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    after = step(improved)
    show(client, after)
    old = [r.member for r in client.records(label='iofq')
           if r.request.params['beam_centre'] != {'x': 1.0}]
    print(f'{loop:8} reduced after the change: {sorted(after)}; made before it: {old}')
    p.close()

print('-- E2: an upgrade removed version 1 of the spec')
p = Proposal()
client = p.open(bind=[toys.TOYS[toys.SpecId.of(toys.IOFQ_V2)]])
batch = make(client)
p.measure(1, [1.0, 1.0], role='sample')
print('obvious:', naive_step(batch, base))
print('handled:', auto_step(batch, base, at='sample'))
p.close()

print("-- one row refused (run 2 says bins='two'), runs 1 and 3 fine")
for loop in ('obvious', 'handled'):
    p = Proposal()
    client = p.open()
    batch = make(client, label=f'iofq-{loop}')
    p.measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    p.measure(2, [1.0, 2.0, 3.0, 4.0], role='sample', bins='two')
    p.measure(3, [1.0, 2.0, 3.0, 4.0], role='sample')
    for n in (1, 2):
        if loop == 'obvious':
            out = naive_step(batch, base)
        else:
            out = auto_step(batch, base, at='sample')
        print(f'{loop:8} step {n}:', out if isinstance(out, str) else
              (sorted(out[0]), out[1]))
    p.close()

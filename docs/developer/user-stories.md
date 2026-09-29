# User stories

Each story has an actor, a goal, the client code that reaches it, and its checks as assertions.
The code uses only the API of [proposals/core-api.md](proposals/core-api.md), and the checks observe only what that API shows: values, records, labels, provenance, and errors.
What a story needs from the system, such as cost, placement, or persistence, is a separate story in [system-stories.md](system-stories.md).
A story that needs something the API does not have says so in a "Gap" line, and [Findings](#findings) collects it.

## Toy specs

Toy specs stand in for instrument workflows, so that a story's numbers can be checked by hand.
A dataset made by `measure` holds a list of counts.
Reductions take runs directly, and `CUT` and `EXPORT` read results; a separate record is made only for a result worth keeping by itself.

| Spec | Params | Outputs | Computes | Stands in for |
|---|---|---|---|---|
| `IOFQ` | `run`, `bins=2`, `threshold=0.0`, `can=None`, `beam_centre=None`, `normalization=None` | `iofq`, `masked` | counts minus the can's counts; values below `threshold` set to 0 (`masked`); minus `beam_centre`; divided by `normalization`; summed into `bins` equal groups (`iofq`) | a SANS or diffraction reduction of one run |
| `BEAM_CENTRE` | `run` | `centre` | the mean of the counts | beam-centre finding |
| `VANADIUM` | `run`, `scale=1.0` | `normalization` | the sum of the counts times `scale` | vanadium processing |
| `NORMALIZE` | `runs: list`, `scale=1.0` | `normalized` | the counts summed over runs, divided by their total, times `scale` | a reduction that sums runs internally |
| `BACKGROUND` | `sample_runs: list`, `background_runs: list` | `subtracted` | summed sample counts minus summed background counts | a sum of two sets of runs |
| `CONTRIBUTE` | `run` | `numerator`, `denominator`, `transmission` | the counts; their total; the first count divided by the total | the per-run part of `NORMALIZE` |
| `PARTS_SUM` | accumulator spec, element `NormalizationParts` (`numerator`, `denominator`) | `numerator`, `denominator` | each field summed | the accumulation of `NORMALIZE` |
| `FINALIZE` | `numerator`, `denominator`, `scale=1.0` | `normalized` | `numerator / denominator * scale` | the part of `NORMALIZE` after the sum |
| `ANGLE` | `run` | `counts` | the counts | one angle of a rotation scan |
| `CUT` | `data`, `index` | `cut` | the value at `index` | a cut through a volume, from another package |
| `SUM.of(Counts)` | generic accumulator spec, element `Counts` (`counts`) | `counts` | the sum | a generic accumulator from ess.reduce |
| `BANKS` | `run` | `mantle`, `high_resolution` | the first half of the counts; the second half | a diffraction reduction with one result per detector bank |
| `STITCH` | `runs: list`, `reference` | `scaled: list`, `stitched` | each run's counts divided by the reference's counts, times the factor that makes its first value equal the last value of the scaled curve before it; the first curve is not scaled (`scaled`); the scaled curves concatenated (`stitched`) | a reflectometry reduction that stitches angles with scale factors fitted over all of them |
| `EXPORT` | `data: list` | `text` | one line per entry, its values separated by commas | writing a file for another program |
| `IOFQ_V2` | as `IOFQ`, with `threshold` renamed `mask_below`, and `bins=4` | `iofq`, `masked` | as `IOFQ` | version 2 of `IOFQ`: same name, `sans-iofq`, a renamed parameter and a new default |

`FINALIZE` over `PARTS_SUM` over `CONTRIBUTE` computes what `NORMALIZE` computes.
A story may add a toy spec to this table; it must take runs directly and be checkable by hand.

## Conventions

Fixtures: `client` is a client for one proposal over a fresh backend.
`measure(n, counts, **fields)` makes run `n` appear as a dataset with the given counts and metadata, and returns its reference.
`catalogue` is a fake dataset source, and `scicat` a fake publisher.
`folder` is a directory with files that hold counts, as `measure` datasets do.
`connect(proposal=..., user=...)` returns a new client over the same backend, by default for `client`'s proposal and user; `measure(..., proposal=...)` makes a dataset belong to another proposal.
`other` is a client of a second backend that shares `catalogue` and `scicat`.
`catalogue.correct(dataset, **fields)` changes a dataset's metadata; `catalogue.add_published(entry)` lists a published entry as a dataset, as SciCat does.
`scicat.entries[pid]` is a published entry, with `.provenance` and `.supersedes`.
`clock` is a fake clock the backend reads; `crash()` ends the notebook's process without cleanup.
`corrupt(run)` makes a dataset unreadable, with the failure message `'file signature not found'`; `repair(run)` undoes it.
`upgrade(specs=..., versions=...)` replaces the backend's workflow packages: the specs it offers and the software versions its records name.

Besides the calls in core-api.md, the stories read records with these calls:

```python
client.wait(records)                  # the records, once completed
client.output(record, 'iofq')         # an output's value
client.records(label='iofq')          # records under a label, oldest first; also spec=, since=
record.request.params                 # every parameter value, defaults included
record.request.datasets()             # the datasets the request names directly
client.provenance(record).datasets()  # every dataset the record depends on, through all its inputs
record.status, record.failure
```

## S. Small stories

One concept each.

### S1. Reduce one run

Actor: user in a notebook. Goal: reduce one run with one parameter changed from its default, and read the result.

```python
run = measure(1, counts=[1.0, 2.0, 3.0, 4.0])
result = client.compute(IOFQ, {'run': run, 'threshold': 1.5})

assert client.output(result, 'iofq').values.tolist() == [2.0, 7.0]   # [0, 2, 3, 4] in 2 groups
assert result.request.params['bins'] == 2                            # default, recorded
assert result.request.datasets() == [run]
```

### S2. Tune one parameter

Actor: user in a notebook. Goal: change the binning several times, looking at the result after each change.

```python
run = measure(1, counts=[1.0, 2.0, 3.0, 4.0])
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
    for bins in (1, 2, 4):
        result = client.compute(tune, {'bins': bins}, label='iofq')

assert client.latest('iofq') == result
assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0, 3.0, 4.0]
plain = client.compute(IOFQ, result.request.params)                  # the record alone reproduces it
assert client.output(plain, 'iofq') == client.output(result, 'iofq')
```

That the run is not loaded again for each binning is system story S2.

### S3. Look at a value inside a reduction

Actor: user in a notebook. Goal: see an intermediate value, the counts after masking, without a separate workflow.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
result = client.compute(IOFQ, {'run': run, 'threshold': 2.5})

assert client.output(result, 'masked').values.tolist() == [0.0, 0.0, 3.0, 4.0]
assert client.output(result, 'iofq').values.tolist() == [0.0, 7.0]
```

The author must have declared `masked` as an output. Every record of `IOFQ` then has it.

### S4. Submit a chain in one go

Actor: user in a notebook. Goal: submit two reductions and a third request that combines them, without waiting in between.

```python
r611, r612 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0])
parts = [Request(CONTRIBUTE, {'run': run}) for run in (r611, r612)]
total = Request(PARTS_SUM, {'numerator': [p.ref('numerator') for p in parts],
                            'denominator': [p.ref('denominator') for p in parts]})
c611, c612, pending = client.submit([*parts, total])          # returns at once

(summed,) = client.wait([pending])
assert client.output(summed, 'numerator').values.tolist() == [3.0, 9.0]
assert summed.request.params['numerator'] == [c611.ref('numerator'), c612.ref('numerator')]
```

### S5. Sum runs

Actor: user in a notebook. Goal: reduce three runs of one sample as one measurement.

```python
runs = [measure(1, [1.0, 2.0]), measure(2, [1.0, 2.0]), measure(3, [0.0, 2.0])]
total = client.compute(NORMALIZE, {'runs': runs, 'scale': 2.0})

assert client.output(total, 'normalized').values.tolist() == [0.5, 1.5]   # [2, 6] / 8 * 2
assert total.request.datasets() == runs
```

This is way 1 of "One sum, three ways" in core-api. B2 uses ways 2 and 3.

### S6. Sum sample runs and background runs

Actor: user in a notebook. Goal: sum each set of runs and subtract the background.

```python
samples = [measure(1, [5.0, 5.0]), measure(2, [7.0, 5.0])]
backgrounds = [measure(3, [1.0, 1.0]), measure(4, [1.0, 2.0])]
result = client.compute(BACKGROUND, {'sample_runs': samples, 'background_runs': backgrounds})

assert client.output(result, 'subtracted').values.tolist() == [10.0, 7.0]   # [12, 10] - [2, 3]
assert result.request.datasets() == samples + backgrounds
```

### S7. Reduce each sample with the can measured before it

Actor: instrument scientist. Goal: every sample subtracts the can measured most recently before it, without naming the can.

```python
can_1 = measure(1, [1.0, 1.0], role='can')
first = measure(2, [5.0, 6.0], role='sample')
can_3 = measure(3, [2.0, 2.0], role='can')
second = measure(4, [6.0, 9.0], role='sample')
template = Template(IOFQ, blanks=('run', 'can'))
cans = Lookup(can=LastBefore(Selector(role='can')))    # the latest can measured before the run

requests = client.apply(template, [first, second], lookup=cans, label='iofq')
reduced = client.wait(client.submit(requests))

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[4.0, 5.0], [4.0, 7.0]]
assert [r.request.datasets() for r in reduced] == [[first, can_1], [second, can_3]]
```

Gap: a lookup type, a `lookup=` argument on `apply`, and a rule for which blank the dataset fills when a template has two.

### S8. Trace a result to raw data

Actor: user. Goal: learn which raw runs and parameter values a two-step result came from.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
vanadium = client.compute(VANADIUM, {'run': vanadium_run, 'scale': 2.0})
result = client.compute(IOFQ, {'run': sample, 'normalization': vanadium.ref('normalization')})

provenance = client.provenance(result)
assert set(provenance.datasets()) == {sample, vanadium_run}
(upstream,) = provenance.records()             # the records `result` reads, through all its inputs
assert upstream.request.params['scale'] == 2.0
assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]   # [4, 8] / ((1 + 1) * 2)
```

Gap: provenance shows only `.datasets()`; no call reaches the parameter values of an input record.

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user of a local application. Goal: list the files in a folder, add a reference measurement by PID, and plot all of them.

```python
files = sorted(folder.glob('*.h5'))                      # two files, counts [1, 2] and [3, 4]
vanadium = measure(9, [5.0, 6.0], pid='20.500.12269/vanadium')
named = [dataset(path=f) for f in files] + [dataset(pid='20.500.12269/vanadium')]
plots = [client.compute(IOFQ, {'run': d}) for d in named]

assert [client.output(p, 'iofq').values.tolist() for p in plots] == [
    [1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
assert plots[2].request.datasets() == [vanadium]
```

Gap: `dataset()` is shown only with `run=`; naming a local file by path and a catalogue dataset by PID is not specified.
Listing the folder is plain Python and makes no record. What is fetched, and when, is system story A1.

### A2. Run number instead of file

Actor: user at the instrument. Goal: type a run number and get the dataset.

```python
run = measure(4711, [1.0, 2.0])
result = client.compute(IOFQ, {'run': dataset(run=4711)})

assert result.request.datasets() == [run]              # the dataset itself, not the number
with pytest.raises(SubmitError):                        # no run 4712 exists
    client.compute(IOFQ, {'run': dataset(run=4712)})
assert client.records() == [result]
```

Gap: core-api does not say when a run number resolves to a dataset, or what an unknown one raises; `SubmitError` is not defined.

### A3. Work without the facility mount

System story only; see [system-stories.md](system-stories.md).

### A4. Mistaken copy into the shared service

Actor: user of the shared service. Goal: remove a file that should not have left their machine.

```python
first = client.compute(IOFQ, {'run': dataset(path=private_file)})
(uploaded,) = first.request.datasets()
client.remove(uploaded)

again = client.compute(IOFQ, first.request.params)
assert client.records(spec=IOFQ) == [first, again]      # the first record stays
assert again.failure.kind == 'missing-dataset'
```

Gap: no call removes a dataset, and failure kinds are not defined.
That no copy of the file stays in the service is system story A4.

### A5. Metadata corrected after the fact

Actor: instrument scientist. Goal: see the corrected sample name of a run already reduced.

```python
runs = [measure(n, [1.0, 2.0], sample='water') for n in (1, 2, 3)]
reduced = [client.compute(IOFQ, {'run': run}) for run in runs]
catalogue.correct(runs[1], sample='heavy water')

(named,) = reduced[1].request.datasets()
assert named == runs[1]                                  # the correction keeps the dataset
assert client.metadata(named)['sample'] == 'heavy water'
```

Gap: no call reads a dataset's metadata.

## B. Manual and interactive reduction

### B1. Tune a SANS reduction and save the result as a template

Actor: user in a notebook. Goal: change binning and mask several times, looking at the result after each change, then save the final parameters as the beamtime's template.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold')))
    for bins, threshold in [(1, 0.0), (4, 0.0), (4, 1.5), (2, 1.5)]:
        client.compute(tune, {'bins': bins, 'threshold': threshold}, label='iofq')

final = client.latest('iofq')
beamtime = Template.from_request(final.request, blanks=('run',))
new = measure(2, [2.0, 1.0, 4.0, 3.0])
(reduced,) = client.wait(client.submit(client.apply(beamtime, [new], label='iofq-beamtime')))

assert len(client.records(label='iofq')) == 4
assert (beamtime.params['bins'], beamtime.params['threshold']) == (2, 1.5)
assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]    # [2, 0, 4, 3] in 2 groups
```

Gap: no call makes a template from a record's request, and a template has no name or store beyond the notebook.
That each change comes back quickly is system story B1.

### B2. Add a run to a sum, then remove one

Actor: user in a notebook. Goal: runs 611 and 612 are summed; 613 finishes and is added; 612 turns out bad and is removed.

```python
r611, r612, r613 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0]), measure(613, [3.0, 1.0])
with client.session() as session:
    contribute = session.stage(Template(CONTRIBUTE, blanks=('run',)))
    total = session.accumulator(PARTS_SUM)
    parts = {}
    for run in (r611, r612):
        parts[run] = client.compute(contribute, {'run': run})
        total.push(parts[run])
    first = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

    parts[r613] = client.compute(contribute, {'run': r613})
    total.push(parts[r613])
    added = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

kept = [parts[r611], parts[r613]]
summed = client.compute(PARTS_SUM, {'numerator': [p.ref('numerator') for p in kept],
                                    'denominator': [p.ref('denominator') for p in kept]})
removed = client.compute(FINALIZE, summed.refs(), label='sum')

assert [client.output(r, 'normalized').values.tolist() for r in (first, added, removed)] == [
    [0.25, 0.75], [0.375, 0.625], [0.5, 0.5]]
assert client.output(client.compute(NORMALIZE, {'runs': [r611, r612]}), 'normalized') == \
    client.output(first, 'normalized')
assert client.provenance(removed).datasets() == [r611, r613]
assert client.records(label='sum') == [first, added, removed]
assert len(client.records(spec=CONTRIBUTE)) == 3                     # each run reduced once
```

Gap: removing uses a plain request over the kept contributions, because an accumulator has no `remove` (core-api open question 3).
That adding 613 costs about one run is system story B2.

### B3. Compare two parameter sets side by side

Actor: user in a notebook. Goal: look at the result with and without a mask, keep one, discard the other.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold')))
    plain = client.compute(tune, {'bins': 2, 'threshold': 0.0}, label='iofq')
    masked = client.compute(tune, {'bins': 2, 'threshold': 2.5}, label='iofq-masked')
    finer = client.compute(tune, {'bins': 4, 'threshold': 2.5}, label='iofq-masked')

assert [client.output(r, 'iofq').values.tolist() for r in (plain, masked, finer)] == [
    [3.0, 7.0], [0.0, 7.0], [0.0, 0.0, 3.0, 4.0]]
assert client.latest('iofq') == plain
assert client.records(label='iofq-masked') == [masked, finer]
```

Keeping a variant means reading its label from now on. Discarding the other needs no call.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI. Goal: drag through cuts of a Q-E volume, then fit the cut that looks right.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
volume = client.compute(ANGLE, {'run': run})                 # stands in for a Q-E volume
cuts = [client.output(volume, 'counts', index=i) for i in range(4)]   # dragging a slider
fit = client.compute(CUT, {'data': volume.ref('counts'), 'index': 2})

assert [c.value for c in cuts] == [1.0, 2.0, 3.0, 4.0]
assert client.output(fit, 'cut').value == 3.0
assert client.records() == [volume, fit]                     # the views made no record
```

Gap: `client.output` reads a whole output; a view of part of an output has no call.
The chosen cut is a parameter of the next request. How fast a view comes back is system story B4.

### B5. Notebook kernel dies mid-session

Actor: user in a notebook. Goal: after a restart, continue where they were.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
    tuned = client.compute(tune, {'bins': 1}, label='iofq')
    crash()                                        # the kernel dies inside the session

client = connect()
last = client.latest('iofq')
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': last.request.params['run']}, blanks=('bins',)))
    again = client.compute(tune, {'bins': 4}, label='iofq')

assert last == tuned
assert client.records(label='iofq') == [tuned, again]
assert client.output(again, 'iofq').values.tolist() == [1.0, 2.0, 3.0, 4.0]
```

The stage dies with the kernel, so the new session loads the run again. What the backend does with the dead session is system story B5.

### B6. Find last week's result

Actor: user after a week. Goal: find the reduction made last Tuesday and its parameters.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
clock.set(tuesday)
made = client.compute(IOFQ, {'run': run, 'threshold': 1.5})
clock.set(tuesday + timedelta(days=7))
client.compute(IOFQ, {'run': run, 'threshold': 2.5})

(found,) = [r for r in client.records(since=tuesday) if r.created < tuesday + timedelta(days=1)]
assert found == made
assert found.request.params == {'run': run, 'bins': 2, 'threshold': 1.5, 'can': None,
                                'beam_centre': None, 'normalization': None}
```

Gap: a record shows no creation time, although `client.records(since=)` filters by one.
That the record is still there after a week is system story B6.

## C. Chaining

### C1. Beam centre feeds a batch of sample reductions

Actor: user. Goal: use the beam centre as an input of every sample reduction; provenance reaches the beam-centre run.

```python
centre_run = measure(1, [1.0, 1.0, 1.0, 1.0])
centre = client.compute(BEAM_CENTRE, {'run': centre_run}, label='beam-centre')
samples = [measure(2, [2.0, 3.0, 4.0, 5.0], role='sample'),
           measure(3, [5.0, 4.0, 3.0, 2.0], role='sample')]
template = Template(IOFQ, params={'beam_centre': centre.ref('centre')}, blanks=('run',))

reduced = client.wait(client.submit(client.apply(template, samples, label='iofq')))

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[3.0, 7.0], [7.0, 3.0]]
assert set(client.provenance(reduced[0]).datasets()) == {samples[0], centre_run}
```

A beam centre found again later is a new record under `beam-centre`; the template keeps the reference it was made with until someone changes it.

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another backend published to SciCat.

```python
vanadium = other.compute(VANADIUM, {'run': measure(1, [1.0, 1.0]), 'scale': 2.0})
pid = other.publish(vanadium.ref('normalization'), 'scicat')

sample = measure(2, [4.0, 8.0])
result = client.compute(IOFQ, {'run': sample, 'normalization': dataset(pid=pid)})

assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]    # [4, 8] / ((1 + 1) * 2)
assert set(client.provenance(result).datasets()) == {sample, dataset(pid=pid)}
assert client.records() == [result]              # the vanadium record is on the other backend
```

Provenance ends at the published dataset, not at the vanadium run (see [Findings](#findings)). How the backend reads the published output is system story C2.

### C3. Per-bank diffraction results

Actor: DREAM user. Goal: plot one bank and feed another into an export.

```python
run = measure(1, [1.0, 2.0, 5.0, 6.0])
reduced = client.compute(BANKS, {'run': run})
exported = client.compute(EXPORT, {'data': [reduced.ref('high_resolution')]})

assert client.output(reduced, 'mantle').values.tolist() == [1.0, 2.0]    # the bank to plot
assert client.output(exported, 'text') == '5.0,6.0'
assert exported.request.params['data'] == [reduced.ref('high_resolution')]
```

### C4. Reflectometry angle series

Actor: reflectometry user. Goal: reduce four angles against a reference, stitch them, and export one file with one curve per angle.

```python
reference = measure(10, [2.0, 2.0], role='reference')
angles = [measure(11, [8.0, 4.0], angle=0.5), measure(12, [2.0, 1.0], angle=1.0),
          measure(13, [4.0, 2.0], angle=2.0), measure(14, [2.0, 0.5], angle=4.0)]
stitched = client.submit(STITCH, {'runs': angles, 'reference': reference})
exported = client.compute(EXPORT, {'data': stitched.ref('scaled')})

assert client.output(exported, 'text') == '4.0,2.0\n2.0,1.0\n1.0,0.5\n0.5,0.125'
assert set(client.provenance(exported).datasets()) == {reference, *angles}
```

Gap: list-valued outputs; core-api shows only single-valued data fields and does not say whether a reference to a list output fills a list parameter.
The stitch fits scale factors over all angles at once, so it is one spec over a list of runs, not an accumulation.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: adjust the vanadium processing and see the effect on a sample reduction at once.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
with client.session() as session:
    vanadium = session.stage(Template(VANADIUM, params={'run': vanadium_run}, blanks=('scale',)))
    reduce = session.stage(Template(IOFQ, params={'run': sample}, blanks=('normalization',)))
    for scale in (1.0, 2.0):
        processed = client.compute(vanadium, {'scale': scale}, label='vanadium')
        reduced = client.compute(reduce, {'normalization': processed.ref('normalization')},
                                 label='iofq')

assert [client.output(r, 'iofq').values.tolist() for r in client.records(label='iofq')] == [
    [2.0, 4.0], [1.0, 2.0]]                      # [4, 8] divided by 2, then by 4
assert reduced.request.params['normalization'] == client.latest('vanadium').ref('normalization')
```

The notebook is the driver: it reruns the sample after each vanadium change. That the change comes back quickly is system story C5.

## D. Batch

### D1. Temperature scan

Actor: instrument scientist. Goal: reduce a scan with one template, one temperature per run; plot three chosen temperatures; one corrupt file affects nothing else.

```python
temperatures = ['250K', '260K', '270K', '280K', '290K']
runs = [measure(n, [float(n)] * 4, temperature=t) for n, t in enumerate(temperatures, start=1)]
corrupt(runs[1])                                                     # 260K
template = Template(IOFQ, params={'bins': 1}, blanks=('run',))
client.wait(client.submit(client.apply(template, runs, label='scan', member_field='temperature')))

latest = {t: client.latest('scan', member=t) for t in temperatures}
assert [client.output(latest[t], 'iofq').values.tolist() for t in ('250K', '270K', '290K')] == [
    [4.0], [12.0], [20.0]]
assert {t: r.status for t, r in latest.items()} == {
    '250K': 'completed', '260K': 'failed', '270K': 'completed', '280K': 'completed',
    '290K': 'completed'}
```

Gap: how `apply` gives each dataset a member; `member_field` names the dataset field whose value becomes the member.
The member is the temperature, so the result at 250 K is `client.latest('scan', member='250K')`.

### D2. Overnight cluster batch

Actor: NMX user. Goal: submit thirty long runs, close the laptop, and the next day rerun the failed ones.

```python
runs = {str(n): measure(n, [float(n)] * 4) for n in range(1, 31)}
corrupt(runs['7'])                                                   # the transfer was cut short
for member, run in runs.items():
    client.submit(IOFQ, {'run': run}, label='night', member=member)

morning = connect()                                                  # the next day, a new client
latest = morning.wait([morning.latest('night', member=m) for m in runs])
failed = {m: r for m, r in zip(runs, latest) if r.status == 'failed'}
repair(runs['7'])                                                    # the transfer is repeated
reruns = morning.wait([morning.submit(r.request.spec, r.request.params, label='night', member=m)
                       for m, r in failed.items()])

assert list(failed) == ['7']
assert reruns[0].request == failed['7'].request
assert morning.latest('night', member='7') == reruns[0]
assert morning.output(reruns[0], 'iofq').values.tolist() == [14.0, 14.0]
assert len(morning.records(label='night')) == 31                     # the failed record stays
```

The next-day client finds the batch by its label and rebuilds the member list from run numbers it already knows.
That the runs continue while no client is connected is system story D2.

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: cancel a running batch of 500 with a wrong shared parameter, fix the parameter, and resubmit without waiting for the cancel.

```python
runs = [measure(n, [1.0, 1.0, 1.0, 1.0]) for n in range(1, 501)]
wrong = Template(IOFQ, params={'threshold': 20.0}, blanks=('run',))
first = client.submit(client.apply(wrong, runs, label='scan'))
client.cancel(first)
fixed = wrong.revise(params={'threshold': 0.5})
second = client.wait(client.submit(client.apply(fixed, runs, label='scan')))

assert {r.status for r in client.wait(first)} <= {'completed', 'cancelled'}
assert [client.output(r, 'iofq').values.tolist() for r in second] == 500 * [[2.0, 2.0]]
assert len(client.records(label='scan')) == 1000
```

Gap: `client.cancel(records)`.
Gap: revising a template; `revise` returns a new template with the named fields replaced.
That the cancel stops the started requests at once is system story D3.

### D4. Typo caught before 500 failures

Actor: user filling in a batch form. Goal: a parameter the params model rejects is caught before any record exists.

```python
runs = [measure(n, [1.0, 1.0]) for n in range(1, 501)]
typo = Template(IOFQ, params={'threshold': '2,5'}, blanks=('run',))
requests = client.apply(typo, runs, label='scan')

with pytest.raises(SubmitError, match='threshold'):
    client.submit(requests)
assert client.records() == []
```

Gap: `SubmitError` is not defined.

### D5. Understand why a run failed

Actor: user. Goal: read why a member failed, fix its input, and rerun that member.

```python
good = measure(1, [1.0, 1.0, 1.0, 1.0], temperature='250K')
bad = measure(2, [2.0, 2.0, 2.0, 2.0], temperature='260K')
corrupt(bad)
template = Template(IOFQ, blanks=('run',))
requests = client.apply(template, [good, bad], label='scan', member_field='temperature')
client.wait(client.submit(requests))

failed = client.latest('scan', member='260K')
repeat = measure(3, [2.0, 2.0, 2.0, 2.0], temperature='260K')     # the measurement is repeated
rerun = client.compute(IOFQ, {**failed.request.params, 'run': repeat}, label='scan', member='260K')

assert failed.failure.message == 'file signature not found'
assert client.latest('scan', member='260K') == rerun
assert client.output(rerun, 'iofq').values.tolist() == [4.0, 4.0]
assert client.records(label='scan')[-1] == rerun
assert failed.status == 'failed'                                     # the failure stays
```

Gap: `member_field`, as in D1.

### D6. Rerun last year's batch with a new workflow version

Actor: instrument scientist. Goal: reduce last year's batch again with a new spec version, keeping the old results.

```python
template = Template(IOFQ, params={'threshold': 1.5}, blanks=('run',))
runs = [measure(n, [1.0, 2.0, 3.0, 4.0]) for n in (1, 2, 3)]
client.wait(client.submit(client.apply(template, runs, label='scan')))

# a year later
last_year = client.records(label='scan')
runs = [r.request.params['run'] for r in last_year]
with pytest.raises(SubmitError, match='threshold'):
    client.submit(client.apply(template.revise(spec=IOFQ_V2), runs, label='scan'))
moved = template.revise(spec=IOFQ_V2, params={'mask_below': 1.5})
this_year = client.wait(client.submit(client.apply(moved, runs, label='scan')))

assert [client.output(r, 'iofq').values.tolist() for r in (last_year[0], this_year[0])] == [
    [2.0, 7.0], [0.0, 2.0, 3.0, 4.0]]
assert [r.request.params['bins'] for r in (last_year[0], this_year[0])] == [2, 4]   # defaults
assert len(client.records(label='scan')) == 6
```

Gap: revising a template, as in D3.
The old records keep the default of version 1, so the change of default shows in the records.

### D7. Rotation scan over a thousand angles

Actor: spectroscopy user. Goal: reduce a crystal rotation scan of a thousand runs, one run per angle, into one volume, and look at cuts through the volume while the scan continues.

```python
for n in range(1, 1001):
    measure(n, [1.0, float(n)], scan='17')                     # one run per angle
    if n == 500:
        measure(5, [1.0, 5.0], scan='17')                      # the file of run 5 arrives again

with client.session() as session:
    volume = session.accumulator(SUM.of(Counts))
    for run in islice(client.watch(Selector(scan='17')), 1000):
        volume.push(client.submit(ANGLE, {'run': run}))
        client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'index': 0},
                      label='cut', member='17')
    total = client.compute(volume)

angles = client.records(spec=ANGLE)
cuts = client.wait(client.records(label='cut'))
plain = client.compute(SUM.of(Counts), {'counts': [a.ref('counts') for a in angles]})
assert [client.output(c, 'cut').value for c in cuts] == [float(k) for k in range(1, 1001)]
assert client.output(total, 'counts').values.tolist() == [1000.0, 500500.0]
assert total.request == plain.request                          # the record of the plain request
assert len(angles) == 1000                                     # run 5 is reduced once
assert len(client.provenance(total).datasets()) == 1000
```

The story assumes that `watch` yields each dataset once, including those that exist when it starts.
That each angle runs on its own node as it arrives is system story D7.

## E. Automatic reduction

### E1. Series grows, reduction follows

Actor: reflectometry user during a beamtime. Goal: after each angle, the stitched curve grows by one angle; one curve per sample, not one per arrival.

```python
reference = measure(1, [1.0, 1.0], role='reference')
template = Template(STITCH, params={'reference': reference}, blanks=('runs',))
rule = Rule('reflectivity', template, selector=Selector(role='sample'), label='reflectivity',
            series='sample')
loop = TriggerLoop(client, [rule])

r2 = measure(2, [1.0, 2.0], role='sample', sample='si')
loop.step()
r4 = measure(4, [4.0, 8.0], role='sample', sample='si')
r3 = measure(3, [1.0, 1.0], role='sample', sample='si')
measure(2, [1.0, 2.0], role='sample', sample='si')             # arrives again
loop.step()

curve = client.latest('reflectivity', member='si')
assert len(client.records(label='reflectivity')) == 2         # one per step, not one per arrival
assert curve.request.params['runs'] == [r2, r3, r4]             # run order, not arrival order
assert client.output(curve, 'stitched').values.tolist() == [1.0, 2.0, 2.0, 2.0, 2.0, 4.0]
```

Gap: a rule that fills a list parameter with every dataset of the same series so far, in run order, one member per series value (`series=`).
Gap: `TriggerLoop.step()`, which handles the datasets that arrived since the last step and returns the records it submitted.
Each record stitches every angle so far; a stitch is not an accumulation (C4).

### E2. Automatic reduction goes quiet

Actor: instrument operator. Goal: after an upgrade removed the template's spec version, see why nothing is reduced.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
loop = TriggerLoop(client, [rule])
upgrade(specs=[IOFQ_V2])                                       # version 1 is gone
measure(1, [1.0, 1.0], role='sample')

assert loop.step() == []
assert loop.status(rule).reason == 'unknown spec sans-iofq version 1'
```

Gap: `TriggerLoop.step()`, as in E1.
Gap: `loop.status(rule)`, the reason a rule submitted nothing.

### E3. Reduction of our own output

Actor: none; a failure mode. Goal: a published result that SciCat lists as a dataset does not trigger the rule.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(),   # every dataset
            label='iofq')
loop = TriggerLoop(client, [rule])
measure(1, [1.0, 2.0, 3.0, 4.0])
(reduced,) = loop.step()
pid = client.publish(reduced.ref('iofq'), 'scicat')
catalogue.add_published(scicat.entries[pid])

assert loop.step() == []
```

Gap: nothing says whether a selector matches a published output that the catalogue lists as a dataset.

### E4. Template improved during a beamtime

Actor: instrument scientist. Goal: new runs use the improved template; earlier results stay; the results made before the improvement can be found.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
(before,) = TriggerLoop(client, [rule]).step()

improved = rule.revise(template=rule.template.revise(params={'threshold': 1.5}))
second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
(after,) = TriggerLoop(client, [improved]).step()

assert after.request.datasets() == [second]                     # run 1 is not reduced again
assert [client.output(r, 'iofq').values.tolist() for r in (before, after)] == [
    [3.0, 7.0], [2.0, 7.0]]
stale = [r for r in client.records(label='iofq') if r.request.params['threshold'] != 1.5]
assert [r.request.datasets() for r in stale] == [[first]]       # to reprocess, if the user wants
```

Gap: revising a rule and its template, as in D3.
Gap: replacing a rule in a running trigger loop (core-api open question 4); the story starts a new loop.

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague. Goal: the SciCat entry alone answers what raw files, parameters, and software produced it.

```python
centre_run, run = measure(1, [1.0, 1.0, 1.0, 1.0]), measure(2, [2.0, 3.0, 4.0, 5.0])
centre = client.compute(BEAM_CENTRE, {'run': centre_run})
result = client.compute(IOFQ, {'run': run, 'beam_centre': centre.ref('centre')})
pid = client.publish(result.ref('iofq'), 'scicat')

provenance = scicat.entries[pid].provenance                    # read without the client
assert provenance == client.provenance(result)
assert set(provenance.datasets()) == {centre_run, run}
assert [r.request.params for r in provenance.records()] == [centre.request.params]
assert {'essapps', 'scipp'} <= provenance.software.keys()
```

Gap: provenance shows only `.datasets()`; the story also reads `.records()` (as in S8) and `.software`, the package versions.

### F2. Reproduce after two upgrades

Actor: user. Goal: learn before anything runs that the exact result is not reproducible in the current environment; a rerun is an honestly labelled new record.

```python
result = client.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0]), 'threshold': 1.5})
upgrade(versions={'scipp': '99.0'})

with pytest.raises(SubmitError, match='scipp'):
    client.recompute(result)                                   # in the record's environment
again = client.compute(result.request.spec, result.request.params)   # in the current one

assert again.request == result.request
assert client.provenance(again).software['scipp'] == '99.0'
assert client.provenance(result).software['scipp'] != '99.0'
```

Gap: `client.recompute(record)`, which runs a request in its record's environment or refuses before running.
That the recorded environment can be installed again is system story F2.

### F3. Publish what was tuned interactively

Actor: user in a notebook. Goal: publish the tuned result; what enters SciCat is reproducible from its record.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold')))
    for bins, threshold in [(1, 0.0), (2, 0.0), (2, 1.5)]:
        tuned = client.compute(tune, {'bins': bins, 'threshold': threshold}, label='iofq')
pid = client.publish(tuned.ref('iofq'), 'scicat')

again = client.compute(IOFQ, tuned.request.params)
assert scicat.entries[pid].provenance == client.provenance(tuned)
assert again.request == tuned.request
assert [client.output(r, 'iofq').values.tolist() for r in (tuned, again)] == 2 * [[2.0, 7.0]]
```

### F4. Publish a corrected version

Actor: user. Goal: publish a correction that names what it supersedes; the old entry stays.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
bad = client.compute(IOFQ, {'run': run, 'threshold': 5.0})
old = client.publish(bad.ref('iofq'), 'scicat')
fixed = client.compute(IOFQ, {'run': run, 'threshold': 1.5})
new = client.publish(fixed.ref('iofq'), 'scicat', supersedes=old)

assert scicat.entries[new].supersedes == old
assert scicat.entries[old].provenance == client.provenance(bad)
```

Gap: `publish` takes no `supersedes`.

## G. Roles and deployment

### G1. Instrument scientist prepares a beamtime

Actor: instrument scientist, then an external user. Goal: results made in a commissioning proposal are readable by the users of the coming proposal; nothing else crosses proposals.

```python
scientist = connect(proposal='commissioning', user='anna')
user = connect(proposal='p2', user='eve')                      # p2 may read commissioning
vanadium = scientist.compute(VANADIUM, {'run': measure(1, [1.0, 1.0], proposal='commissioning')})
sample = measure(2, [2.0, 2.0, 2.0, 2.0], proposal='p2')
result = user.compute(IOFQ, {'run': sample, 'normalization': vanadium.ref('normalization')})

assert user.output(result, 'iofq').values.tolist() == [2.0, 2.0]   # [2, 2, 2, 2] / 2
with pytest.raises(SubmitError, match='p2'):
    scientist.compute(IOFQ, {'run': sample})
assert scientist.records() == [vanadium]
```

Gap: `SubmitError`, as in D4.
That an operator grants the read, and the backend enforces it, is system story G1.

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: run a workflow from a notebook without installing it; such records are marked and cannot be published.

```python
draft = make_iofq_workflow()                                  # IOFQ's implementation, being edited
dev = connect(bind={IOFQ: draft})
result = dev.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0])})

assert dev.provenance(result).software['sans-iofq'] == 'bound in notebook'
with pytest.raises(PublishError, match='bound in notebook'):
    dev.publish(result.ref('iofq'), 'scicat')
```

Gap: a client whose backend runs an implementation bound in the notebook (`bind=`), and a mark on its records.
Gap: `PublishError` is not defined.
That an edited binding takes effect without a restart is system story G2.

### G3. Local application, remote compute

System story only; see [system-stories.md](system-stories.md).

### G4. Two notebooks on one machine

Actor: user with two notebooks. Goal: one notebook uses a result made in the other.

```python
first, second = connect(), connect()                           # two notebooks, one proposal
centre = first.compute(BEAM_CENTRE, {'run': measure(1, [1.0, 1.0, 1.0, 1.0])}, label='beam-centre')
found = second.latest('beam-centre')
result = second.compute(IOFQ, {'run': measure(2, [2.0, 3.0, 4.0, 5.0]),
                               'beam_centre': found.ref('centre')})

assert found == centre
assert second.output(result, 'iofq').values.tolist() == [3.0, 7.0]
```

That both notebooks run on one machine without locking each other out is system story G4.

### G5. Reference across proposals refused

Actor: external user. Goal: a reference to another proposal's record is refused before any record exists.

```python
theirs, mine = connect(proposal='p1', user='anna'), connect(proposal='p2', user='eve')
centre = theirs.compute(BEAM_CENTRE, {'run': measure(1, [1.0, 1.0], proposal='p1')})
sample = measure(2, [2.0, 2.0], proposal='p2')

with pytest.raises(SubmitError, match='p1'):
    mine.compute(IOFQ, {'run': sample, 'beam_centre': centre.ref('centre')})
assert mine.records() == []
```

Gap: `SubmitError`, as in D4.
That no client can get around the refusal is system story G5.

## H. Operations

### H1. Disk fills up

System story only; see [system-stories.md](system-stories.md).

### H2. Backend upgrade with runs in flight

System story only; see [system-stories.md](system-stories.md).

### H3. Proposal ends

System story only; see [system-stories.md](system-stories.md).

## Findings

Where the API gets awkward or lacks something, by theme.
The stories name each gap in a "Gap" line; these are the decisions behind them.

### Records and requests

1. **What a list submission returns (S4, C1).** `client.submit([...])` returns the records in request order, and a `Request.ref(...)` used in a later request becomes a reference to the submitted record. core-api shows neither.
2. **Status and failure (D1, D2, D3, D5).** The stories use the statuses `'completed'`, `'failed'`, `'cancelled'`, and `failure.message`, and assume that `client.wait` returns failed records instead of raising. core-api should also say that a *finished* record never changes, since a pending one does.
3. **Errors (A2, D4, D6, F2, G1, G2, G5).** No exceptions are defined. The stories use `SubmitError` for an invalid value, an unknown spec version, an unknown run number, or a refused reference, and `PublishError` for a refused publication. A batch form (D4) needs the field that is wrong, and would like the check as early as `apply`.
4. **Cancel (D3).** No `client.cancel(records)`.
5. **Record time (B6).** `client.records(since=)` filters by a time that records do not show. Suggestion: `record.created`, and `until=`.
6. **Output selection (S3).** A request cannot select outputs, so an intermediate exists only if the author declared it, and then every record has it. Suggestion: keep it so, and leave to the system whether an output nobody reads is stored.
7. **List-valued outputs (C3, C4).** STITCH returns one curve per angle and EXPORT takes a list. core-api does not say whether a list output fills a list parameter, or how to reference one entry. Suggestion: it does when the elements agree, and `ref('scaled', i)` names one entry. C3 with fixed per-bank outputs checks nothing new; it matters only for a varying number of banks, which is this case.
8. **Views (B4).** `client.output` reads a whole output; dragging through cuts needs a read of part of one, which makes no record.

### Datasets

9. **Naming datasets (A1, A4, C2).** `dataset()` is shown only with `run=`. The stories need `path=` and `pid=`, and a decision whether a record keeps what the user typed beside the identity. A4 argues against keeping a local path on a shared service.
10. **Listing and watching datasets (A1, S7, C1, D7, E1).** No call lists datasets; a browsing UI or batch form needs `client.datasets(selector)` beside `client.watch(selector)`. For `watch`, core-api must say whether it yields datasets that exist when it starts, and whether a file that arrives again is yielded again. D7 assumes existing ones first, each once; otherwise an accumulator counts a run twice.
11. **Metadata (A5).** No call reads a dataset's current metadata (`client.metadata(dataset)`).
12. **Removing a dataset (A4).** No call removes a dataset, and failure kinds such as `missing-dataset` are not defined. Open: whether outputs derived from it go too, since they may be as sensitive as the file.
13. **Published outputs as datasets (E3, C2).** A published output listed by the catalogue must stay usable as an input (C2), but a rule over raw data must not match it (E3). Suggestion: a dataset shows whether it is raw, and a selector matches only raw datasets unless it asks for others.

### Labels and batches

14. **Members in a batch (D1, D2, D5).** `apply` has no way to give each dataset a member; the stories use `member_field='temperature'`. A record does not show its label or member, so D2 passes them again when it reruns a failed record. Suggestion: `record.label` and `record.member`; `apply` and `Rule` take the same `member_field`.
15. **Listing a label's members (D2).** A next-day client or a colleague needs `client.members(label)` → `{member: latest record}`; D2 rebuilds the list from run numbers it happens to know.
16. **Listing labels (B3).** A UI that shows a user's variants needs to list labels, and perhaps hide one.

### Templates, lookups, rules, and the trigger loop

17. **Template from a record (B1).** No `Template.from_request(request, blanks=...)`.
18. **Revising (D3, D6, E4).** The stories use `revise(...)`. If `Template` and `Rule` are frozen dataclasses, `dataclasses.replace` does it and nothing needs adding.
19. **Records do not name their template (E4).** "Every record names the template version" cannot be checked, since a record holds the request, not the template it was filled from. Suggestion: drop that part of E4's goal; a template stays unnamed plain data, and the request says everything that determines the result.
20. **Lookups (S7).** Lookups appear only in prose. S7 needs a lookup type (the latest dataset matching a selector measured before this one), `apply(..., lookup=)`, and a rule for which blank the dataset fills when a template has several. Suggestion: the dataset fills the one blank the lookup leaves; `apply` and `Rule` take the same lookup.
21. **A series under a rule (E1).** A rule fills one dataset per arrival. E1 needs a rule that fills a list parameter with every dataset of the same sample so far, in run order, one member per sample (`series='sample'`).
22. **Trigger loop calls (E1 to E4).** Tests need `loop.step()`, which handles what arrived since the last step and returns the records it submitted; E2 needs `loop.status(rule)`. Suggestion: the loop checks each rule's template when it is given the rule, and reads what it has handled from the records under the rule's label, so that a restarted or replaced loop needs no memory of its own (E4 starts a new loop and must not reduce run 1 again).
23. **Where templates and rules live (B1, E4).** "The beamtime's template" lives only in the notebook, and E4 replaces a rule by starting a new loop. Joins core-api open question 4.

### Holders and sessions

24. **A session whose client dies (B5, H2).** A dying kernel never leaves `with client.session()`. The guarantee "ending their session releases them" needs a second clause: the backend ends a session whose client is gone. A call through a stage whose session was ended, for example by a backend restart, needs a defined error.
25. **Where a session runs (G3).** G3's application must choose it: this session on the laptop, the reductions on the cluster. Either `client.session()` takes a placement, or a desktop application runs its own backend that references records on the cluster's. G3 stays in the system tier until this is decided.
26. **Accumulator records grow with every read (D7).** Each `client.submit(volume)` makes a record naming every element so far: a thousand reads give 500,500 references. An accumulator's outputs model is its element model, so its record could instead be the accumulator spec over the previous record's output and the elements pushed since. That is still the record of a plain request, and provenance still reaches every angle. core-api currently says "over the elements pushed so far", which rules this out; to decide.

### Provenance and publication

27. **Reading provenance (S8, F1, F3).** Provenance offers only `.datasets()`. The stories need `.records()` (the records a record reads, through all its inputs), `.software` (package versions), and the record's own request. Provenance must be plain data that a publisher stores and that compares equal.
28. **Provenance through a published output (C2).** A record that reads a published output names that dataset, so its provenance stops there and does not reach the vanadium run. Either provenance follows the published entry, or "reaches raw datasets" is weakened across a publication.
29. **Grants and ended proposals (G1, H3).** A p2 record references a commissioning record. When the commissioning proposal is dropped, the p2 record's provenance loses a step. The system must say what of a dropped proposal is kept while another references it.
30. **Recompute (F2).** A rerun in the current environment is plain `compute`. Missing is a way to ask for the record's environment and be refused before anything runs (`client.recompute(record)`).
31. **Publishing a correction (F4).** `publish` takes no `supersedes`.
32. **Implementations bound in a notebook (G2).** Nothing runs a spec whose implementation is defined in the notebook, or marks its records; G2 uses `connect(bind={IOFQ: draft})` and a `publish` that refuses such records.
33. **F3 repeats S2 and F1.** It adds one check, that a record made through a stage can be published like the plain request's record; it could be one assertion in F1.

### System effects that show through the API

34. **Dropped outputs (H1).** core-api does not say what `client.output` does for a dropped output, or what a new request that references it does: fail, or compute it again.

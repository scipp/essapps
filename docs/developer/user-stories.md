# User stories

Each story has an actor, a goal, the client code that reaches it, and its checks as assertions.
The code uses only the API of [README.md](README.md) and the calls listed under [Conventions](#conventions).
The checks observe only what that API shows: values, records, labels, provenance, and errors.
What a story needs from the system, such as cost, placement, or persistence, is a separate story in [system-stories.md](system-stories.md).
A story that needs what the design leaves open or defers says so in a "Gap" line, and [Open](#open) lists these.

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
| `VOLUME` | accumulator spec, element `Counts` (`counts`) | `counts` | the sum | the accumulation of a rotation scan |
| `STITCH` | `runs: list`, `reference` | `stitched` | each run's counts divided by the reference's counts, times the factor that makes its first value equal the last value of the curve before it, with the first curve not scaled; these curves concatenated | a reflectometry reduction that stitches angles with scale factors fitted over all of them |
| `EXPORT` | `data` | `text` | the values, separated by commas | writing a file for another program |
| `IOFQ_V2` | as `IOFQ`, with `threshold` renamed `mask_below`, and `bins=4` | `iofq`, `masked` | as `IOFQ` | version 2 of `IOFQ`: same name, `sans-iofq`, a renamed parameter and a new default |

`FINALIZE` over `PARTS_SUM` over `CONTRIBUTE` computes what `NORMALIZE` computes.
A story may add a toy spec to this table; it must take runs directly and be checkable by hand.

## Conventions

Fixtures: `client` is `connect(url, proposal='p1')`, a client of a fresh hosted backend at `url`.
In the stories, `connect(proposal=..., user=...)` is `connect(url, ...)` to the same backend, by default for `client`'s proposal and user; `user=` stands for logging in as another user.
`other` is a client of a second hosted backend.
Every backend in the stories, including one that `local(...)` makes, reads the same datasets and publishes to the same `scicat`.
`measure(n, counts, **fields)` makes run `n` appear as a raw dataset with the given counts and metadata, and returns its reference.
The metadata of such a dataset also holds its run number, as `run`; `measure(..., proposal=...)` makes the dataset belong to another proposal.
`datasets` is the fake dataset source of every backend; stories query it through `client.datasets` as in README.md.
It has two helpers for tests: `datasets.correct(dataset, **fields)` changes a dataset's metadata, and `datasets.add_published(entry)` lists a published entry as a derived dataset, as SciCat does, and returns its reference.
`scicat` is a fake publisher; `scicat.entries[pid]` is a published entry, with `.provenance` and `.supersedes`.
`folder` is a directory with files that hold counts, as `measure` datasets do.
`clock` is a fake clock the backend reads; `crash()` ends the notebook's process without cleanup.
`corrupt(run)` makes a dataset unreadable, with the failure message `'file signature not found'`; `repair(run)` undoes it.
`upgrade(specs=..., versions=...)` returns a client of an upgraded backend over the same datasets: the specs it offers and the software versions its records name.
`replace` is `dataclasses.replace`.

Besides the calls in README.md, the stories use these:

```python
client.records(spec=IOFQ)    # records, oldest first; filters by spec= as by label=, since=, until=
record.request.datasets()    # the datasets the request names directly
```

The trigger loop belongs to the sub-design for batch and automatic reduction.
The stories use two of its calls:

```python
loop.step()                  # handles what arrived since the last step; returns the records it made
loop.status(rule).reason     # why a rule submitted nothing
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
assert sc.identical(client.output(plain, 'iofq'), client.output(result, 'iofq'))
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
c611, c612, summed = client.submit([*parts, total])          # returns at once

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

This is way 1 of "One sum, three ways" in the README. B2 uses ways 2 and 3.

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

requests = apply(template, [first, second], client.datasets, member_field='run', lookup=cans)
reduced = list(client.compute(requests, label='iofq').values())

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[4.0, 5.0], [4.0, 7.0]]
assert [r.request.datasets() for r in reduced] == [[first, can_1], [second, can_3]]
```

Each dataset fills the one blank the lookup leaves.

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

### A3. Work without the facility mount

System story only; see [system-stories.md](system-stories.md).

### A4. Mistaken copy into the shared service

Actor: user of the shared service. Goal: remove a file that should not have left their machine.

```python
private_file = next(folder.glob('*.h5'))                # should have stayed on the laptop
first = client.compute(IOFQ, {'run': dataset(path=private_file)})
(uploaded,) = first.request.datasets()
client.remove(uploaded)

with pytest.raises(SubmitError, match='run'):
    client.compute(IOFQ, first.request.params)
assert client.records(spec=IOFQ) == [first]             # the first record stays
```

Gap: no call removes a dataset. This is deferred, together with whether the outputs derived from it go too.
That no copy of the file stays in the service is system story A4.

### A5. Metadata corrected after the fact

Actor: instrument scientist. Goal: see the corrected sample name of a run already reduced.

```python
runs = [measure(n, [1.0, 2.0], sample='water') for n in (1, 2, 3)]
reduced = [client.compute(IOFQ, {'run': run}) for run in runs]
datasets.correct(runs[1], sample='heavy water')

(named,) = reduced[1].request.datasets()
assert named == runs[1]                                  # the correction keeps the dataset
assert client.datasets.metadata(named)['sample'] == 'heavy water'
```

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
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
new = measure(2, [2.0, 1.0, 4.0, 3.0])
requests = apply(beamtime, [new], client.datasets, member_field='run')
(reduced,) = client.compute(requests, label='iofq-beamtime').values()

assert len(client.records(label='iofq')) == 4
assert (beamtime.params['bins'], beamtime.params['threshold']) == (2, 1.5)
assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]    # [2, 0, 4, 3] in 2 groups
```

The template is plain data, kept in the notebook or in a file. That each change comes back quickly is system story B1.

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
        total.push(parts[run].refs('numerator', 'denominator'))
    first = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

    parts[r613] = client.compute(contribute, {'run': r613})
    total.push(parts[r613].refs('numerator', 'denominator'))
    added = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

kept = [parts[r611], parts[r613]]
summed = client.compute(PARTS_SUM, {'numerator': [p.ref('numerator') for p in kept],
                                    'denominator': [p.ref('denominator') for p in kept]})
removed = client.compute(FINALIZE, summed.refs(), label='sum')

assert [client.output(r, 'normalized').values.tolist() for r in (first, added, removed)] == [
    [0.25, 0.75], [0.375, 0.625], [0.5, 0.5]]
way_1 = client.compute(NORMALIZE, {'runs': [r611, r612]})
assert client.output(way_1, 'normalized').values.tolist() == [0.25, 0.75]
assert client.provenance(removed).datasets() == [r611, r613]
assert client.records(label='sum') == [first, added, removed]
assert len(client.records(spec=CONTRIBUTE)) == 3                     # each run reduced once
```

Gap: removing uses a plain request over the kept contributions, because an accumulator has no `remove` (README.md open question 3).
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

Gap: the form of a view waits for the plotting work; `client.output(..., index=)` stands in for it.
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

(found,) = client.records(since=tuesday, until=tuesday + timedelta(days=1))
assert found == made
assert found.created == tuesday
assert found.request.params == {'run': run, 'bins': 2, 'threshold': 1.5, 'can': None,
                                'beam_centre': None, 'normalization': None}
```

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

requests = apply(template, samples, client.datasets, member_field='run')
reduced = list(client.compute(requests, label='iofq').values())

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[3.0, 7.0], [7.0, 3.0]]
assert set(client.provenance(reduced[0]).datasets()) == {samples[0], centre_run}
```

A beam centre found again later is a new record under `beam-centre`; the template keeps the reference it was made with until someone changes it.

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another backend published to SciCat.

```python
vanadium = other.compute(VANADIUM, {'run': measure(1, [1.0, 1.0]), 'scale': 2.0})
pid = other.publish(vanadium.ref('normalization'), 'scicat')
published = datasets.add_published(scicat.entries[pid])

sample = measure(2, [4.0, 8.0])
result = client.compute(IOFQ, {'run': sample, 'normalization': dataset(pid=pid)})

assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]    # [4, 8] / ((1 + 1) * 2)
assert set(client.provenance(result).datasets()) == {sample, published}
assert client.records() == [result]              # the vanadium record is on the other backend
```

Provenance stops at the published dataset, since what lies behind a dataset belongs to its source. How the backend reads the published output is system story C2.

### C4. Reflectometry angle series

Actor: reflectometry user. Goal: reduce four angles against a reference, stitch them, and export the stitched curve to a file.

```python
reference = measure(10, [2.0, 2.0], role='reference')
angles = [measure(11, [8.0, 4.0], angle=0.5), measure(12, [2.0, 1.0], angle=1.0),
          measure(13, [4.0, 2.0], angle=2.0), measure(14, [2.0, 0.5], angle=4.0)]
stitched = client.submit(STITCH, {'runs': angles, 'reference': reference})
exported = client.compute(EXPORT, {'data': stitched.ref('stitched')})

assert client.output(exported, 'text') == '4.0,2.0,2.0,1.0,1.0,0.5,0.5,0.125'
assert set(client.provenance(exported).datasets()) == {reference, *angles}
```

The stitch fits scale factors over all angles at once, so it is one spec over a list of runs, not an accumulation.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: adjust the vanadium processing and see the effect on a sample reduction at once.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
with client.session() as session:
    vanadium = session.stage(Template(VANADIUM, params={'run': vanadium_run}, blanks=('scale',)))
    reduce = session.stage(Template(IOFQ, params={'run': sample}, blanks=('normalization',)))
    reduced = []
    for scale in (1.0, 2.0):
        processed = client.compute(vanadium, {'scale': scale}, label='vanadium')
        reduced.append(client.compute(reduce, {'normalization': processed.ref('normalization')},
                                      label='iofq'))

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
    [2.0, 4.0], [1.0, 2.0]]                      # [4, 8] divided by 2, then by 4
assert client.records(label='iofq') == reduced
assert reduced[-1].request.params['normalization'] == client.latest('vanadium').ref('normalization')
```

The notebook is the driver: it reruns the sample after each vanadium change. It keeps the records whose outputs it reads later, since a label keeps no values. That the change comes back quickly is system story C5.

## D. Batch

### D1. Temperature scan

Actor: instrument scientist. Goal: reduce a scan with one template, one temperature per run; plot three chosen temperatures; one corrupt file affects nothing else.

```python
temperatures = ['250K', '260K', '270K', '280K', '290K']
runs = [measure(n, [float(n)] * 4, temperature=t) for n, t in enumerate(temperatures, start=1)]
corrupt(runs[1])                                                     # 260K
template = Template(IOFQ, params={'bins': 1}, blanks=('run',))
requests = apply(template, runs, client.datasets, member_field='temperature')
scan = client.compute(requests, label='scan')                        # keyed by temperature

assert [client.output(scan[t], 'iofq').values.tolist() for t in ('250K', '270K', '290K')] == [
    [4.0], [12.0], [20.0]]
assert client.wait(scan) == {
    '250K': 'completed', '260K': 'failed', '270K': 'completed', '280K': 'completed',
    '290K': 'completed'}
assert client.members('scan') == scan
```

Gap: `member_field` and `client.members` are tentative (README.md open question 4).
Later, the result at 250 K is `client.latest('scan', member='250K')`.

### D2. Overnight cluster batch

Actor: NMX user. Goal: submit thirty long runs, close the laptop, and the next day rerun the failed ones.

```python
runs = {str(n): measure(n, [float(n)] * 4) for n in range(1, 31)}
corrupt(runs['7'])                                                   # the transfer was cut short
for member, run in runs.items():
    client.submit(IOFQ, {'run': run}, label='night', member=member)

morning = connect()                                                  # the next day, a new client
night = morning.members('night')
failed = [night[m] for m, s in morning.wait(night).items() if s == 'failed']
repair(runs['7'])                                                    # the transfer is repeated
reruns = [morning.submit(r.request.spec, r.request.params, label=r.label, member=r.member)
          for r in failed]
morning.wait(reruns)

assert [r.member for r in failed] == ['7']
assert reruns[0].request == failed[0].request
assert morning.latest('night', member='7') == reruns[0]
assert morning.output(reruns[0], 'iofq').values.tolist() == [14.0, 14.0]
assert len(morning.records(label='night')) == 31                     # the failed record stays
```

Gap: labels and members on records, and `client.members`, are tentative, as in D1.
That the runs continue while no client is connected is system story D2.

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: cancel a running batch of 500 with a wrong shared parameter, fix the parameter, and resubmit without waiting for the cancel.

```python
runs = [measure(n, [1.0, 1.0, 1.0, 1.0]) for n in range(1, 501)]
wrong = Template(IOFQ, params={'threshold': 20.0}, blanks=('run',))
first = client.submit(apply(wrong, runs, client.datasets, member_field='run'), label='scan')
client.cancel(first)
fixed = replace(wrong, params={'threshold': 0.5})
second = client.compute(apply(fixed, runs, client.datasets, member_field='run'), label='scan')

assert set(client.wait(first).values()) <= {'completed', 'cancelled'}
assert [client.output(r, 'iofq').values.tolist() for r in second.values()] == 500 * [[2.0, 2.0]]
assert len(client.records(label='scan')) == 1000
```

That the cancel stops the started requests at once is system story D3.

### D4. Typo caught before 500 failures

Actor: user filling in a batch form. Goal: a parameter the params model rejects is caught before any record exists.

```python
runs = [measure(n, [1.0, 1.0]) for n in range(1, 501)]
typo = Template(IOFQ, params={'threshold': '2,5'}, blanks=('run',))
requests = apply(typo, runs, client.datasets, member_field='run')

with pytest.raises(SubmitError, match='threshold'):
    client.submit(requests, label='scan')
assert client.records() == []
```

### D5. Understand why a run failed

Actor: user. Goal: read why a member failed, fix its input, and rerun that member.

```python
good = measure(1, [1.0, 1.0, 1.0, 1.0], temperature='250K')
bad = measure(2, [2.0, 2.0, 2.0, 2.0], temperature='260K')
corrupt(bad)
template = Template(IOFQ, blanks=('run',))
requests = apply(template, [good, bad], client.datasets, member_field='temperature')
failed = client.compute(requests, label='scan')['260K']

repeat = measure(3, [2.0, 2.0, 2.0, 2.0], temperature='260K')     # the measurement is repeated
rerun = client.compute(IOFQ, {**failed.request.params, 'run': repeat}, label=failed.label,
                       member=failed.member)

assert client.failure(failed) == 'file signature not found'
assert client.latest('scan', member='260K') == rerun
assert client.output(rerun, 'iofq').values.tolist() == [4.0, 4.0]
assert client.records(label='scan')[-1] == rerun
assert client.status(failed) == 'failed'                                   # the failure stays
```

Gap: `member_field`, and labels and members on records, are tentative, as in D1.

### D6. Rerun a batch with a new workflow version

Actor: instrument scientist. Goal: reduce an earlier batch of the proposal again with a new spec version, keeping the first results.

```python
template = Template(IOFQ, params={'threshold': 1.5}, blanks=('run',))
runs = [measure(n, [1.0, 2.0, 3.0, 4.0]) for n in (1, 2, 3)]
client.compute(apply(template, runs, client.datasets, member_field='run'), label='scan')

# weeks later, version 2 of the spec is released
before = client.records(label='scan')
runs = [r.request.params['run'] for r in before]
renamed = apply(replace(template, spec=IOFQ_V2), runs, client.datasets, member_field='run')
with pytest.raises(SubmitError, match='threshold'):
    client.submit(renamed, label='scan')
moved = replace(template, spec=IOFQ_V2, params={'mask_below': 1.5})
requests = apply(moved, runs, client.datasets, member_field='run')
after = list(client.compute(requests, label='scan').values())

assert [client.output(r, 'iofq').values.tolist() for r in (before[0], after[0])] == [
    [2.0, 7.0], [0.0, 2.0, 3.0, 4.0]]
assert [r.request.params['bins'] for r in (before[0], after[0])] == [2, 4]   # defaults
assert len(client.records(label='scan')) == 6
```

The first records keep the default of version 1, so the change of default shows in the records.

Gap: reading `before[0]`'s output weeks later needs it to have been saved, since only history is kept that long ([system.md](system.md), Values). Saving belongs to the provenance and publication sub-design.

### D7. Rotation scan over a thousand angles

Actor: spectroscopy user. Goal: reduce a crystal rotation scan of a thousand runs, one run per angle, into one volume, and look at cuts through the volume while the scan continues.

```python
for n in range(1, 1001):
    measure(n, [1.0, float(n)], scan='17')                     # one run per angle
    if n == 500:
        measure(5, [1.0, 5.0], scan='17')                      # the file of run 5 arrives again

cuts, pushed = [], []
with client.session() as session:
    volume = session.accumulator(VOLUME)
    angles = (client.submit(ANGLE, {'run': run})
              for run in islice(client.datasets.watch(Selector(scan='17')), 1000))
    for angle in client.as_completed(angles):                  # in the order they finish
        volume.push(angle.refs())
        pushed.append(angle)
        cuts.append(client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'index': 0},
                                  label='cut', member='17'))
    total = client.compute(volume)

assert [client.output(c, 'cut').value for c in cuts] == [float(k) for k in range(1, 1001)]
assert client.output(total, 'counts').values.tolist() == [1000.0, 500500.0]
assert client.provenance(total).records() == pushed            # the angles, in push order
assert len(client.records(spec=ANGLE)) == 1000                 # run 5 is reduced once
assert len(client.provenance(total).datasets()) == 1000
```

`watch` yields run 5 once, although its file arrives twice.
The angles are reduced in parallel and pushed in the order they finish, so the volume's request lists them in that order.
The notebook keeps the cuts it reads; the volume of each snapshot is released once its cut has run.
That each angle runs on its own node as it arrives, and how the thousand records of the volume are stored, is system story D7.

## E. Automatic reduction

### E1. Series grows, reduction follows

Actor: reflectometry user during a beamtime. Goal: after each angle, the stitched curve grows by one angle; one curve per sample, not one per arrival.

```python
reference = measure(1, [1.0, 1.0], role='reference')
template = Template(STITCH, params={'reference': reference}, blanks=('runs',))
rule = Rule('reflectivity', template, selector=Selector(role='sample'), series='sample',
            label='reflectivity')
loop = TriggerLoop(client, rules=[rule])

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

Each record stitches every angle so far. A stitch is not an accumulation (C4).

Gap: no client holds the curve the loop made, so reading its output needs the rule to save what it makes ([system.md](system.md), Values). Saving belongs to the provenance and publication sub-design.

### E2. Automatic reduction goes quiet

Actor: instrument operator. Goal: after an upgrade removed the template's spec version, see why nothing is reduced.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
upgraded = upgrade(specs=[IOFQ_V2])                            # version 1 is gone
loop = TriggerLoop(upgraded, rules=[rule])
measure(1, [1.0, 1.0], role='sample')

assert loop.step() == []
assert loop.status(rule).reason == '1: unknown spec sans-iofq/v1'   # member 1
```

### E3. Reduction of our own output

Actor: none; a failure mode. Goal: a published result that SciCat lists as a dataset does not trigger the rule.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(),   # every raw dataset
            label='iofq')
loop = TriggerLoop(client, rules=[rule])
measure(1, [1.0, 2.0, 3.0, 4.0])
(reduced,) = loop.step()
pid = client.publish(reduced.ref('iofq'), 'scicat')
datasets.add_published(scicat.entries[pid])

assert loop.step() == []
```

A published entry is a derived dataset, and a selector matches only raw datasets unless it names another kind.

### E4. Template improved during a beamtime

Actor: instrument scientist. Goal: new runs use the improved template; earlier results stay; the results made before the improvement can be found.

```python
rule = Rule('auto-iofq', Template(IOFQ, blanks=('run',)), selector=Selector(role='sample'),
            label='iofq')
first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
(before,) = TriggerLoop(client, rules=[rule]).step()

improved = replace(rule, template=replace(rule.template, params={'threshold': 1.5}))
second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
(after,) = TriggerLoop(client, rules=[improved]).step()

assert after.request.datasets() == [second]                     # run 1 is not reduced again
assert [client.output(r, 'iofq').values.tolist() for r in (before, after)] == [
    [3.0, 7.0], [2.0, 7.0]]
stale = [r for r in client.records(label='iofq') if r.request.params['threshold'] != 1.5]
assert [r.request.datasets() for r in stale] == [[first]]       # to reprocess, if the user wants
```

The second loop stands in for the driving server replacing the rule. It learns that run 1 is handled from the records under `iofq`.

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague. Goal: the SciCat entry alone answers what raw files, parameters, and software produced it.

```python
centre_run, run = measure(1, [1.0, 1.0, 1.0, 1.0]), measure(2, [2.0, 3.0, 4.0, 5.0])
centre = client.compute(BEAM_CENTRE, {'run': centre_run})
with client.session() as session:                               # tuned before publishing
    tune = session.stage(Template(IOFQ, params={'run': run, 'beam_centre': centre.ref('centre')},
                                  blanks=('threshold',)))
    result = client.compute(tune, {'threshold': 1.5})
pid = client.publish(result.ref('iofq'), 'scicat')
plain = client.compute(IOFQ, result.request.params)             # the same request, without a stage

provenance = scicat.entries[pid].provenance                    # read without the client
assert provenance == client.provenance(result) == client.provenance(plain)
assert set(provenance.datasets()) == {centre_run, run}
assert [r.request.params for r in provenance.records()] == [centre.request.params]
assert {'essapps', 'scipp'} <= provenance.software.keys()
```

The entry carries what lasts. The history behind it expires after the retention period (system story H3).

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

Gap: `client.recompute(record)`, which runs a request in its record's environment or refuses before running, is deferred.
That the recorded environment can be installed again is system story F2.

### F4. Publish a corrected version (deferred)

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

Gap: `publish` takes no `supersedes`. Corrections that supersede a published entry are deferred.

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

That an operator grants the read, and the backend enforces it, is system story G1.

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: run a workflow defined in a notebook without installing it; its records say what ran.

```python
draft = make_iofq_workflow()                                  # IOFQ's implementation, being edited
dev = local(proposal='p1', bind={IOFQ: draft})                # a backend in the notebook's process
result = dev.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0])})
pid = dev.publish(result.ref('iofq'), 'scicat')               # publishing is not refused

assert dev.provenance(result).software['sans-iofq'] == 'bound in notebook'
assert scicat.entries[pid].provenance == dev.provenance(result)
```

That a hosted backend runs only installed workflows, and that an edited binding takes effect for the next request, is system story G2.

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

Both notebooks are clients of one backend, such as a hosted one; a backend in each notebook shares nothing.

Gap: without a shared backend, the first notebook saves the centre to a folder both can read, and the second names the file as a dataset, `dataset(path=...)`, instead of referencing the output. Saving belongs to the provenance and publication sub-design.

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

That no client can get around the refusal is system story G5.

## H. Operations

### H1. Disk fills up

System story only; see [system-stories.md](system-stories.md).

### H2. Backend upgrade with runs in flight

System story only; see [system-stories.md](system-stories.md).

### H3. Records expire

System story only; see [system-stories.md](system-stories.md).

## Open

What the design leaves open or defers, with the stories each item affects.

- **Removing a dataset** (A4): deferred, together with whether the outputs derived from it go too.
- **Saving** (D6, E1, G4): an output read after no client holds it must have been saved ([system.md](system.md), Values). Saving belongs to the provenance and publication sub-design.
- **Views** (B4): the form of a read of part of an output waits for the plotting work.
- **Removing an element from an accumulator** (B2): README.md open question 3.
- **Labels and members** (D1, D2, D5, and every story that calls `apply`): `member_field`, `client.members`, and labels and members on records are tentative; README.md open question 4.
- **Grouping** (system story D7): how an author declares that grouping does not change the result of an accumulator spec; README.md open question 1.
- **Placing a session** (system story G3): the name and values of the placement argument; README.md open question 2.
- **Recomputing in a record's environment** (F2): deferred.
- **Publishing a correction** (F4): deferred.

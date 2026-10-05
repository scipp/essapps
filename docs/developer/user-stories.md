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
| `NORMALIZE` | `runs`: a table of rows with a field `run`; `scale=1.0` | `normalized` | the counts summed over the runs, divided by their total, times `scale` | a sum of runs, such as a SANS I(Q) |
| `BACKGROUND` | `sample_runs: list`, `background_runs: list` | `subtracted` | summed sample counts minus summed background counts | a sum of two sets of runs |
| `CONTRIBUTE` | `run` | `numerator`, `denominator`, `transmission` | the counts; their total; the first count divided by the total | a reduction of one run whose outputs another request sums |
| `PARTS_SUM` | `parts`: a table of `NormalizationParts` (`numerator`, `denominator`) | `numerator`, `denominator` | each field summed over the rows | a sum of outputs of other records |
| `ANGLE` | `run` | `counts` | the counts | a reduction of one run into a volume |
| `CUT` | `data`, `index` | `cut` | the value at `index` | a cut through a volume, from another package |
| `VOLUME` | `runs`: a table of rows with a field `run` | `counts` | the counts summed over the runs | a rotation scan reduced into one volume, one run per angle |
| `COPY` | `data` | `data` | a copy of `data` | a record of a state of an accumulator |
| `STITCH` | `runs: list`, `reference` | `stitched` | each run's counts divided by the reference's counts, times the factor that makes its first value equal the last value of the curve before it, with the first curve not scaled; these curves concatenated | a reflectometry reduction that stitches angles with scale factors fitted over all of them |
| `EXPORT` | `data` | `text` | the values, separated by commas | writing a file for another program |
| `IOFQ_V2` | as `IOFQ`, with `threshold` renamed `mask_below`, and `bins=4` | `iofq`, `masked` | as `IOFQ` | version 2 of `IOFQ`: same name, `sans-iofq`, a renamed parameter and a new default |

The binding of `IOFQ` is the function `iofq`.
`NORMALIZE` and `VOLUME` are bound to `Summing`, a toy `StreamProcessor`: it sums the counts of each table's runs, and computes the outputs from the sums, so it can be an accumulator.
`PARTS_SUM` is bound to `combine(operator.add)`.
A story may add a toy spec to this table; it must take runs directly and be checkable by hand.

## Conventions

Fixtures: `client` is a client for proposal `p1`.
In the notebook stories it is `local(proposal='p1', datasets=datasets, bind=...)`, a backend in the user's process ([ADR 0002](adr/0002-the-client-is-the-lifetime.md)).
In the batch and automatic stories, sections D and E, it is `connect(url, proposal='p1')`, a client of the service at `url` ([ADR 0005](adr/0005-the-service-writes-every-output.md)), which is designed and not implemented.
The story tests make a `Client` of one in-process `Backend` for both.
In the stories, `connect(proposal=..., user=...)` is `connect(url, ...)` to the same service, by default for `client`'s proposal and user; `user=` stands for logging in as another user.
`other` is a client of a second backend.
Every backend in the stories reads the same datasets and publishes to the same `scicat`.
`measure(n, counts, **fields)` makes run `n` appear as a raw dataset with the given counts and metadata, and returns its reference.
The metadata of such a dataset also holds its run number, as `run`; `measure(..., proposal=...)` makes the dataset belong to another proposal.
`datasets` is the fake dataset source of every backend; stories query it through `client.datasets` as in README.md.
It has a helper for tests: `datasets.add_published(entry)` lists a published entry as a derived dataset, as SciCat does, and returns its reference.
`scicat` is a fake publisher; `scicat.entries[pid]` is a published entry, with `.provenance`.
`folder` is a directory with files that hold counts, as `measure` datasets do.
`corrupt(run)` makes a dataset unreadable, with the failure message `'file signature not found'`; `repair(run)` undoes it.
`upgrade(specs=..., versions=...)` returns a client of an upgraded backend over the same datasets: the specs it offers and the software versions its records name.
`replace` is `dataclasses.replace`.

Besides the calls in README.md, the stories use this one:

```python
record.request.datasets()    # the datasets the request names directly
```

The trigger loop belongs to the sub-design for batch and automatic reduction.
The stories use two of its calls:

```python
loop.step()                  # handles what arrived since the last step; returns the records it made
loop.status(rule).reason     # why a rule submitted nothing or skipped a dataset
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
tune = client.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
for bins in (1, 2, 4):
    result = client.compute(tune, {'bins': bins}, label='tuning')

assert client.latest('tuning') == result
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

### S4. Submit a chain without waiting

Actor: user in a notebook. Goal: submit two reductions and a third request that combines them, without waiting in between.

```python
r611, r612 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0])
parts = client.submit([Request(CONTRIBUTE, {'run': run}) for run in (r611, r612)])  # pending
summed = client.submit(PARTS_SUM, {'parts': [p.refs('numerator', 'denominator') for p in parts]})

assert client.output(summed, 'numerator').values.tolist() == [3.0, 9.0]
```

Both calls return at once. The sum runs once both parts have completed.

### S5. Sum runs

Actor: user in a notebook. Goal: reduce three runs of one sample as one measurement, and subtract the sum of two background runs.

```python
runs = [measure(1, [1.0, 2.0]), measure(2, [1.0, 2.0]), measure(3, [0.0, 2.0])]
rows = [{'run': run} for run in runs]
total = client.compute(NORMALIZE, {'runs': rows, 'scale': 2.0})
backgrounds = [measure(4, [1.0, 1.0]), measure(5, [0.0, 1.0])]
result = client.compute(BACKGROUND, {'sample_runs': runs, 'background_runs': backgrounds})

assert client.output(total, 'normalized').values.tolist() == [0.5, 1.5]   # [2, 6] / 8 * 2
assert total.request.datasets() == runs
assert client.output(result, 'subtracted').values.tolist() == [1.0, 4.0]  # [2, 6] - [1, 2]
assert result.request.datasets() == runs + backgrounds
```

This is the plain request of "One sum, two ways" in the README: `NORMALIZE` takes a table of runs, and `BACKGROUND` two lists of runs.
B2 sums with an accumulator.

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

System story only; see [system-stories.md](system-stories.md).

## B. Manual and interactive reduction

### B1. Tune a SANS reduction and save the result as a template

Actor: user in a notebook. Goal: change binning and mask several times, looking at the result after each change, then save the final parameters as the beamtime's template.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
tune = client.stage(Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold')))
for bins, threshold in [(1, 0.0), (4, 0.0), (4, 1.5), (2, 1.5)]:
    client.compute(tune, {'bins': bins, 'threshold': threshold}, label='tuning')

final = client.latest('tuning')
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
new = measure(2, [2.0, 1.0, 4.0, 3.0])
requests = apply(beamtime, [new], client.datasets, member_field='run')
(reduced,) = client.compute(requests, label='iofq-beamtime').values()

assert len(client.records(label='tuning')) == 4
assert (beamtime.params['bins'], beamtime.params['threshold']) == (2, 1.5)
assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]    # [2, 0, 4, 3] in 2 groups
```

The template is plain data, kept in the notebook or in a file. That each change comes back quickly is system story S2.

### B2. Add a run to a sum, then start over without one

Actor: user in a notebook. Goal: runs 611 and 612 are summed; 613 finishes and is added; 612 turns out bad, and the sum starts over without it.

```python
r611, r612, r613 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0]), measure(613, [3.0, 1.0])
total = client.accumulator(Template(NORMALIZE, params={'scale': 2.0}, blanks=('runs',)))
for run in (r611, r612):
    total.push({'runs': {'run': run}})
first = client.output(total, 'normalized')                # runs 611 and 612

total.push({'runs': {'run': r613}})                       # 611 and 612 are not reduced again
added = client.output(total, 'normalized')                # all three

rows = [{'run': run} for run in (r611, r613)]
again = client.compute(NORMALIZE, {'runs': rows, 'scale': 2.0})

assert [value.values.tolist() for value in (first, added, client.output(again, 'normalized'))] == [
    [0.5, 1.5], [0.75, 1.25], [1.0, 1.0]]      # [3, 9] / 12 * 2; [6, 10] / 16 * 2; [4, 4] / 8 * 2
assert client.provenance(total).datasets() == [r611, r612, r613]
assert client.provenance(again).datasets() == [r611, r613]
assert client.records() == [again]
```

Reading the accumulator makes no record: `first` and `added` are copies, which the next push leaves unchanged.
The provenance of `total` is that of the plain request over its three rows.
The user starts over with the plain request over the runs to keep (README.md, "One sum, two ways").
An accumulator has no `remove` (README.md open question 2).
That adding 613 costs about one run is system story B2.

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

### B6. Find last week's result

System story only; see [system-stories.md](system-stories.md).

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
A result of another proposal, such as a direct-beam function an instrument scientist prepared for the users of later proposals, is read the same way, never through the other proposal's records (G5).

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

The stitch fits scale factors over all angles at once, so it is one request over all runs, not an accumulation.

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: adjust the vanadium processing and see the effect on a sample reduction at once.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
vanadium = client.stage(Template(VANADIUM, params={'run': vanadium_run}, blanks=('scale',)))
reduce = client.stage(Template(IOFQ, params={'run': sample}, blanks=('normalization',)))
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

The notebook is the driver: it reruns the sample after each vanadium change. Its client keeps the outputs it reads later; a label keeps no values. That the change comes back quickly is system story C5.

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
assert {r.member: r for r in client.records(label='scan')} == scan
```

Gap: `member_field`, and labels and members on records, are tentative (README.md open question 3).
Later, the result at 250 K is `client.latest('scan', member='250K')`.

### D2. Overnight cluster batch

Actor: NMX user. Goal: submit thirty long runs, close the laptop, and the next day read why some failed and rerun them.

```python
runs = {str(n): measure(n, [float(n)] * 4) for n in range(1, 31)}
corrupt(runs['7'])                                                   # the transfer was cut short
for member, run in runs.items():
    client.submit(IOFQ, {'run': run}, label='night', member=member)

morning = connect()                                                  # the next day, a new client
night = {r.member: r for r in morning.records(label='night')}
failed = [night[m] for m, s in morning.wait(night).items() if s == 'failed']
reasons = morning.failure(failed)
repair(runs['7'])                                                    # the transfer is repeated
reruns = [morning.submit(r.request.spec, r.request.params, label=r.label, member=r.member)
          for r in failed]
morning.wait(reruns)

assert [r.member for r in failed] == ['7']
assert reasons == ['file signature not found']
assert reruns[0].request == failed[0].request
assert morning.latest('night', member='7') == reruns[0]
assert morning.output(reruns[0], 'iofq').values.tolist() == [14.0, 14.0]
assert morning.status(failed) == ['failed']                          # the failure stays
assert len(morning.records(label='night')) == 31                     # and so does its record
```

Gap: labels and members on records are tentative, as in D1.
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

The service wrote `before[0]`'s output to a file when the record completed, so it can be read weeks later, until the proposal's history is dropped ([system.md](system.md), The service).

### D7. Rotation scan over a thousand angles

Actor: spectroscopy user. Goal: reduce a crystal rotation scan of a thousand runs, one run per angle, into one volume, and look at cuts through the volume while the scan continues.

```python
for n in range(1, 1001):
    measure(n, [1.0, float(n)], scan='17')                     # one run per angle
    if n == 500:
        measure(5, [1.0, 5.0], scan='17')                      # the file of run 5 arrives again

cuts, pushed = [], []
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.datasets.watch(Selector(scan='17')), 1000):
    volume.push({'runs': {'run': run}})                        # waits until the previous cut has run
    pushed.append(run)
    cuts.append(client.submit(CUT, {'data': volume.ref('counts'), 'index': 0},
                              label='cut', member='17'))
total = client.compute(COPY, {'data': volume.ref('counts')})

assert [client.output(c, 'cut').value for c in cuts] == [float(k) for k in range(1, 1001)]
assert client.output(total, 'data').values.tolist() == [1000.0, 500500.0]
provenance = client.provenance(total)
assert provenance.accumulated == (                             # the runs, in push order
    Request(VOLUME, {'runs': [{'run': run} for run in pushed]}),)
assert len(provenance.datasets()) == 1000                      # run 5, measured again, once
```

`watch` yields run 5 once, although its file arrives twice.
Each push reduces one run and adds it to the volume in place, so one volume is kept, not one per cut.
Each cut binds to the state after its push, and the next push waits until that cut has run.
`COPY` makes a record of the last state, and its provenance expands that state into the plain request over the thousand rows.
On the service, the volume is a job of its own, and each run is reduced in that job.
That a cut is ready within seconds of each run, and that history grows by a constant amount per run, is system story D7.

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

The service writes each curve to a file when its record completes, so any client of the proposal reads it later ([system.md](system.md), The service).

### E2. Automatic reduction goes quiet

Actor: instrument scientist. Goal: after an upgrade removed the template's spec version, see why nothing is reduced.

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
tune = client.stage(Template(IOFQ, params={'run': run, 'beam_centre': centre.ref('centre')},
                             blanks=('threshold',)))
result = client.compute(tune, {'threshold': 1.5})               # tuned before publishing
pid = client.publish(result.ref('iofq'), 'scicat')
plain = client.compute(IOFQ, result.request.params)             # the same request, without a stage

provenance = scicat.entries[pid].provenance                    # read without the client
assert provenance == client.provenance(result) == client.provenance(plain)
assert set(provenance.datasets()) == {centre_run, run}
assert [r.request.params for r in provenance.records()] == [centre.request.params]
assert {'essapps', 'scipp'} <= provenance.software.keys()
```

The entry carries what lasts. The history behind it is dropped once the proposal has been idle for the retention period (system story H3).

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

## G. Roles and deployment

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: run a workflow defined in a notebook without installing it; its records say what ran.

```python
draft = iofq                                                  # IOFQ's binding, being edited
dev = local(proposal='p1', datasets=datasets, bind={IOFQ: draft})   # a backend in this process
result = dev.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0])})
pid = dev.publish(result.ref('iofq'), 'scicat')               # publishing is not refused

assert dev.provenance(result).software['sans-iofq'] == 'bound in notebook'
assert scicat.entries[pid].provenance == dev.provenance(result)
```

That the service runs only installed workflows, and that an edited binding takes effect for the next request, is system story G2.

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

### H3. History ends when the work does

System story only; see [system-stories.md](system-stories.md).

## Open

What the design leaves open or defers, with the stories each item affects.

- **Removing a dataset** (system story A4): deferred, together with whether the outputs derived from it go too.
- **The service** (sections D and E): designed and not implemented; the file format of each output type and the folder layout of its files are open (scipp/essapps#23).
- **Views** (B4): the form of a read of part of an output waits for the plotting work.
- **Labels and members** (D1, D2, and every story that calls `apply`): `member_field`, and labels and members on records, are tentative; README.md open question 3.
- **Grouping** (system story D7): spreading one accumulator over several nodes needs a merge of two held states; README.md open question 1.
- **Recomputing in a record's environment** (F2): deferred.

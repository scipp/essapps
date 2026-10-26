# User stories

Each story has an actor, a goal, the client code that reaches it, and its checks as assertions.
The code uses only the API of [README.md](README.md) and the calls listed under [Conventions](#conventions).
The checks observe only what that API shows: values, records, labels, provenance, and errors.
What a story needs from the system, such as cost, placement, or persistence, is a separate story in [system-stories.md](system-stories.md).
A story that needs what the design leaves open or defers says so in a "Gap" line, and [Open](#open) lists these.

## Toy specs

Toy specs stand in for instrument workflows, so that a story's numbers can be checked by hand.
A dataset made by `measure` holds a list of counts.
Reductions take runs directly, and `CUT` and `EXPORT` read results; a separate record is made only for a result worth keeping by itself, for a value that one user types in and another computes (section I), or for a value that a workflow computes and a file also holds, which a loader workflow reads (C2).

| Spec | Params | Outputs | Computes | Stands in for |
|---|---|---|---|---|
| `IOFQ` | `reduction: Reduction` (`bins=2`, `threshold=0.0`), `sample: Sample`, `beam_centre: BeamCentre \| None`, `normalization: ComputedNormalization \| None` (no default) | `iofq`, `masked` | the counts with values below `threshold` set to 0 (`masked`); minus `beam_centre.position`; divided by the normalization's `factor`; summed into `bins` equal groups (`iofq`) | a SANS reduction without a can, of one run |
| `IOFQ_CAN` | the params of `IOFQ`, and `can: Can` | `iofq`, `masked` | as `IOFQ`, of the counts minus the can's counts | a SANS reduction of one run with the can subtracted |
| `BEAM_CENTRE` | `direct_beam: DirectBeam` | `centre: BeamCentre` | `BeamCentre(position=...)`, with the mean of the counts | beam-centre finding |
| `VANADIUM` | `scaling: Scaling` (`scale=1.0`), `vanadium: Vanadium` | `normalization: ComputedNormalization` | `ComputedNormalization(factor=...)`, with the sum of the counts times `scale` | vanadium processing |
| `LOAD_NORMALIZATION` | `published: PublishedNormalization` | `normalization: ComputedNormalization` | `ComputedNormalization(factor=...)`, with the factor read from the file | reading a result published to a catalogue |
| `NORMALIZE` | `scaling: Scaling`, `runs: list[Sample]`, a table | `normalized` | the counts summed over the runs, divided by their total, times `scale` | a sum of runs, such as a SANS I(Q) |
| `BACKGROUND` | `sample_runs: list[Sample]`, `background_runs: list[Background]`, a table each | `subtracted` | summed sample counts minus summed background counts | a sum of two sets of runs |
| `CONTRIBUTE` | `sample: Sample` | `parts: Parts` (`numerator`, `denominator`), `transmission: Transmission` | a copy of the counts and their total; `Transmission(fraction=...)`, with the first count divided by the total | a reduction of one run whose outputs another request sums |
| `PARTS_SUM` | `parts: list[Parts]` | `numerator`, `denominator` | each field of `Parts` summed over the list | a sum of outputs of other records |
| `ANGLE` | `sample: Sample` | `volume: Volume` | `Volume(counts=...)`, with a copy of the counts | a reduction of one run into a volume |
| `CUT` | `position: Position` (`index`), `volume: Volume` | `cut` | the value of `volume.counts` at `index` | a cut through a volume, from another package |
| `VOLUME` | `runs: list[Sample]`, a table | `volume: Volume` | `Volume(counts=...)`, with the counts summed over the runs | a rotation scan reduced into one volume, one run per angle |
| `STITCH` | `runs: list[Sample]`, a table; `reference: Reference` | `stitched: Reflectivity` | `Reflectivity(curve=...)`, with each run's counts divided by the reference's counts, times the factor that makes its first value equal the last value of the curve before it, with the first curve not scaled; these curves concatenated | a reflectometry reduction that stitches angles with scale factors fitted over all of them |
| `EXPORT` | `result: Reflectivity` | `text: str` | the values of `result.curve`, separated by commas | writing a file for another program |
| `IOFQ_V2` | `reduction: ReductionV2` (`bins=4`, `mask_below=0.0`), the others as `IOFQ` | `iofq`, `masked` | as `IOFQ` | version 2 of `IOFQ`: same name, `sans-iofq`, a renamed field of `reduction` and a new default |
| `SLICER` | `sample: Sample` | `slices: list[TimeRange]` | a `TimeRange` (`start`, `stop`) for every two bins of the run's log | a first reduction that finds time slices in a log |
| `SLICES` | `sample: Sample`, `time_range: list[TimeRange]`, a table | `sliced` | the sample's counts in each time range, summed | a reduction of one run sliced in time |
| `FIND_CENTRE` | `direct_beam: DirectBeam` | `centre: Centre` | `Centre(x=...)`, with the mean of the counts | beam-centre finding, with the centre as a group |
| `CENTRED_MULTI` | `centre: Centre`; `sample: list[SampleWithTransmission]`, a table; `can: list[Can]`, a table | `centred` | each sample run's counts times its transmission, summed; minus the cans' counts, summed; minus `centre.x` | a SANS reduction of several runs, with a beam centre typed in or computed and a transmission per run |

The type at a place, the value of a parameter, an element of a list, or a field of a group at any depth, decides how a request gives its value (README.md, Specs and bindings, which also has the row for a union of groups):

| The type at a place | In the toy specs | Given as |
|---|---|---|
| a file: `Path` | the `file` of each run, such as `Sample.file` | a dataset reference |
| a group, no field of which is a data value | the runs, such as `Sample` and `SampleWithTransmission`; `Reduction`, `Scaling`, `Position`, `TimeRange`, `Centre`, `PublishedNormalization` | an object of its fields, each a place, or a reference to an output that fits and holds no file |
| a group with a field that is a data value | `BeamCentre`, `ComputedNormalization`, `Transmission`, `Parts`, `Volume`, `Reflectivity` | a reference to an output that fits |
| a list, `list[X]` | each list parameter, such as `runs` and `parts` | a list, each element a place of type `X` |
| a scalar: `int`, `float`, `str`, `bool`, an enum, or a `Literal` | `int`, `float`, as in `bins` and `scale` | an inline value |
| a type without a JSON form, a *data value*, such as `sc.Variable` | the fields of the groups above, such as `Volume.counts` | nothing: it is a field of a group |

Each parameter is a group, a union of groups, or a list of one; a file or a number is a field of a group.
Each run is a group of its file, `file: Path`, with a class for each role: `Sample`, `Can`, `DirectBeam`, `Vanadium`, `Background`, and `Reference`, each a subclass of `Run`:

```python
class Run(BaseModel):
    file: Path

class Sample(Run): ...
class Can(Run): ...

{'sample': {'file': run}, 'can': {'file': can}}   # IOFQ_CAN
```

A request gives each run as an object with a dataset reference at `file`, and a table of runs as a list of them, such as `[{'file': r} for r in runs]`.
An output without a type in the list of toy specs is a `sc.Variable`.
`BeamCentre`, `Transmission`, `Volume`, and `Reflectivity` are groups of one `sc.Variable` each, named by its role: `position`, `fraction`, `counts`, and `curve`.
`ComputedNormalization` holds its `factor`, a `sc.Variable`, and `PublishedNormalization` a file of it, `file: Path`.
`Parts` is a group of two `sc.Variable`s.
`SampleWithTransmission` is a `Sample` with `transmission: Transmission`, without a default: each row gives the transmission of its run.
A reference to an output fits a place when the output's type fits the type at the place (README.md): `BEAM_CENTRE`'s `centre` fits `IOFQ`'s `beam_centre`, `VANADIUM`'s and `LOAD_NORMALIZATION`'s `normalization` fit `IOFQ`'s `normalization`, `FIND_CENTRE`'s `centre` fits `CENTRED_MULTI`'s `centre`, `CONTRIBUTE`'s `transmission` fits the field `transmission` of a `SampleWithTransmission`, `ANGLE`'s and `VOLUME`'s `volume` fit `CUT`'s `volume`, and `STITCH`'s `stitched` fits `EXPORT`'s `result`.
`record.ref(output, key)` names one element of a list output, by its index as a string: `slicer.ref('slices', key='0')` is a `TimeRange`, and fits a row of `time_range`.
The list parameters of `NORMALIZE`, `BACKGROUND`, `VOLUME`, and `STITCH` are tables, each with a run per row; that of `SLICES` has a time range per row.
These specs declare their tables with `tables=`, and each table needs a row, with `Field(min_length=1)`, as in `runs: list[Sample] = Field(min_length=1)`.
`CENTRED_MULTI` has the params `centre: Centre`, `sample: list[SampleWithTransmission] = Field(min_length=1)`, and `can: list[Can] = Field(min_length=1)`, with `tables=('sample', 'can')`.
So both `sample` and `can` are tables that need a row.
`PARTS_SUM` declares no table, so its `parts` is a value like any other, given whole in a request.

`IOFQ` and `VANADIUM`, which the stories stage, are bound to the functions `iofq` and `vanadium` by a toy binding that serves plain requests and stages.
`NORMALIZE`, `BACKGROUND`, and `VOLUME` are bound with `DriverBinding` to `Summing`, a toy driver: it sums the counts of each table's runs, and computes the outputs from the sums, so a push into an accumulator adds to the sums instead of keeping the rows.
`STITCH`, `SLICES`, and `CENTRED_MULTI` are bound with `DriverBinding` to a driver that keeps the rows and computes the outputs from all of them at each read.
`PARTS_SUM` is bound with `function_binding` to a function that sums each field of `Parts` over the list `parts`.
The other toy specs are bound with `function_binding`.
No toy spec returns an output that shares memory with a value it takes.
A story may add a toy spec to the list of toy specs; it must take runs directly, or be a loader of a file, and be checkable by hand.

## Conventions

Fixtures: `client` is a client for proposal `p1`.
In the notebook stories it is `local(proposal='p1', datasets=datasets, bind=...)`, a backend in the user's process ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).
In the batch and automatic stories, sections D and E, it is `connect(url, proposal='p1')`, a client of the service at `url` ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)), which is designed and not implemented.
The story tests make a `Client` of one in-process `Backend` for both, with a fake store in memory.
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
`upgrade(specs=..., versions=...)` returns a client of an upgraded backend over the same datasets, history, and store: the specs it offers, every one by default, and the software versions its records name. The upgrade ends every client of the backend before it, and `connect` then makes clients of the upgraded backend.
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
result = client.compute(IOFQ, params={'reduction': Reduction(threshold=1.5),
                                  'sample': {'file': run}, 'normalization': None})

assert client.output(result, 'iofq').values.tolist() == [2.0, 7.0]   # [0, 2, 3, 4] in 2 groups
assert result.request.params['reduction']['bins'] == 2                # default, recorded
assert result.request.datasets() == [run]
```

### S2. Tune one parameter

Actor: user in a notebook. Goal: change the binning several times, looking at the result after each change.

```python
run = measure(1, counts=[1.0, 2.0, 3.0, 4.0])
tune = client.stage(Template(IOFQ, params={'sample': {'file': run}, 'normalization': None},
                           blanks=('reduction',)))
for bins in (1, 2, 4):
    result = client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='tuning')

assert client.latest('tuning') == result
assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0, 3.0, 4.0]
plain = client.compute(IOFQ, params=result.request.params)   # the record alone reproduces it
assert sc.identical(client.output(plain, 'iofq'), client.output(result, 'iofq'))
```

That the run is not loaded again for each binning is system story S2.

### S3. Look at a value inside a reduction

Actor: user in a notebook. Goal: see an intermediate value, the counts after masking, without a separate workflow.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
result = client.compute(IOFQ, params={'reduction': Reduction(threshold=2.5),
                                  'sample': {'file': run}, 'normalization': None})

assert client.output(result, 'masked').values.tolist() == [0.0, 0.0, 3.0, 4.0]
assert client.output(result, 'iofq').values.tolist() == [0.0, 7.0]
```

The author must have declared `masked` as an output. Every record of `IOFQ` then has it.

### S4. Submit a chain without waiting

Actor: user in a notebook. Goal: submit two reductions and a third request that combines them, without waiting in between.

```python
r611, r612 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0])
parts = client.submit([Request(CONTRIBUTE, params={'sample': {'file': run}}) for run in (r611, r612)])  # pending
summed = client.submit(PARTS_SUM, params={'parts': [p.ref('parts') for p in parts]})

assert client.output(summed, 'numerator').values.tolist() == [3.0, 9.0]
```

Both calls return at once. The sum runs once both parts have completed.

### S5. Sum runs

Actor: user in a notebook. Goal: reduce three runs of one sample as one measurement, and subtract the sum of two background runs.

```python
runs = [measure(1, [1.0, 2.0]), measure(2, [1.0, 2.0]), measure(3, [0.0, 2.0])]
total = client.compute(NORMALIZE, params={'scaling': Scaling(scale=2.0),
                                          'runs': [{'file': r} for r in runs]})
backgrounds = [measure(4, [1.0, 1.0]), measure(5, [0.0, 1.0])]
result = client.compute(BACKGROUND, params={'sample_runs': [{'file': r} for r in runs],
                                            'background_runs': [{'file': r} for r in backgrounds]})

assert client.output(total, 'normalized').values.tolist() == [0.5, 1.5]   # [2, 6] / 8 * 2
assert total.request.datasets() == runs
assert client.output(result, 'subtracted').values.tolist() == [1.0, 4.0]  # [2, 6] - [1, 2]
assert result.request.datasets() == runs + backgrounds
```

This is a plain request over tables (README.md, "Combining runs: tables"): `NORMALIZE` takes one table of runs, and `BACKGROUND` two, as `IOFQ_MULTI` does. B2 sums with an accumulator.

### S7. Reduce each sample with the can measured before it

Actor: instrument scientist. Goal: every sample subtracts the can measured most recently before it, without naming the can.

```python
can_1 = measure(1, [1.0, 1.0], role='can')
first = measure(2, [5.0, 6.0], role='sample')
can_3 = measure(3, [2.0, 2.0], role='can')
second = measure(4, [6.0, 9.0], role='sample')
template = Template(IOFQ_CAN, params={'normalization': None}, blanks=('sample', 'can'))
cans = Lookup({('can', 'file'): LastBefore(Selector(role='can'))})   # the latest can measured before the run

requests = apply(template, [first, second], client.datasets, at=('sample', 'file'), member_field='run',
                 lookup=cans)
reduced = list(client.compute(requests, label='iofq').values())

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[4.0, 5.0], [4.0, 7.0]]
assert [r.request.datasets() for r in reduced] == [[first, can_1], [second, can_3]]
```

Each dataset fills the file of the one blank the lookup leaves, at `('sample', 'file')`.

### S8. Trace a result to raw data

Actor: user. Goal: learn which raw runs and parameter values a two-step result came from.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
vanadium = client.compute(VANADIUM, params={'scaling': Scaling(scale=2.0), 'vanadium': {'file': vanadium_run}})
result = client.compute(IOFQ, params={'sample': {'file': sample}, 'normalization': vanadium.ref('normalization')})

provenance = client.provenance(result)
assert set(provenance.datasets()) == {sample, vanadium_run}
(upstream,) = provenance.records()             # the records `result` reads, through all its references
assert upstream.request.params['scaling'] == {'scale': 2.0}
assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]   # [4, 8] / ((1 + 1) * 2)
```

## A. Getting data in

### A1. Browse a local folder next to a catalogue reference

Actor: user of a local application. Goal: list the files in a folder, add a reference measurement by PID, and plot all of them.

```python
files = sorted(folder.glob('*.h5'))                      # two files, counts [1, 2] and [3, 4]
vanadium = measure(9, [5.0, 6.0], pid='20.500.12269/vanadium')
named = [dataset(path=f) for f in files] + [dataset(pid='20.500.12269/vanadium')]
plots = [client.compute(IOFQ, params={'sample': {'file': d}, 'normalization': None}) for d in named]

assert [client.output(p, 'iofq').values.tolist() for p in plots] == [
    [1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
assert plots[2].request.datasets() == [vanadium]
```

Listing the folder is plain Python and makes no record. What is fetched, and when, is system story A1.

### A2. Run number instead of file

Actor: user at the instrument. Goal: type a run number and get the dataset.

```python
run = measure(4711, [1.0, 2.0])
result = client.compute(IOFQ, params={'sample': {'file': dataset(run=4711)}, 'normalization': None})

assert result.request.datasets() == [run]              # the dataset itself, not the number
with pytest.raises(SubmitError):                        # no run 4712 exists
    client.compute(IOFQ, params={'sample': {'file': dataset(run=4712)}, 'normalization': None})
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
tune = client.stage(Template(IOFQ, params={'sample': {'file': run}, 'normalization': None},
                           blanks=('reduction',)))
for bins, threshold in [(1, 0.0), (4, 0.0), (4, 1.5), (2, 1.5)]:
    reduction = Reduction(bins=bins, threshold=threshold)
    client.compute(tune, params={'reduction': reduction}, label='tuning')

final = client.latest('tuning')
beamtime = Template(final.request.spec, params=final.request.params, blanks=('sample',))
new = measure(2, [2.0, 1.0, 4.0, 3.0])
requests = apply(beamtime, [new], client.datasets, at=('sample', 'file'), member_field='run')
(reduced,) = client.compute(requests, label='iofq-beamtime').values()

assert len(client.records(label='tuning')) == 4
assert beamtime.params['reduction'] == {'bins': 2, 'threshold': 1.5}
assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]    # [2, 0, 4, 3] in 2 groups
```

The template is plain data, kept in the notebook or in a file. That each change comes back quickly is system story S2.

### B2. Add a run to a sum, then start over without one

Actor: user in a notebook. Goal: runs 611 and 612 are summed; 613 finishes and is added; 612 turns out bad, and the sum starts over without it.

```python
r611, r612, r613 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0]), measure(613, [3.0, 1.0])
total = client.accumulator(Template(NORMALIZE, params={'scaling': Scaling(scale=2.0)}, blanks=('runs',)))
for run in (r611, r612):
    total.push(runs=[{'file': run}])
first = client.output(total, 'normalized')                # runs 611 and 612

total.push(runs=[{'file': r613}])                         # 611 and 612 are not reduced again
added = client.output(total, 'normalized')                # all three

again = client.compute(NORMALIZE, params={'scaling': Scaling(scale=2.0),
                                          'runs': [{'file': r611}, {'file': r613}]})

assert [value.values.tolist() for value in (first, added, client.output(again, 'normalized'))] == [
    [0.5, 1.5], [0.75, 1.25], [1.0, 1.0]]      # [3, 9] / 12 * 2; [6, 10] / 16 * 2; [4, 4] / 8 * 2
assert client.provenance(total).datasets() == [r611, r612, r613]
assert client.provenance(again).datasets() == [r611, r613]
assert client.records() == [again]
```

Reading the accumulator makes no record: `first` and `added` are copies, which the next push leaves unchanged.
The provenance of `total` is that of the plain request over its three rows.
An accumulator has no `remove` (README.md open question 2), so the user starts over with the plain request over the runs to keep (README.md, "Stages and accumulators").
That adding 613 costs about one run is system story B2.

### B4. Explore a 4D volume

Actor: spectroscopy user in the web UI. Goal: drag through cuts of a Q-E volume, then fit the cut that looks right.

```python
run = measure(1, [1.0, 2.0, 3.0, 4.0])
angle = client.compute(ANGLE, params={'sample': {'file': run}})              # stands in for a Q-E volume
cuts = [client.output(angle, 'volume', select={'q': i}) for i in range(4)]    # dragging a slider
fit = client.compute(CUT, params={'position': Position(index=2), 'volume': angle.ref('volume')})

assert [c.counts.value for c in cuts] == [1.0, 2.0, 3.0, 4.0]
assert client.output(fit, 'cut').value == 3.0
assert client.records() == [angle, fit]                      # the selections made no record
```

A selection of a group output slices each array field that has the dimension, here `counts`, and copies the other fields whole; so the request of `CUT` refers to the same output that the slider selects from.
The chosen cut is a parameter of the next request. How fast a selection comes back is system story B4.
Gap: a selection only indexes; a look that computes, such as a projection, waits for the plotting work (README.md open question 4).

### B6. Find last week's result

System story only; see [system-stories.md](system-stories.md).

## C. Chaining

### C1. Beam centre feeds a batch of sample reductions

Actor: user. Goal: use the beam centre as a parameter of every sample reduction; provenance reaches the beam-centre run.

```python
centre_run = measure(1, [1.0, 1.0, 1.0, 1.0])
centre = client.compute(BEAM_CENTRE, params={'direct_beam': {'file': centre_run}}, label='beam-centre')
samples = [measure(2, [2.0, 3.0, 4.0, 5.0], role='sample'),
           measure(3, [5.0, 4.0, 3.0, 2.0], role='sample')]
template = Template(IOFQ, params={'beam_centre': centre.ref('centre'), 'normalization': None},
                    blanks=('sample',))

requests = apply(template, samples, client.datasets, at=('sample', 'file'), member_field='run')
reduced = list(client.compute(requests, label='iofq').values())

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [[3.0, 7.0], [7.0, 3.0]]
assert set(client.provenance(reduced[0]).datasets()) == {samples[0], centre_run}
```

A beam centre found again later is a new record under `beam-centre`; the template keeps the reference it was made with until someone changes it.

### C2. Vanadium from the catalogue

Actor: user. Goal: use a vanadium result that another backend published to SciCat.

```python
vanadium = other.compute(VANADIUM, params={'scaling': Scaling(scale=2.0),
                                           'vanadium': {'file': measure(1, [1.0, 1.0])}})
pid = other.publish(vanadium.ref('normalization'), 'scicat')
published = datasets.add_published(scicat.entries[pid])

sample = measure(2, [4.0, 8.0])
loaded = client.compute(LOAD_NORMALIZATION, params={'published': {'file': dataset(pid=pid)}})
result = client.compute(IOFQ, params={'sample': {'file': sample},
                                      'normalization': loaded.ref('normalization')})

assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]    # [4, 8] / ((1 + 1) * 2)
assert set(client.provenance(result).datasets()) == {sample, published}
assert client.records() == [loaded, result]      # the vanadium record is on the other backend
```

Provenance stops at the published dataset, since what lies behind a dataset belongs to its source. How the backend reads the published output is system story C2.
The published normalization is a file of its factor, and a dataset reference fits a file only. So `LOAD_NORMALIZATION` reads the file and outputs a `ComputedNormalization`, which `IOFQ`'s `normalization` takes by reference, as it takes the output of `VANADIUM`. `normalization` is a group, not a union of the two ways, since no reference fits a union (README.md, Specs and bindings).
A result of another proposal, such as a direct-beam function an instrument scientist prepared for the users of later proposals, is read the same way, never through the other proposal's records (G5).

### C4. Reflectometry angle series

Actor: reflectometry user. Goal: reduce four angles against a reference, stitch them, and export the stitched curve to a file.

```python
reference = measure(10, [2.0, 2.0], role='reference')
angles = [measure(11, [8.0, 4.0], angle=0.5), measure(12, [2.0, 1.0], angle=1.0),
          measure(13, [4.0, 2.0], angle=2.0), measure(14, [2.0, 0.5], angle=4.0)]
series = client.submit(STITCH, params={'runs': [{'file': a} for a in angles],
                                       'reference': {'file': reference}})
exported = client.compute(EXPORT, params={'result': series.ref('stitched')})

assert client.output(exported, 'text') == '4.0,2.0,2.0,1.0,1.0,0.5,0.5,0.125'
assert set(client.provenance(exported).datasets()) == {reference, *angles}
```

The stitch fits scale factors over all angles at once, so it cannot add one angle at a time. An accumulator over `STITCH` still gives what the plain request gives at each read: its driver keeps the rows and stitches them again at each read (README.md, "What a binding provides").

### C5. Vanadium and sample tuned together

Actor: instrument scientist in a notebook. Goal: adjust the vanadium processing and see the effect on a sample reduction at once.

```python
vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
vanadium = client.stage(Template(VANADIUM, params={'vanadium': {'file': vanadium_run}}, blanks=('scaling',)))
reduce = client.stage(Template(IOFQ, params={'sample': {'file': sample}}, blanks=('normalization',)))
reduced = []
for scale in (1.0, 2.0):
    processed = client.compute(vanadium, params={'scaling': Scaling(scale=scale)}, label='vanadium')
    normalization = processed.ref('normalization')
    reduced.append(client.compute(reduce, params={'normalization': normalization}, label='iofq'))

assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
    [2.0, 4.0], [1.0, 2.0]]                      # [4, 8] divided by 2, then by 4
assert client.records(label='iofq') == reduced
normalization = reduced[-1].request.params['normalization']
assert normalization == client.latest('vanadium').ref('normalization')
```

The notebook is the controller: it reruns the sample after each vanadium change. Its client keeps the outputs it reads later; a label keeps no values. That the change comes back quickly is system story C5.

## D. Batch

### D1. Temperature scan

Actor: instrument scientist. Goal: reduce a scan with one template, one temperature per run; plot three chosen temperatures; one corrupt file affects nothing else.

```python
temperatures = ['250K', '260K', '270K', '280K', '290K']
runs = [measure(n, [float(n)] * 4, temperature=t) for n, t in enumerate(temperatures, start=1)]
corrupt(runs[1])                                                     # 260K
template = Template(IOFQ, params={'reduction': Reduction(bins=1), 'normalization': None}, blanks=('sample',))
requests = apply(template, runs, client.datasets, at=('sample', 'file'), member_field='temperature')
scan = client.compute(requests, label='scan', persist=True)         # keyed by temperature

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
    client.submit(IOFQ, params={'sample': {'file': run}, 'normalization': None}, label='night', member=member, persist=True)
client.close()                                                       # the laptop is closed

morning = connect()                                                  # the next day, a new client
night = {r.member: r for r in morning.records(label='night')}
failed = [night[m] for m, s in morning.wait(night).items() if s == 'failed']
reasons = morning.failure(failed)
repair(runs['7'])                                                    # the transfer is repeated
reruns = [morning.submit(r.request.spec, params=r.request.params, label=r.label, member=r.member,
                         persist=True) for r in failed]
morning.wait(reruns)

assert [r.member for r in failed] == ['7']
assert reasons == ['file signature not found']
assert reruns[0].request == failed[0].request
assert morning.latest('night', member='7') == reruns[0]
assert morning.output(night['1'], 'iofq').values.tolist() == [2.0, 2.0]     # written in the night
assert morning.output(reruns[0], 'iofq').values.tolist() == [14.0, 14.0]
assert morning.status(failed) == ['failed']                          # the failure stays
assert len(morning.records(label='night')) == 31                     # and so does its record
```

The night's client keeps nothing: each record's outputs are written to the store before it completes, and the morning client reads them there.

Gap: labels and members on records are tentative, as in D1.
That the runs continue while no client is connected is system story D2.

### D3. Cancel and resubmit

Actor: user of the shared service. Goal: cancel a running batch of 500 with a wrong shared parameter, fix the parameter, and resubmit without waiting for the cancel.

```python
runs = [measure(n, [1.0, 1.0, 1.0, 1.0]) for n in range(1, 501)]
wrong = Template(IOFQ, params={'reduction': Reduction(threshold=20.0), 'normalization': None},
                blanks=('sample',))
first = client.submit(apply(wrong, runs, client.datasets, at=('sample', 'file'), member_field='run'),
                      label='scan', persist=True)
client.cancel(first)
fixed = replace(wrong, params={'reduction': Reduction(threshold=0.5), 'normalization': None})
second = client.compute(apply(fixed, runs, client.datasets, at=('sample', 'file'), member_field='run'),
                        label='scan', persist=True)

assert set(client.wait(first).values()) <= {'completed', 'cancelled'}
assert [client.output(r, 'iofq').values.tolist() for r in second.values()] == 500 * [[2.0, 2.0]]
assert len(client.records(label='scan')) == 1000
```

That the cancel stops the started requests at once is system story D3.

### D4. Typo caught before 500 failures

Actor: user filling in a batch form. Goal: a parameter the params model rejects is caught before any record exists.

```python
runs = [measure(n, [1.0, 1.0]) for n in range(1, 501)]
typo = Template(IOFQ, params={'reduction': {'threshold': '2,5'}, 'normalization': None}, blanks=('sample',))
requests = apply(typo, runs, client.datasets, at=('sample', 'file'), member_field='run')

with pytest.raises(SubmitError, match=r'reduction\.threshold'):
    client.submit(requests, label='scan')
assert client.records() == []
```

### D6. Rerun a batch with a new workflow version

Actor: instrument scientist. Goal: reduce an earlier batch of the proposal again with a new spec version, keeping the first results.

```python
template = Template(IOFQ, params={'reduction': Reduction(threshold=1.5), 'normalization': None},
                    blanks=('sample',))
runs = [measure(n, [1.0, 2.0, 3.0, 4.0]) for n in (1, 2, 3)]
client.compute(apply(template, runs, client.datasets, at=('sample', 'file'), member_field='run'),
               label='scan', persist=True)

# weeks later, version 2 of the spec is released
before = client.records(label='scan')
runs = [r.request.params['sample']['file'] for r in before]
renamed = apply(replace(template, spec=IOFQ_V2), runs, client.datasets, at=('sample', 'file'),
                member_field='run')
with pytest.raises(SubmitError, match=r'reduction\.threshold: Extra inputs'):
    client.submit(renamed, label='scan')
moved = replace(template, spec=IOFQ_V2, params={'reduction': ReductionV2(mask_below=1.5), 'normalization': None})
requests = apply(moved, runs, client.datasets, at=('sample', 'file'), member_field='run')
after = list(client.compute(requests, label='scan', persist=True).values())

assert [client.output(r, 'iofq').values.tolist() for r in (before[0], after[0])] == [
    [2.0, 7.0], [0.0, 2.0, 3.0, 4.0]]
assert [r.request.params['reduction']['bins'] for r in (before[0], after[0])] == [2, 4]   # defaults
assert len(client.records(label='scan')) == 6
```

The first records keep the default of version 1, so the change of default shows in the records.

`before[0]` was persisted at submission, so its output can be read weeks later, until the proposal's history is dropped (README.md, [How long records and values are kept](README.md#how-long-records-and-values-are-kept)).

### D7. Rotation scan over three hundred angles

Actor: spectroscopy user. Goal: reduce a crystal rotation scan of 300 runs, one run per angle, into one volume, and look at cuts through the volume while the scan continues.

```python
for n in range(1, 301):
    measure(n, [1.0, float(n)], scan='17')                     # one run per angle
    if n == 150:
        measure(5, [1.0, 5.0], scan='17')                      # the file of run 5 arrives again

cuts, pushed = [], []
scan = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.datasets.watch(Selector(scan='17')), 300):
    scan.push(runs=[{'file': run}])                            # added once the previous cut is copied
    pushed.append({'file': run})
    cuts.append(client.output(scan, 'volume', select={'q': 0}))     # a copy of the cut; no record
total = client.freeze(scan, persist=True)                      # the last state, written once

assert [c.counts.value for c in cuts] == [float(k) for k in range(1, 301)]
assert client.output(total, 'volume').counts.values.tolist() == [300.0, 45150.0]
assert total.request == Request(VOLUME, params={'runs': pushed})   # push order
assert client.records() == [total]                             # the cuts made no record
assert len(client.provenance(total).datasets()) == 300         # run 5, measured again, once
```

`watch` yields run 5 once, although its file arrives twice.
A second push of run 5 would be refused, since the rows of a table are distinct.
Each push reduces one run and adds it to the volume in place, so one volume is kept, not one per cut.
Each cut pins the state after its push and copies only the selected cut, and the next push is added once the copy is made.
`freeze` turns the last state into a record without a copy: its value is the volume itself, written once to the store.
Where the service holds the volume is open (README.md open question 5).
That a cut is ready within seconds of each run, and how history grows with the runs, is system story D7.

## E. Automatic reduction

### E1. Series grows, reduction follows

Actor: reflectometry user during a beamtime. Goal: after each angle, the stitched curve grows by one angle; one curve per sample, not one per arrival.

```python
reference = measure(1, [1.0, 1.0], role='reference')
template = Template(STITCH, params={'reference': {'file': reference}}, blanks=('runs',))
rule = Rule('reflectivity', template, selector=Selector(role='sample'), series='sample',
            at=('runs', 'file'), label='reflectivity')
loop = TriggerLoop(client, rules=[rule])

r2 = measure(2, [1.0, 2.0], role='sample', sample='si')
loop.step()
r4 = measure(4, [4.0, 8.0], role='sample', sample='si')
r3 = measure(3, [1.0, 1.0], role='sample', sample='si')
measure(2, [1.0, 2.0], role='sample', sample='si')             # arrives again
loop.step()

user = connect()                                              # any client of the proposal
latest = user.latest('reflectivity', member='si')
assert len(user.records(label='reflectivity')) == 2           # one per step, not one per arrival
assert latest.request.params['runs'] == [{'file': r} for r in (r2, r3, r4)]   # run order, not arrival order
assert user.output(latest, 'stitched').curve.values.tolist() == [1.0, 2.0, 2.0, 2.0, 2.0, 4.0]
```

Each record is a plain request that stitches every angle so far, one row per angle. An accumulator over `STITCH` would give the same curves (C4); whether a rule pushes into an accumulator is open ([automatic-reduction.md](automatic-reduction.md)).

The trigger loop persists what it submits, every output by default, so any client of the proposal reads each curve (README.md, [How long records and values are kept](README.md#how-long-records-and-values-are-kept)).

### E2. Automatic reduction goes quiet

Actor: instrument scientist. Goal: after an upgrade removed the template's spec version, see why nothing is reduced.

```python
rule = Rule('auto-iofq', Template(IOFQ, params={'normalization': None}, blanks=('sample',)), selector=Selector(role='sample'),
            at=('sample', 'file'), label='iofq')
upgraded = upgrade(specs=[IOFQ_V2])                            # version 1 is gone
loop = TriggerLoop(upgraded, rules=[rule])
measure(1, [1.0, 1.0], role='sample')

assert loop.step() == []
assert loop.status(rule).reason == '1: unknown spec sans-iofq/v1'   # member 1
```

### E3. Reduction of our own output

Actor: none; a failure mode. Goal: a published result that SciCat lists as a dataset does not trigger the rule.

```python
rule = Rule('auto-iofq', Template(IOFQ, params={'normalization': None}, blanks=('sample',)), selector=Selector(),   # every raw dataset
            at=('sample', 'file'), label='iofq')
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
rule = Rule('auto-iofq', Template(IOFQ, params={'normalization': None}, blanks=('sample',)), selector=Selector(role='sample'),
            at=('sample', 'file'), label='iofq')
first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
(before,) = TriggerLoop(client, rules=[rule]).step()

improved = replace(rule, template=replace(rule.template,
                                 params={'reduction': Reduction(threshold=1.5), 'normalization': None}))
second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
(after,) = TriggerLoop(client, rules=[improved]).step()

user = connect()                                                # any client of the proposal
assert after.request.datasets() == [second]                     # run 1 is not reduced again
assert [user.output(r, 'iofq').values.tolist() for r in (before, after)] == [
    [3.0, 7.0], [2.0, 7.0]]
stale = [r for r in user.records(label='iofq') if r.request.params['reduction']['threshold'] != 1.5]
assert [r.request.datasets() for r in stale] == [[first]]       # to reprocess, if the user wants
```

The second loop stands in for a running trigger loop whose rule is replaced. It learns that run 1 is handled from the records under `iofq`.

## F. Publication and provenance

### F1. Publish, then trace six months later

Actor: user, then a colleague. Goal: the SciCat entry alone answers what raw files, parameters, and software produced it.

```python
centre_run, run = measure(1, [1.0, 1.0, 1.0, 1.0]), measure(2, [2.0, 3.0, 4.0, 5.0])
centre = client.compute(BEAM_CENTRE, params={'direct_beam': {'file': centre_run}})
tune = client.stage(Template(IOFQ, params={'sample': {'file': run}, 'beam_centre': centre.ref('centre')},
                             blanks=('reduction',)))
result = client.compute(tune, params={'reduction': Reduction(threshold=1.5)})   # tuned before publishing
pid = client.publish(result.ref('iofq'), 'scicat')
plain = client.compute(IOFQ, params=result.request.params)   # without a stage

provenance = scicat.entries[pid].provenance                    # read without the client
assert provenance == client.provenance(result) == client.provenance(plain)
assert set(provenance.datasets()) == {centre_run, run}
assert [r.request.params for r in provenance.records()] == [centre.request.params]
assert {'essdispatch', 'scipp'} <= provenance.software.keys()
```

The entry carries what lasts. The history behind it is dropped once the proposal has been idle for the retention period (system story H3).

### F2. Reproduce after two upgrades

Actor: user. Goal: learn before anything runs that the exact result is not reproducible in the current environment; a rerun is an honestly labelled new record.

```python
result = client.compute(IOFQ, params={'reduction': Reduction(threshold=1.5),
                                      'sample': {'file': measure(1, [1.0, 2.0, 3.0, 4.0])}})
upgraded = upgrade(versions={'scipp': '99.0'})                 # ends client

with pytest.raises(SubmitError, match='scipp'):
    upgraded.recompute(result)                                 # in the record's environment
again = upgraded.compute(result.request.spec, params=result.request.params)   # in the current one

assert again.request == result.request
assert upgraded.provenance(again).software['scipp'] == '99.0'
assert upgraded.provenance(result).software['scipp'] != '99.0'
```

Gap: `client.recompute(record)`, which runs a request in its record's environment or refuses before running, is deferred.
That the recorded environment can be installed again is system story F2.

## G. Roles and deployment

### G2. Developer iterates on a workflow

Actor: workflow developer. Goal: run a workflow defined in a notebook without installing it.

```python
draft = iofq                                                  # IOFQ's function, being edited
dev = local(proposal='p1', datasets=datasets, bind=[function_binding(IOFQ, draft)])   # a backend in this process
result = dev.compute(IOFQ, params={'sample': {'file': measure(1, [1.0, 2.0, 3.0, 4.0])}})

assert dev.output(result, 'iofq').values.tolist() == [3.0, 7.0]
```

That the service runs only installed workflows, and that an edited binding takes effect for the next request, is system story G2.

### G5. Reference across proposals refused

Actor: external user. Goal: a reference to another proposal's record is refused before any record exists.

```python
theirs, mine = connect(proposal='p1', user='anna'), connect(proposal='p2', user='eve')
centre = theirs.compute(BEAM_CENTRE, params={'direct_beam': {'file': measure(1, [1.0, 1.0], proposal='p1')}})
sample = measure(2, [2.0, 2.0], proposal='p2')

with pytest.raises(SubmitError, match='p1'):
    mine.compute(IOFQ, params={'sample': {'file': sample}, 'beam_centre': centre.ref('centre')})
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

## I. Values typed in or computed

One user types a value in and another computes it; the request gives each value, or each row, inline or by reference.

### I1. Time slices found by a first reduction

Actor: user in a notebook. Goal: sum a run over the time slices that a first reduction found in its log; a slice given again is not summed twice.

```python
run = measure(611, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
slicer = client.compute(SLICER, params={'sample': {'file': run}})
n = len(client.output(slicer, 'slices'))                       # a reference names one row
slices = client.accumulator(Template(SLICES, params={'sample': {'file': run}}, blanks=('time_range',)))
found = [slicer.ref('slices', key=str(i)) for i in range(n)]
slices.push(time_range=found)

assert client.output(slices, 'sliced').value == 21.0           # 1 + 2 + ... + 6 in three slices
provenance = client.provenance(slices)
assert provenance.request.params['time_range'] == found
assert provenance.records() == [slicer]

slices.push(time_range=[TimeRange(start=0, stop=2)])           # slice 0, typed in
with pytest.raises(NotReadable, match=r'push 1 failed: time_range\[3\]: repeats'):
    client.output(slices, 'sliced')
```

The client reads how many slices there are, since a reference names one row, never a whole list.
The state's request holds the references, so it says where each slice came from.
The rows of a table are compared as the request holds them and, since `TimeRange` has a JSON form, also by the rows read.
So `TimeRange(start=0, stop=2)` and `slicer.ref('slices', key='0')` are one row.
The repeat is found when the push is read, so the push fails and stops the accumulator, as any push whose data fail does.

### I2. A beam centre typed in or computed

Actor: user. Goal: give one multi-run spec a beam centre that a first reduction found, or one typed in.

```python
direct_beam = measure(600, [0.5, 1.5])
sample = measure(611, [12.0, 36.0])
can = measure(620, [1.0, 1.0])
finder = client.compute(FIND_CENTRE, params={'direct_beam': {'file': direct_beam}})
t611 = client.compute(CONTRIBUTE, params={'sample': {'file': sample}})   # transmission 1 / 4
computed = client.accumulator(Template(CENTRED_MULTI, params={'centre': finder.ref('centre')},
                                       blanks=('sample', 'can')))
typed = client.accumulator(Template(CENTRED_MULTI, params={'centre': Centre(x=1.0)},
                                    blanks=('sample', 'can')))
for accumulator in (computed, typed):
    accumulator.push(sample=[{'file': sample, 'transmission': t611.ref('transmission')}])
    accumulator.push(can=[{'file': can}])

assert client.output(finder, 'centre') == Centre(x=1.0)        # the mean of [0.5, 1.5]
assert [client.output(a, 'centred').values.tolist() for a in (computed, typed)] == [
    [1.0, 7.0], [1.0, 7.0]]                                     # [12, 36] / 4 - [1, 1] - 1
assert client.provenance(computed).records() == [finder, t611]
assert client.provenance(typed).request.params['centre'] == {'x': 1.0}
```

Both drivers get a `Centre`; neither sees whether it was typed in or computed.
The record of the typed-in centre holds the inline value in its JSON form, and that of the computed one holds the reference, so its provenance reaches the finder.
Both accumulators give the sample's transmission by reference to `t611`, as in I3.
`IOFQ`'s `beam_centre`, a `BeamCentre`, has a field that is a data value, so it takes a reference only (C1).

### I3. A transmission computed for each run

Actor: user. Goal: sum two runs, each with the transmission computed for it.

```python
runs = [measure(611, [1.0, 3.0]), measure(612, [4.0, 4.0])]
can = measure(620, [1.0, 1.0])
t611, t612 = (client.compute(CONTRIBUTE, params={'sample': {'file': run}})   # transmissions 1 / 4 and 1 / 2
              for run in runs)
total = client.accumulator(Template(CENTRED_MULTI, params={'centre': Centre(x=0.0)},
                                    blanks=('sample', 'can')))
total.push(sample=[{'file': run, 'transmission': t.ref('transmission')}
                   for run, t in zip(runs, (t611, t612), strict=True)])
total.push(can=[{'file': can}])

assert client.output(total, 'centred').values.tolist() == [1.25, 1.75]   # [1, 3] / 4 + [4, 4] / 2 - [1, 1]
assert client.provenance(total).records() == [t611, t612]
```

A push gives each field of a row inline or by reference: here the file of each run as a dataset reference, and its transmission by reference to the record computed for that run.
`Transmission` has a field that is a data value, so a transmission takes a reference only.
The provenance of the sum reaches both records.

## Open

What the design leaves open or defers, with the stories each item affects.

- **Removing a dataset** (system story A4): deferred, together with whether the outputs derived from it go too.
- **The service** (sections D and E): designed and not implemented; the format of each persisted output type and the layout of the store are open ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).
- **Looks that compute** (B4, D7): a selection only indexes; a projection or a thick slice summed waits for the plotting work; README.md open question 4.
- **Persist at submission** (D1, D2, D3, D6, E1 to E4): whether `persist=` should also keep the values for the client; README.md open question 6.
- **Labels and members** (D1, D2, and every story that calls `apply`): `member_field`, and labels and members on records, are tentative; README.md open question 3.
- **Grouping** (system story D7): spreading one accumulator over several nodes needs a merge of two drivers; README.md open question 1.
- **Recomputing in a record's environment** (F2): deferred.
- **Publishing** (C2): a published output is a file in the catalogue, which a workflow reads as it reads a run; `publish` and SciCat are not implemented.

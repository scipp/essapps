# ESS data-reduction framework: the API

**Status: the in-process backend (`local()`) implements this API, including freeze, `select=`, and persist to a fake store. Not implemented: the service, `client.publish`, software versions in provenance, and marking the records of code bound in a notebook.**

This document shows by example what workflow authors, app authors, and notebooks write.
It states each rule once.
The [ADRs](adr/index.md) give the reasons, and [the requirements](../requirements/README.md) the goals.
[user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.
[three-ways-to-run-a-spec.html](three-ways-to-run-a-spec.html) shows the main concepts on two slides.
The first release combines no runs and tunes no parameters interactively.
It needs neither the section Stages and accumulators nor the loop over arrivals in Drivers.

## From a for loop

A notebook today:

```python
curves = {}
for run in (60339, 60340, 60341):
    curves[run] = reduce_iofq(run, bins=100)
```

The same with the framework:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, {'run': dataset(run=run), 'bins': 100})
client.wait(records)
client.output(records[60339], 'iofq')
```

`client` connects to the framework (see Client and backend).
`IOFQ` is the spec of the I(Q) reduction, which names its parameters and outputs.
`dataset(run=run)` names the raw data of a run (see Datasets).
`client.submit` returns a *record* at once.
The three reductions run in parallel, possibly on another machine.
`client.wait` blocks until they have finished, and `client.output` reads an output by name.

What the framework adds to the plain loop:

- Each call is kept as a record. The same process finds it later, and on the service, other notebooks and programs too.
- Output values are kept for the client that asked, and for every client of the proposal once persisted.
- Calls can run elsewhere and in parallel.
- Every output can answer where it came from: which spec, which parameter values, which datasets, which software versions.

## Terms

The framework is essdispatch (`ess.dispatch`) and essspec (`ess.spec`), in this repository.

| Term | What it is |
|---|---|
| backend | the process that runs requests and keeps records |
| client | a connection to one backend, for one proposal (an experiment at ESS) |
| spec | a workflow's name, version, parameters, and outputs |
| binding | the code that computes a spec |
| request | a spec and its parameter values |
| logged | recorded by the backend in the proposal's *history*. `submit` and the other calls that start work return once logged. |
| record | a request as the backend logged it. A record never changes, only its status. |
| reference | an input that points to an output or a dataset |
| label, member | names under which records are found later |
| template | a spec with *fixed values* for some parameters and *blanks* for the others |
| store, persist | the *store* keeps outputs outside the backend's memory. To *persist* an output is to have it written there. |
| dataset source | where a backend finds datasets |
| table, row | a parameter that holds a list of rows. A row is one run, or the runs that belong together. |
| stage | a template the backend keeps for a client, to compute its fixed part once |
| accumulator | a template whose blanks are tables, kept by the backend for a client |
| push | one call that gives an accumulator a row for one or more of its tables |
| held state | the object the binding makes from an accumulator's fixed values, which takes the pushes. The framework never looks inside it. |
| added | a push is added once the backend has applied it to the held state, in the order logged |
| state n | the accumulator after its first n pushes |
| plain request | a request with every value given. A call through a stage and a state of an accumulator have plain requests too. |
| pin | to take an accumulator's state after the pushes logged so far (see Pins) |
| freeze | to pin an accumulator's last state and turn it into a record, without a copy |


## Client and backend

```python
client = local(proposal='p1', datasets=..., bind={IOFQ: iofq, ...}, store=...)
```

`local` makes a backend in the user's process and a client of it, for interactive work such as a notebook.
`datasets` is the dataset source (see Datasets), `bind` maps each spec to its binding, and `store` is optional (see Persist).
Batch and automatic reduction run on *the service*, a backend that DMSC hosts.
`connect('https://reduce.example', proposal='p1')` makes a client of it.
The service runs only workflows from installed packages.

## Specs and bindings

A *workflow* is a computation that a package offers.

**Spec.** A workflow package declares a spec (`ess.spec.WorkflowSpec`) for each workflow.
Its params and outputs are pydantic models with field types from `ess.spec`.
A *data field* holds data, such as an array or a file, and a request gives its value by reference (see References).

```python
class IofQParams(BaseModel):
    run: NexusFile                          # a data field: a raw NeXus file
    bins: int = 100
    can: NexusFile | None = None            # the empty-can run, if any
    beam_centre: Array() | None = None      # a data field: a scipp array

class IofQOutputs(BaseModel):
    iofq: Array(ArraySpec(dims=('Q',), unit='dimensionless'))

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

A request computes every output its spec declares.

**Binding.** The simplest binding is a function that takes the parameters by name and returns the outputs by name:

```python
def iofq(run, bins, can, beam_centre) -> dict:
    ...
    return {'iofq': result}
```

The backend passes every parameter, with the defaults of the params model filled in.
Defaults belong in the params model only.
A binding must not modify its inputs or return an output that shares memory with an input.
`ess.spec.testing` checks this for values backed by numpy.

A sciline pipeline becomes a binding by naming the key that each parameter sets and the key that computes each output:

```python
PipelineBinding(pipeline,       # from ess.spec.pipeline
                params={'run': Filename[SampleRun], 'bins': QBins, ...},
                outputs={'iofq': BackgroundSubtractedIofQ})
```

`local` can also bind a spec to code defined in the notebook, such as `bind={IOFQ: draft}`.
A record made this way notes that its binding was defined in the notebook.

## Requests and records

Submitting a request returns a *record*.
The record holds the request with every parameter value filled in, defaults included, and the names of its outputs.
It holds no output values.
Running the same request again makes a new record.
Its *status* is `pending`, then `completed`, `failed`, or `cancelled`.

```python
result = client.compute(IOFQ, {'run': dataset(run=60339), 'bins': 100})   # submit, then wait
client.output(result, 'iofq')            # raises if the record failed
client.output(result, 'iofq', select={'Q': slice(0, 10)})
```

In the user's process, `client.output` of a record returns the output value itself, not a copy.
The framework never changes a record's output value (see open question 7).
`select=` copies the part of an array output that it names, by dimension, and makes no record.
On the service, only that part leaves the backend.

| Call | Does |
|---|---|
| `client.submit(...)` | submits and returns pending records |
| `client.compute(...)` | `submit`, then `wait` |
| `client.status(records)` | the current status of each record |
| `client.wait(records)` | blocks until all have finished, and does not raise failures |
| `client.failure(records)` | why each record failed, or `None` |
| `client.cancel(records)` | cancels unfinished records of the proposal, including those of other clients |
| `client.as_completed(records)` | yields the records as they finish |
| `client.release(records)` | drops the client's hold on their outputs |

Each takes one request or record, a list, or a dict, and returns the same shape.
`Request(IOFQ, params)` makes a request without submitting it.
A caller without the workflow package gives `SpecId(name='sans-iofq', version=1)` in place of `IOFQ`.
A request that cannot run is refused at submission with a `SubmitError` naming the field at fault.
If one request of a call is refused, none is submitted.

## References

An input is given by *reference*: to an output of a record, to a dataset, or to an output of an accumulator (see Stages and accumulators).
A reference to a pending record is allowed, so a chain is submitted without waiting:

```python
centre = client.submit(BEAM_CENTRE, {'run': dataset(run=60330)})   # pending
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
```

The backend runs the second request once the first has completed.
A request never references another request of the same `submit` call.
An output fits a parameter if both are data fields of the same format and, where both declare an `ArraySpec`, the two are equal.

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment.
A *member* names one part of a label, such as one sample or one temperature.
Both are optional and given at submission, not in the request.

```python
client.compute(IOFQ, {'run': run, 'bins': 50}, label='iofq', member='250K')
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # dict keys become the members
client.latest('iofq', member='250K')                  # the newest record of this member
client.records(label='iofq')                          # oldest first; without label=, every record of the proposal
{r.member: r for r in client.records(label='iofq')}   # the newest of each member: later records replace earlier ones
```

## Templates

A *template* is a spec, its fixed values, and its blanks, the names of the parameters left open.
Stages, accumulators, and automatic reduction are built from templates.
A template can be made from any request's values.
A name in `blanks` drops the value given for it:

```python
template = Template(IOFQ, params={'bins': 100}, blanks=('run',))
final = client.latest('iofq', member='250K')    # the final reduction of one sample
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))   # its run left open
```

## How long records and values are kept

Records are the proposal's history ([ADR 0004](adr/0004-history-is-append-only-lists.md)).
They are dropped as a whole once the proposal has been idle for days to weeks.
A proposal is idle while it has no open client, no pending record, and no write to the store in progress.

**Values.** An output value, such as an I(Q) array, lives while something keeps it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)):

| Keeper | Keeps the value until |
|---|---|
| the client that asked for the record (`submit`, `compute`, or `freeze` without `persist=`) | the client releases the record or ends |
| a pending request that reads it | the request's workflow has returned |
| a push, a stage, or an accumulator's template that references it | the binding has taken it, or the push, stage, or accumulator is released, stopped, or dropped |
| a persist request | the value is written |
| the store | the proposal's history is dropped |

Nothing else keeps a value, not a record object, a reference, or a label.
A value that is gone does not come back.

The binding is a black box.
The framework hands it a stage's fixed values at the stage's first call, an accumulator's template when it makes the held state, and a push's rows when the push is added.
The framework does not track what the binding keeps after that.

A submission may reference only

- output values its client keeps,
- persisted outputs,
- datasets,
- the current state of its client's own accumulators.

Any other reference is refused, even to a record that is still pending.
`client.output` reads only the same values, and raises for any other.
So another client, even one in the same process, reads a value only once it is persisted.

```python
centre = client.compute(BEAM_CENTRE, {'run': dataset(run=60330)})
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request keeps the centre until it has run
client.output(result, 'iofq')       # allowed: this client asked for the record
client.output(centre, 'centre')     # raises: this client released it
```

`client.close()`, or the end of a `with client:` block, releases everything the client keeps.
Closing a client made with `local()` also closes its backend, after the persisted outputs are written.

**Work that nothing keeps is cancelled.** A pending record whose outputs nothing would keep finishes as `cancelled`.
So releasing a pending record that no pending request reads cancels it.
A cancelled request keeps nothing it reads, so cancelling passes along a chain.
A running workflow is not interrupted.
Its record is cancelled at once, and its outputs are dropped when it returns.

**Persist.** Nothing is written unless persisted ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md)).
An output is *persisted* while a persist request names it and its write has not failed.

```python
client.persist(result, 'iofq')     # the store keeps iofq too; every output if none is named
client.submit(IOFQ, {'run': dataset(run=60341)}, label='night', persist=('iofq',))   # only the store keeps iofq
morning = connect(url, proposal='p1')                       # the next day, any process
morning.output(morning.records(label='night')[0], 'iofq')   # read from the store
```

- `client.persist` adds the store as a keeper next to the client.
- `persist=` at submission makes the store the only keeper. `persist=True` names every output, and outputs not named are dropped. A record persisted this way completes once its outputs are written, and fails if a write fails.
- A client reads the values it keeps from memory. It reads other persisted values from the store, after waiting for their write.

**The two deployments.** In the user's process, values live in its memory, and `local(store=...)` sets the store.
The service keeps values in its memory and limits the memory of each client (the *cap*).
It ends a client that makes no call within its *lease*, and gives each proposal a store.

## Datasets

```python
dataset(run=60339)
dataset(path='/home/user/data/run1.h5')
dataset(pid='20.500.12269/vanadium')     # an output published in a catalogue
```

A request names a dataset by what a person knows, such as a run number.
The backend resolves the name to the dataset's identity, which the record holds in place of the name.
The record holds no metadata.
A client reads it from the dataset source.

**Dataset source.** A backend finds datasets through its *dataset source*, which DMSC deploys with it.
A client lists datasets, waits for new ones, and reads their metadata through `client.datasets`, from the backend's dataset source.
A client sees only the datasets its proposal may read.
A *selector* picks datasets by metadata.
It matches raw datasets unless it names another kind, such as calibration.

```python
samples = client.datasets.list(Selector(role='sample'))       # dataset references, usable as inputs
for run in client.datasets.watch(Selector(scan='17')): ...   # existing ones first, then new ones
client.datasets.metadata(run)['sample']                       # the source's current values
```

## Combining runs: tables

Many reductions combine several runs, such as the sample and can runs of SANS.
A spec takes them as *tables*, next to the parameters that all runs share.
A table is a list of *rows*.
A row is a *flat* model, whose fields are never another model or table.

```python
class SampleRow(BaseModel):
    run: NexusFile
    transmission: NexusFile | None = None   # hypothetical: esssans takes one transmission run for all runs of a sample

class CanRow(BaseModel):
    run: NexusFile

class MultiIofQParams(BaseModel):
    sample_runs: list[SampleRow] = Field(min_length=1)   # a table, with at least one row
    can_runs: list[CanRow] = []             # a table; empty if there is no can run
    bins: int = 100                         # shared by all runs
    beam_centre: Array() | None = None

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, ..., params=MultiIofQParams, outputs=IofQOutputs)
```

Each row of a request is a dict of values or references, one per field.
A request that gives every row is a plain request:

```python
result = client.compute(IOFQ_MULTI, {'can_runs': [{'run': dataset(run=614)}],
                                     'sample_runs': [{'run': dataset(run=611)}, {'run': dataset(run=612)}]})
```

A row is what the reduction repeats per run, such as a run and its own transmission run.
Rows of different tables are independent.
Pairing runs into rows is the application's job.
The binding handles structure below a row, such as detector banks, and decides what is summed ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

**Single-run and multi-run specs.** `IOFQ_MULTI` is a *multi-run spec*, with a table of runs of each kind.
`IOFQ` is the *single-run spec* of the same reduction, with one run of each kind.
A package may offer both, and they declare the same outputs model.
With one row in each table, the multi-run spec gives the same outputs as the single-run spec.
An empty table stands for an optional run left out: `'can_runs': []` matches `'can': None`.

## Stages and accumulators

A plain request loads the run again for each value of `bins`.
Adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators keep what such work computes in memory between requests.
The savings depend on the binding (see What a binding provides).
A stage of a `PipelineBinding` computes the fixed part once, and a function binding computes everything at each call.
An accumulator adds a run without reducing the earlier ones only if its binding accumulates.
A client keeps a stage or an accumulator until it releases it or ends.

**Stage.** A *stage* is a template that the backend keeps for a client:

```python
tune = client.stage(Template(IOFQ, params={'run': dataset(run=60339)}, blanks=('bins',)))
for bins in (50, 100, 200):
    client.compute(tune, {'bins': bins}, label='iofq')   # the first call loads the run
```

A call through the stage fills the blanks and makes the record of the plain request.
The first call *stages* the binding with the fixed values.
The binding then computes what depends only on them, and later calls compute only the rest.
If staging fails, the stage *stops*.
The first call and the calls waiting to run fail, and later calls are refused, each with the reason.

**Accumulator.** An *accumulator* is a template whose blanks are tables.
The backend keeps it for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)):

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={'bins': 100}, blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611)}})   # one row
iofq.push({'sample_runs': {'run': dataset(run=612)},    # one row in each of two tables
           'can_runs': {'run': dataset(run=614)}})
client.output(iofq, 'iofq')                              # the output of state 2
```

The plain request of state n is the template with each table filled by the rows of the first n pushes, in push order.
State n has the outputs of its plain request.
`client.accumulator` and `push` return once logged.
The backend makes the held state, and adds each push, later.

**Pins.** A call that pins takes state n, where n is the number of pushes logged so far.
Pinning does not wait for these pushes to be added.
`client.output` then waits for state n before it copies.

| Call | Returns | Holds back push n+1 |
|---|---|---|
| `client.output(acc, 'iofq')` | a copy of the output of state n, or of the part `select=` names, and makes no record | while it waits for state n and copies |
| `client.submit(EXPORT, {'data': acc.ref('iofq')})` | a record of the `EXPORT` request. The submission also makes the record of state n, whose outputs the request reads without a copy. | from submission until the request's workflow has returned |
| `client.freeze(acc)` | the record of state n (see Freeze) | no push follows |
| `client.provenance(acc)` | the provenance of the plain request of state n, at once | no |

No client keeps the record of a state that a submission makes ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)), and `client.output` of it raises.
A request may reference at most one accumulator.
A row or a template may reference none ([ADR 0003](adr/0003-accumulators-add-in-place.md)).
Only the client that made an accumulator pins it.

A push validates each row against its table's row model.
A pin validates the plain request of its state, including table lengths and the params model's own validators.
It is refused if that request would be refused.

The accumulator *stops* if

- adding a push fails,
- a record that a push's rows reference does not complete, or
- the binding fails to make the held state.

Later pushes and pins are refused with the reason, and the records of states it did not reach fail.

**Adding waits for readers.** A notebook that uses a binding with `held_state` directly (see What a binding provides) is done with each state before the next push:

```python
held = binding.held_state(fixed)        # fixed: the template's fixed values
for run in runs:
    held.push({'runs': {'run': run}})
    counts = held.outputs()['counts']   # may be the array the next push adds into
    show(counts['q', 0])                # done with it before the next push
```

The framework keeps the same order when pushes and readers run in parallel.
The example uses the toy specs `VOLUME`, which sums counts over a table of runs, and `CUT`, which takes the value at `index` ([user-stories.md](user-stories.md)):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
cuts = []
for run in runs:
    volume.push({'runs': {'run': run}})                                  # returns at once
    cuts.append(client.submit(CUT, {'data': volume.ref('counts'), 'index': 0}))
```

Each cut sees the state after the push before it in the loop, and the next push waits for that cut.
In Rust's terms, each reader of a state holds a shared borrow of the held state, and a push waits for a mutable borrow.

The rule is that push n+1 waits for the *readers* of state n:

- requests that read the outputs of the record of state n, from their submission until their workflow has returned, even if cancelled while it runs;
- pins of state n, until they have what they asked for (`client.output` until it has copied).

It has two reasons ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

- The held state only moves forward. After push n+1 is added, the outputs of state n cannot be computed again.
- The binding's `push` may change in place the arrays its `outputs()` returned, as with `counts` above.

So no reader sees a value change.

**Freeze.** `client.freeze(acc)` pins the last state and returns its record:

```python
total = client.freeze(iofq)              # no copy, no more pushes
client.output(total, 'iofq')             # as any record's output
client.freeze(volume, persist=True)      # the store keeps every output, not the client
```

The client keeps the record, unless `persist=` hands it to the store.
At the end of a scan, freezing makes the final volume an ordinary record without a copy.
The client can keep it, persist it, or reference it from another template.

**Releasing.** `client.release(acc)`, or ending its client, returns at once:

```python
iofq.push({'sample_runs': {'run': dataset(run=613)}})
exported = client.submit(EXPORT, {'data': iofq.ref('iofq')})   # pins the state after this push
client.release(iofq)                                           # the push is added, and the export runs
```

The pushes up to the last pinned state are still added, and later pushes are never added.
The held state is dropped once the readers of its states are done.

## What a binding provides

The backend calls a binding in two steps:

```python
call = binding.stage(fixed, blanks)   # fixed: {name: value}; blanks: the names left open
call(bins=50)                         # values for the blanks; once per call through the stage
```

A plain request is the case with no blanks.
A `PipelineBinding` computes what does not depend on the blanks once, through `sciline.Stage`.

A binding of a spec with tables may also provide `held_state(fixed)`, which makes the held state of an accumulator.
Such a binding *accumulates*:

```python
held = binding.held_state(fixed)         # every value but the tables, data fields loaded
held.push({'runs': {'run': run_611}})    # one row per named table, data fields loaded
held.outputs()                           # every output
```

The binding's author promises three things, which the backend does not check:

- After rows are pushed, `outputs()` gives what the plain request over those rows gives.
- `push` may change the held state in place. It must not change the rows passed to it.
- The spec's validators that read more than one value, such as the params model's own, change no value, since the held state got each value typed by its own field.

`combine(operator.add)` is such a binding.
It combines each field of the rows with `operator.add` (story S4).

For a binding without `held_state`, the held state keeps the rows pushed so far.
Each state's outputs are computed as its plain request, through one `binding.stage` call, so the work per run repeats for every row so far.
In the user's process, this held state keeps the data of every row in memory while the accumulator lives.
`PipelineBinding` provides no `held_state` (scipp/essapps#40).

## Drivers

The backend does not decide what to run.
A *driver* decides over time what to submit, push, and release.
It is ordinary code that uses the client, such as a notebook or a server that runs automatic reduction.
Drivers never run in the backend.

**A loop over arrivals** waits for new datasets.
This one adds each run of a rotation scan to a volume as it arrives, and cuts through the volume after each run (story D7):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.datasets.watch(Selector(scan='17')), 300):   # the scan's 300 runs
    volume.push({'runs': {'run': run}})                    # added once the previous cut is copied
    show(client.output(volume, 'counts', select={'q': 0}))
total = client.freeze(volume)
```

The binding of `VOLUME` accumulates, so the loop holds one volume and keeps one record, from `freeze`.

**A loop over records** reduces each dataset in a request of its own.
`client.as_completed` consumes a generator of records in a thread, and yields each record once it has finished:

```python
def submissions():
    for run in client.datasets.watch(Selector(role='sample')):
        yield client.submit(IOFQ, {'run': run})
for record in client.as_completed(submissions()):
    show(client.output(record, 'iofq'))
```

## Batch and automatic reduction

[automatic-reduction.md](automatic-reduction.md) states these rules in full.
`apply` fills a template once per dataset.
A *lookup* fills further blanks, such as the latest can run before each sample run.
`apply` returns the requests keyed by the value of a metadata field (`member_field`):

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, client.datasets,
                 member_field='temperature', lookup=cans)
client.submit(requests, label='iofq', persist=True)
```

A *rule* is plain data: a name, a template, a selector, a label, and optionally a lookup, a series, and the outputs it persists.
The *trigger loop* is a driver that fills the template for each new dataset the selector matches, and submits it under the label, persisted.
With a *series*, one request takes all matching datasets so far with the same value of a metadata field.
An example is all angles of one sample at ESTIA, which the toy spec `STITCH` stitches:

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
TriggerLoop(client, rules=[rule]).run()
```

## Provenance and publication

```python
provenance = client.provenance(result)
provenance.datasets(), provenance.records(), provenance.software
pid = client.publish(result.ref('iofq'), 'scicat')     # the output and its provenance
```

Provenance lists the record's request, every record that supplied an input (directly or through other records), the datasets they used, and the software versions.
If an input is an accumulator's state, the record listed is the record of that state.
Provenance stops at datasets.

Publishing puts an output in a catalogue with its provenance.
The catalogue entry stays after the proposal's history is dropped.
`publish` reads the value as `client.output` does, so it needs no persist request.
A workflow bound in a notebook can be published, and its record says so.

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs.

| Symmetry | The caller may change | Unchanged | Kept by |
|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec with one row per table | the outputs | the binding's author |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record, the binding's author for the outputs |
| arrival | rows pushed one at a time ↔ the same rows in one plain request | the outputs of state n | the binding's author if the binding accumulates, else the framework |
| order | the order of pushes into different tables, and which rows share a push | the outputs once the same rows are in | the binding's author if the binding accumulates, else the framework |
| placement | where a request runs | the record and its outputs | the framework |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries the binding's author keeps, by comparing two calls with the same inputs: `check_one_row`, `check_caching`, and `check_arrival_and_order`.

## Left to the system

Not visible in the code of notebooks, apps, or workflow packages:

- how history and outputs are stored, moved, and dropped
- how a run number or file becomes a dataset identity
- when and where a request runs, and where a stage or a held state lives
- the format of persisted outputs and the store's layout (scipp/essapps#23)
- how the service notices a vanished client and sets the cap per client (scipp/essapps#27, scipp/essapps#34)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes needs a merge of two held states. No requirement asks for it ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** Whether an accumulator offers `remove` depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Looks that compute.** `select=` only indexes an output. A projection, or a thick slice summed, would run a spec next to the value without making a record. The design of such looks waits for the plotting work. So does reading the latest state already added, without a pin.
5. **Accumulators on the service.** How the service holds a held state, bounds its memory, and drops it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), scipp/essapps#27).
6. **Persist at submission.** Should `persist=` at submission also keep the values for the client? To be decided when a story needs it.
7. **Values modified in place.** In the user's process, a notebook that modifies a value `client.output` returned changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.

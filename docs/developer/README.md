# ESS data-reduction framework: the API

**Status: the in-process backend (`local()`) implements this API, including freeze, `select=`, and persist to a fake store. Not implemented: the service, `client.publish`, and software versions in provenance.**

This document shows by example what workflow authors, app authors, and notebooks write.
It states each rule once.
The [ADRs](adr/index.md) give the reasons, and [the requirements](../requirements/README.md) the goals.
[user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.
[three-ways-to-run-a-spec.html](three-ways-to-run-a-spec.html) shows the main concepts on two slides.
[prior-art/](prior-art/README.md) holds studies of other systems.
The first release combines no runs and tunes no parameters interactively.
It needs neither the section Stages and accumulators nor the loop over arrivals in Controllers.

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
    params = IofQParams(sample=Run(file=dataset(run=run)), reduction=Reduction(bins=100))
    records[run] = client.submit(IOFQ, params)
client.wait(records)
client.output(records[60339], 'iofq')
```

`client` connects to the framework (see Client and backend).
`IOFQ` is the spec of the I(Q) reduction, which names its parameters and outputs, and `IofQParams` is its params model (see Specs and bindings).
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
| binding | what the backend calls to compute a spec's requests (see What a binding provides) |
| request | a spec and its parameter values |
| logged | recorded by the backend in the proposal's *history*. `submit` and the other calls that start work return once logged. |
| record | a request as the backend logged it. A record never changes, only its status. |
| reference | an input that points to an output or a dataset |
| label, member | names under which records are found later |
| template | a spec with *fixed values* for some parameters and *blanks* for the others |
| store, persist | the *store* keeps outputs outside the backend's memory. To *persist* an output is to have it written there. |
| dataset source | where a backend finds datasets |
| group | a parameter that holds a flat model, such as the settings of one reduction step, or one run |
| table, row | a parameter that holds a list of rows. A row is one run, or the runs that belong together. |
| stage | a template the backend keeps for a client, to compute its fixed part once |
| accumulator | a template whose blanks are tables, kept by the backend for a client |
| push | one call that gives an accumulator rows of one of its tables |
| driver | the object a binding makes from an accumulator's fixed values, which takes the pushes. The workflow package writes it, and the framework never looks inside it. |
| added | a push is added once the backend has pushed its rows into the driver, in the order logged |
| controller | client code that decides over time what to submit, push, and release, such as the trigger loop (see Controllers) |
| state n | the accumulator after its first n pushes |
| plain request | a request with every value given. A call through a stage and a state of an accumulator have plain requests too. |
| pin | to take an accumulator's state after the pushes logged so far (see Pins) |
| freeze | to pin an accumulator's last state and turn it into a record, without a copy |


## Client and backend

```python
client = local(proposal='p1', datasets=..., bind=[iofq, ...], store=...)
```

`local` makes a backend in the user's process and a client of it, for interactive work such as a notebook.
`datasets` is the dataset source (see Datasets), `bind` lists a binding for each spec (see Specs and bindings), and `store` is optional (see Persist).
Batch and automatic reduction run on *the service*, a backend that DMSC hosts.
`connect('https://reduce.example', proposal='p1')` makes a client of it.
The service runs only workflows from installed packages.

## Specs and bindings

A *workflow* is a computation that a package offers.

**Spec.** A workflow package declares a spec (`ess.spec.WorkflowSpec`) for each workflow.
Its params and outputs are pydantic models with field types from `ess.spec`.
A *data field* holds data, such as an array or a file, and a request gives its value by reference (see References).
Each parameter, a field of the params model, is a *group* or a *table*.
A group holds a flat model, one whose fields are never another model or table, and a table holds a list of them (see Combining runs: tables).
So a group and a row of a table are the same kind of model, and the single-run spec takes `sample: Run` where the multi-run spec takes `sample: list[Run]`.

```python
class Run(BaseModel):                       # a group of IOFQ, and a row of a table
    file: NexusFile                          # a data field: a raw NeXus file

class Reduction(BaseModel):
    bins: int = 100

class Corrections(BaseModel):
    beam_centre: Array() | None = None      # a data field: a scipp array

class IofQParams(BaseModel):
    sample: Run
    can: Run | None = None                  # the empty-can run, if any
    reduction: Reduction = Field(default_factory=Reduction)
    corrections: Corrections = Field(default_factory=Corrections)

class IofQOutputs(BaseModel):
    iofq: Array(ArraySpec(dims=('Q',), unit='dimensionless'))

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

A request computes every output its spec declares.

**Binding.** A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec.
The simplest binding is a function that takes the parameters by name and returns the outputs by name.
It gets a group as a dict of its fields, and a table as a list of such dicts:

```python
def iofq(sample, can, reduction, corrections) -> dict:
    ...                                     # reduction['bins'], corrections['beam_centre']
    return {'iofq': result}

draft = function_binding(IOFQ, iofq)        # from ess.spec
```

The backend passes every parameter, with the defaults of the params model filled in.
Defaults belong in the params model only.
A binding must not modify its inputs or return an output that shares memory with an input.
`ess.spec.testing` checks this for values backed by numpy.
A sciline pipeline, or a driver that accumulates rows, is bound the same way (see What a binding provides).

`local` can also bind a spec to code defined in the notebook, such as `bind=[draft]`, since that code runs in the user's own process.
The service runs only installed workflows.

## Requests and records

Submitting a request returns a *record*.
The record holds the request with every parameter value filled in, defaults included, and the names of its outputs.
It holds no output values.
Running the same request again makes a new record.
Its *status* is `pending`, then `completed`, `failed`, or `cancelled`.

```python
params = IofQParams(sample=Run(file=dataset(run=60339)), reduction=Reduction(bins=100))
result = client.compute(IOFQ, params)    # submit, then wait
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
| `client.release(records)` | drops the client's hold on their outputs |

Each takes one request or record, a list, or a dict, and returns the same shape.
Wherever the client takes values, it takes models or dicts of their fields: `IofQParams(...)` and `{'sample': {'file': ...}}` give the same request.
`Request(IOFQ, params)` makes a request without submitting it.
A caller without the workflow package gives `SpecId(name='sans-iofq', version=1)` in place of `IOFQ`.
A request that cannot run is refused at submission with a `SubmitError` naming the field at fault.
If one request of a call is refused, none is submitted.

## References

An input is given by *reference*: to an output of a record, to a dataset, or to an output of an accumulator (see Stages and accumulators).
A reference to a pending record is allowed, so a chain is submitted without waiting:

```python
centre = client.submit(BEAM_CENTRE, BeamCentreParams(direct_beam=Run(file=dataset(run=60330))))   # pending
result = client.submit(IOFQ, IofQParams(sample=Run(file=dataset(run=60339)),
                                        corrections=Corrections(beam_centre=centre.ref('centre'))))
```

The backend runs the second request once the first has completed.
A request never references another request of the same `submit` call.
An output fits a field of a group or a row if both are data fields of the same format and, where both declare an `ArraySpec`, the two are equal.

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment.
A *member* names one part of a label, such as one sample or one temperature.
Both are optional and given at submission, not in the request.

```python
client.compute(IOFQ, IofQParams(sample=Run(file=run), reduction=Reduction(bins=50)), label='iofq', member='250K')
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # dict keys become the members
client.latest('iofq', member='250K')                  # the newest record of this member
client.records(label='iofq')                          # oldest first; without label=, every record of the proposal
{r.member: r for r in client.records(label='iofq')}   # the newest of each member: later records replace earlier ones
```

## Templates

A *template* is a spec, its fixed values, and its blanks, the names of the parameters left open.
Stages, accumulators, and automatic reduction are built from templates.
A blank is a whole parameter, never a field of a group.
`Template.from_spec(IOFQ, ...)` takes the parameters as the params model does, with `BLANK` for each blank (both from `ess.dispatch`):

```python
template = Template.from_spec(IOFQ, sample=BLANK, reduction=Reduction(bins=100))
```

As with `IofQParams(...)`, an IDE suggests the parameters, and pyright (which Pylance uses) reports one that is misspelt, missing, or of the wrong type.
mypy does not, since it cannot read the signature through the spec.
`Template.from_spec` validates each value given by its own field, and a required parameter may be a blank.
A check that reads several parameters is therefore a model validator, never a field validator reading `info.data`.
The params model's own validators, which read several parameters, run on each request made from the template.

A template can also be made from any request's values.
A name in `blanks` drops the value given for it:

```python
final = client.latest('iofq', member='250K')    # the final reduction of one sample
beamtime = Template(final.request.spec, params=final.request.params, blanks=('sample',))   # its sample left open
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
The framework hands it a stage's fixed values at the stage's first call, an accumulator's template when it makes the driver, and a push's rows when the push is added.
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
centre = client.compute(BEAM_CENTRE, BeamCentreParams(direct_beam=Run(file=dataset(run=60330))))
result = client.submit(IOFQ, IofQParams(sample=Run(file=dataset(run=60339)),
                                        corrections=Corrections(beam_centre=centre.ref('centre'))))
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
client.submit(IOFQ, IofQParams(sample=Run(file=dataset(run=60341))), label='night', persist=('iofq',))   # only the store keeps iofq
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
A row is a *flat* model, whose fields are never another model or table, as a group is.

```python
class MultiIofQParams(BaseModel):
    sample: list[Run] = Field(min_length=1)   # a table, with at least one row
    can: list[Run] = []                     # a table; empty if there is no can run
    reduction: Reduction = Field(default_factory=Reduction)   # a group, shared by all runs
    corrections: Corrections = Field(default_factory=Corrections)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, ..., params=MultiIofQParams, outputs=IofQOutputs)
```

Each row of a request is a model or a dict of values or references, one per field.
A request that gives every row is a plain request:

```python
result = client.compute(IOFQ_MULTI, MultiIofQParams(can=[Run(file=dataset(run=614))],
                                                    sample=[Run(file=dataset(run=611)),
                                                            Run(file=dataset(run=612))]))
```

A row is what the reduction repeats per run, such as a run and its own transmission run.
Rows of different tables are independent.
Pairing runs into rows is the application's job.
The binding's driver handles structure below a row, such as detector banks, and decides what is summed ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

**Single-run and multi-run specs.** `IOFQ_MULTI` is a *multi-run spec*, with a table of runs of each kind.
`IOFQ` is the *single-run spec* of the same reduction, with one run of each kind.
A package may offer both, and they declare the same outputs model.
With one row in each table, the multi-run spec gives the same outputs as the single-run spec.
We recommend that the multi-run spec name each table as the single-run spec names the group, with the same row type: `sample: list[Run]` for `sample: Run`.
A reader then sees at once which table holds the runs of which group, and a driver over the multi-run spec can reuse the keys of the single-run pipeline.
The framework knows of no link between the two specs.
Whether a binding accepts an empty table is the binding's choice.

## Stages and accumulators

A plain request loads the run again for each value of `reduction.bins`.
Adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators keep what such work computes in memory between requests.
Each needs a binding that serves it (see What a binding provides).
A stage needs a `PipelineBinding`, which computes the fixed part once.
An accumulator needs a `DriverBinding`, whose driver adds each run without reducing the earlier ones.
Making a stage or an accumulator over any other binding is refused.
A client keeps a stage or an accumulator until it releases it or ends.

**Stage.** A *stage* is a template that the backend keeps for a client:

```python
tune = client.stage(Template.from_spec(IOFQ, sample=Run(file=dataset(run=60339)), reduction=BLANK))
for bins in (50, 100, 200):
    client.compute(tune, {'reduction': Reduction(bins=bins)}, label='iofq')   # the first call loads the run
```

A call through the stage fills the blanks and makes the record of the plain request.
The first call has the binding make its stage from the fixed values.
The binding then computes what depends only on them, and later calls compute only the rest.
If staging fails, the stage *stops*.
The first call and the calls waiting to run fail, and later calls are refused, each with the reason.

**Accumulator.** An *accumulator* is a template whose blanks are tables.
The backend keeps it for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)):

```python
iofq = client.accumulator(Template.from_spec(IOFQ_MULTI, sample=BLANK, can=BLANK,
                                             reduction=Reduction(bins=100)))
iofq.push('sample', [Run(file=dataset(run=611))])        # one row
iofq.push('sample', [Run(file=dataset(run=612)),         # two rows of one table
                     Run(file=dataset(run=613))])
iofq.push('can', [Run(file=dataset(run=614))])
client.output(iofq, 'iofq')                              # the output of state 3
```

A push names one table and gives one or more of its rows, which enter one state.
Rows of different tables have nothing to do with each other, so a push never names two.
Rows that belong together, such as a run and its own transmission run, are one row of a model with two fields.
A push of no rows is refused.
The plain request of state n is the template with each table filled by the rows of the first n pushes, in push order.
State n has the outputs of its plain request.
`client.accumulator` and `push` return once logged.
The backend has the binding make the driver, and pushes each push into it, later.

**Pins.** A call that pins takes state n, where n is the number of pushes logged so far.
Pinning does not wait for these pushes to be added.
`client.output` then waits for state n before it copies.

| Call | Returns | Holds back push n+1 |
|---|---|---|
| `client.output(acc, 'iofq')` | a copy of the output of state n, or of the part `select=` names, and makes no record | while it waits for state n and copies |
| `client.submit(EXPORT, ExportParams(result=Result(data=acc.ref('iofq'))))` | a record of the `EXPORT` request. The submission also makes the record of state n, whose outputs the request reads without a copy. | from submission until the request's workflow has returned |
| `client.freeze(acc)` | the record of state n (see Freeze) | no push follows |
| `client.provenance(acc)` | the provenance of the plain request of state n, at once | no |

No client keeps the record of a state that a submission makes ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)), and `client.output` of it raises.
A row or a template may reference no accumulator ([ADR 0003](adr/0003-accumulators-add-in-place.md)).
Only the client that made an accumulator pins it.

A push validates each row against its table's row model.
A table whose rows hold a data field also refuses a row equal to an earlier row of the table, in every field, with datasets compared after resolution.
Pushing a run twice is usually a mistake, such as running a notebook cell again.
Only the same dataset, or the same output of the same record, counts as a repeat; a cell that computes a new record before pushing makes a different row.
The same dataset in several rows is fine; only a whole row is refused.
A push checks it against the pushes logged, since a pin that refused the repeat would refuse every later state.
A plain request with a repeated row is refused too, including one in a template.
A spec allows repeats for a table with `Annotated[list[Run], REPEATED_ROWS]`.
A pin validates the plain request of its state, including table lengths and the params model's own validators.
It is refused if that request would be refused.

The accumulator *stops* if

- adding a push fails,
- a record that a push's rows reference does not complete, or
- the binding fails to make the driver.

Later pushes and pins are refused with the reason, and the records of states it did not reach fail.

**Adding waits for readers.** The example uses the toy specs `VOLUME`, which sums counts over a table of runs, and `CUT`, which takes the value at `position.index` ([user-stories.md](user-stories.md)):

```python
volume = client.accumulator(Template.from_spec(VOLUME, runs=BLANK))
for run in runs:
    volume.push('runs', [Run(file=run)])   # returns once logged; the backend adds it later
    cut = client.submit(CUT, CutParams(volume=Volume(data=volume.ref('counts')), position=Position(index=0)))
                                          # pins this state; CUT reads it in place
    show(client.output(volume, 'counts', select={'q': 0}))
                                          # pins this state, waits for it, copies the slice
```

`push` and `submit` return once logged, and the backend adds pushes and runs readers in parallel.
Each reader still sees the state after the push before it in the loop, as if the calls ran one after another.
In Rust's terms, each reader of a state holds a shared borrow of the driver, and a push waits for a mutable borrow.

The rule is that push n+1 waits for the *readers* of state n:

- requests that read the outputs of the record of state n, from their submission until their workflow has returned, even if cancelled while it runs;
- pins of state n, until they have what they asked for (`client.output` until it has copied).

It has two reasons ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

- The driver only moves forward. After push n+1 is added, the outputs of state n cannot be computed again.
- The driver's `push` may change in place the arrays its `outputs()` returned (see What a binding provides).

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
iofq.push('sample', [Run(file=dataset(run=615))])
exported = client.submit(EXPORT, ExportParams(result=Result(data=iofq.ref('iofq'))))   # pins the state after this push
client.release(iofq)                                           # the push is added, and the export runs
```

The pushes up to the last pinned state are still added, and later pushes are never added.
The driver is dropped once the readers of its states are done.

## What a binding provides

A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec:

```python
single = PipelineBinding(IOFQ, pipeline,                 # from ess.spec.pipeline
                         params={'sample.file': Filename[SampleRun], 'reduction.bins': QBins, ...},
                         outputs={'iofq': BackgroundSubtractedIofQ})
multi = DriverBinding(IOFQ_MULTI, IofQDriver)            # from ess.spec
export = function_binding(EXPORT, export_csv)            # from ess.spec
client = local(proposal='p1', datasets=..., bind=[single, multi, export])
```

Each binding serves some of the client's actions:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline (`PipelineBinding`) | yes | yes | refused |
| a driver (`DriverBinding`) | yes | refused | yes |
| a function (`function_binding`) | yes | refused | refused |

A stage or an accumulator that its binding does not serve is refused when it is made.

**The protocol.** The backend calls a binding through three protocols of `ess.spec.binding`, one per client action:

```python
binding.compute(params)                            # Binding: a plain request
call = binding.make_stage(fixed, blanks)           # StageBinding: at a stage's first call
call({'reduction': {'bins': 50}})                  # the blanks' values; once per call through the stage
driver = binding.make_accumulator(fixed, blanks)   # AccumulatorBinding: when an accumulator opens
driver.push('sample', [{'file': run_611}])         # once per push
driver.outputs()                                   # once per state that is read
```

Values pass by parameter name, with data fields loaded: a group as a dict of its fields, a table as a list of such dicts.
`fixed` holds the fixed parameters, and `blanks` names the others.
A parameter in neither takes the spec's default; the backend always passes every parameter.
`compute`, `make_stage`, and the callable it returns may be called from several threads at once, and none modifies the values it is given.
The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.

**A pipeline.** `PipelineBinding` names the sciline key that each parameter, or each field of a group, sets, and the key that computes each output.
A stage computes what does not depend on the blanks once, through `sciline.Stage`, and each call computes the rest.
A plain request is a stage with no blanks, called once.
The author of the pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.

**A driver.** A *driver* is an object with `push(table, rows)` and `outputs()`, as in [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
The workflow package writes it, for one spec, and it holds everything an accumulator keeps.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
`DriverBinding` makes a driver from the fixed values that are not tables, and pushes the rows of each table in `fixed`, one push per table.
A plain request is an accumulator with no blanks, read once.
For the toy SANS reduction, a driver sums each run's numerator and denominator per table, and divides once:

```python
class IofQDriver:
    def __init__(self, fixed):                    # the fixed values that are not tables
        self._bins = fixed['reduction']['bins']
        self._sums = {}                           # per table: numerator and denominator

    def push(self, table, rows):
        for row in rows:
            parts = numerator_and_denominator(row['file'], self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else [a + b for a, b in zip(old, parts)]

    def outputs(self):                            # raises without a sample and a can run
        (sample_num, sample_den), (can_num, can_den) = self._sums['sample'], self._sums['can']
        return {'iofq': sample_num / sample_den - can_num / can_den}
```

A workflow that needs every row at once, such as `STITCH`, writes a driver that keeps its rows and computes over all of them at each read.
The driver promises, and the backend does not check:

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a row. A driver that adds in place copies the first contribution, so that adding never changes a value that another record returned.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be left out.
- A driver may refuse a state, such as one with no can row yet, by raising in `outputs()`. The read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.
- The spec's validators that read more than one value, such as the params model's own, change no value, since the driver got each value typed by its own field.

The backend calls `push` and `outputs` from one thread at a time, and pushes only once the readers of the state before are done.
Code that calls a driver itself keeps the same order:

```python
driver = binding.make_accumulator(fixed, blanks=('runs',))
for run in runs:
    driver.push('runs', [{'file': run}])
    counts = driver.outputs()['counts']   # may be the array the next push adds into
    show(counts['q', 0])                  # done with it before the next push
```

`DriverBinding` may call its function that makes a driver from several threads at once, since plain requests run in parallel.

**A function.** `function_binding` serves plain requests only, for drafts in notebooks and for workflows that hold nothing worth keeping between calls.
It has no stage, since a stage over a function would hold nothing.

## Controllers

The backend does not decide what to run.
A *controller* decides over time what to submit, push, and release.
It is ordinary code that uses the client, such as a notebook, an application's UI, or a server that runs automatic reduction.
Controllers never run in the backend.

**A loop over arrivals** waits for new datasets.
This one adds each run of a rotation scan to a volume as it arrives, and cuts through the volume after each run (story D7):

```python
volume = client.accumulator(Template.from_spec(VOLUME, runs=BLANK))
for run in islice(client.datasets.watch(Selector(scan='17')), 300):   # the scan's 300 runs
    volume.push('runs', [Run(file=run)])                    # added once the previous cut is copied
    show(client.output(volume, 'counts', select={'q': 0}))
total = client.freeze(volume)
```

The binding of `VOLUME` is a driver that sums, so the loop holds one volume and keeps one record, from `freeze`.

**A loop over records** reduces each dataset in a request of its own.
`client.output` waits for its record, so the loop shows each result before it takes the next dataset:

```python
for run in client.datasets.watch(Selector(role='sample')):
    record = client.submit(IOFQ, IofQParams(sample=Run(file=run)))
    show(client.output(record, 'iofq'))
```

## Batch and automatic reduction

[automatic-reduction.md](automatic-reduction.md) states these rules in full.
`apply` fills a template once per dataset.
A *lookup* fills further blanks, such as the latest can run before each sample run.
`apply` returns the requests keyed by the value of a metadata field (`member_field`):

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template.from_spec(IOFQ, sample=BLANK, can=BLANK), samples, client.datasets,
                 member_field='temperature', lookup=cans)
client.submit(requests, label='iofq', persist=True)
```

A *rule* is plain data: a name, a template, a selector, a label, and optionally a lookup, a series, and the outputs it persists.
The *trigger loop* is a controller that fills the template for each new dataset the selector matches, and submits it under the label, persisted.
With a *series*, one request takes all matching datasets so far with the same value of a metadata field.
An example is all angles of one sample at ESTIA, which the toy spec `STITCH` stitches:

```python
rule = Rule('reflectivity', Template.from_spec(STITCH, reference=Run(file=reference), runs=BLANK),
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

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs.

| Symmetry | The caller may change | Unchanged | Kept by | Given |
|---|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec with one row per table | the outputs | the workflow package | that its driver computes what its pipeline computes |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record; the `sciline.Stage` of a `PipelineBinding` for the outputs | a pure pipeline |
| arrival | rows pushed one at a time, or in groups ↔ the same rows in one plain request | the outputs of state n | the driver | the driver's promises |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver | the driver's promises |
| placement | where a request runs | the record and its outputs | the framework | |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries a workflow package keeps, by comparing two calls with the same inputs:

- `check_one_row(single, multi, ...)`: the multi-run binding with one row per table gives the outputs of the single-run binding. It is the only check against an independent reference.
- `check_caching(binding, fixed, calls)`: each call through a stage gives the plain request. For a `PipelineBinding` it mostly tests `ess.spec`.
- `check_arrival_and_order(make_driver, fixed, tables)`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push per table gives. It takes the function that makes the driver, so a package tests its driver without a spec. Both sides go through the driver, so it does not prove the outputs are right.

## Left to the system

Not visible in the code of notebooks, apps, or workflow packages:

- how history and outputs are stored, moved, and dropped
- how a run number or file becomes a dataset identity
- when and where a request runs, and where a stage or a driver lives
- the format of persisted outputs and the store's layout
- how the service notices a vanished client and sets the cap per client (scipp/essapps#27)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes needs a merge of two drivers. No requirement asks for it ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** Whether an accumulator offers `remove` depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Looks that compute.** `select=` only indexes an output. A projection, or a thick slice summed, would run a spec next to the value without making a record. The design of such looks waits for the plotting work. So does reading the latest state already added, without a pin.
5. **Accumulators on the service.** How the service holds a driver, bounds its memory, and drops it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), scipp/essapps#27).
6. **Persist at submission.** Should `persist=` at submission also keep the values for the client? To be decided when a story needs it.
7. **Values modified in place.** In the user's process, a notebook that modifies a value `client.output` returned changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.

# ESS data-reduction framework: the API

**Status: the in-process backend (`local()`) implements this API, including freeze, `select=`, and persist to a fake store. Not implemented: the service, `client.publish`, and software versions in provenance.**

This document shows by example what workflow authors, app authors, and notebooks write.
It states each rule once.
The [ADRs](adr/index.md) give the reasons, and [the requirements](../requirements/README.md) the goals.
[user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.
[prior-art/](prior-art/README.md) holds studies of other systems.
The first release combines no runs and tunes no parameters interactively.
It needs neither the section Stages and accumulators nor the loop over arrivals in Controllers.

## From a for loop

A notebook without the framework:

```python
curves = {}
for run in (60339, 60340, 60341):
    curves[run] = reduce_iofq(run, bins=100)
```

The same with the framework:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, params=IofQParams(reduction=Reduction(bins=100)),
                                 inputs={'sample': dataset(run=run)})
client.wait(records)
client.output(records[60339], 'iofq')
```

`client` connects to the framework (see Client and backend).
`IOFQ` is the spec of the I(Q) reduction, which names its params, inputs, and outputs, and `IofQParams` is its params model (see Specs and bindings).
`dataset(run=run)` names the raw data of a run (see Datasets), here as the input `sample`.
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
| spec | a workflow's name, version, params, inputs, and outputs |
| params, inputs, outputs | the three models of a spec: configuration, data, and results (see Specs and bindings) |
| parameter, input | a field of the params model, or of the inputs model |
| side | whether a name is a parameter, the *params side*, or an input, the *inputs side* |
| literal | a value that has a JSON form and that the client knows: a number, a string, an enum, a model of such values |
| data value | a value that the backend fetches, given by reference: a file, an array, a model holding one, or any other output of a record |
| group | a model of literals as the value of a parameter, such as the settings of one reduction step |
| binding | what the backend calls to compute a spec's requests (see What a binding provides) |
| request | a spec, its params, and its inputs |
| request phase, call phase | the *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |
| logged | recorded by the backend in the proposal's *history*. `submit` and the other calls that start work return once logged. |
| record | a request as the backend logged it. A record never changes, only its status. |
| reference | a value of a request's inputs that names an output of a record or an accumulator, or a dataset |
| fits | a reference fits an input when the type of the data it names can be the input's value (see References) |
| label, member | names under which records are found later |
| template | a spec with *fixed values* for some parameters and inputs and *blanks* for the others |
| fixed value | the value a template gives a parameter or an input, by side, as a request does |
| blank | a parameter, an input, or a table (named by its index) that a template leaves open |
| store, persist | the *store* keeps outputs outside the backend's memory. To *persist* an output is to have it written there. |
| dataset source | where a backend finds datasets |
| table, index, column, row | a *table* is a set of list fields of equal length, named by its *index* and pushed together; its other fields are *columns*, and one position is a *row* (see Combining runs: tables) |
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
A spec has three pydantic models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | configuration: literals only | the values | the same values |
| inputs | data only | a reference per value | the data the references name |
| outputs | results | (none) | the data the workflow returns |

Each parameter is a group, possibly optional, or a list of groups.
Each input is a data value, possibly optional, or a list of data values.
Each output is a data value, possibly optional, or a list or a dict of data values.

```python
class Reduction(BaseModel):                       # a group
    bins: int = 100

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)                # a NeXus file of an empty-can run
IofQ = NewType('IofQ', sc.DataArray)

class BeamCentre(BaseModel, arbitrary_types_allowed=True):   # a model that holds an array
    value: sc.Variable

class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)

class IofQInputs(BaseModel):
    sample: SampleFile
    can: CanFile | None = None                    # the empty-can run, if any
    beam_centre: BeamCentre | None = None

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, inputs=IofQInputs, outputs=IofQOutputs)
```

The models are ordinary pydantic models: the framework reads what it needs from the field types.
A file is `Path` or a `NewType` of it.
Each parameter, input, and output has a type of its own within the spec, such as `SampleFile` and `CanFile` for two NeXus files, since the type is the field's sciline key (see What a binding provides).
The module that defines a spec imports pydantic, `ess.spec`, and scipp for the types of data values, but no workflow package.
A workflow without data takes `NoInputs`, as one without configuration takes `NoParams`.
A request computes every output its spec declares.

A check of one value belongs to the value's type, such as a field validator of `BeamCentre`, and so does the JSON form of a value, which the framework stores and reads back.
`WorkflowSpec` refuses every pydantic decorator of the params and inputs models themselves but a model validator: field validators, serializers, and computed fields.
A check across parameters is a model validator of the params model, and a check across inputs one of the inputs model.
A check across params and inputs, such as a transmission mode that requires a transmission run, is the binding's, and fails when the request runs.

**Binding.** A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec.
The simplest binding is a function that takes the params and the inputs, instances of the spec's models, and returns an instance of its outputs model:

```python
def iofq(params: IofQParams, inputs: IofQInputs) -> IofQOutputs:
    ...                                     # params.reduction.bins; inputs.sample is a local path
    return IofQOutputs(iofq=result)

draft = function_binding(IOFQ, iofq)        # from ess.spec
```

The binding never sees a reference: the backend reads the data of each reference before it calls the binding.
A file input is a local path, and any other input the object, such as a `BeamCentre`.
The backend passes every parameter and input, with the defaults of the models filled in.
Defaults belong in the models only.
A binding must not modify its inputs or return an output that shares memory with an input.
`ess.spec.testing` checks this for values backed by numpy.
A sciline pipeline, or a driver that accumulates rows, is bound the same way (see What a binding provides).

`local` can also bind a spec to code defined in the notebook, such as `bind=[draft]`, since that code runs in the user's own process.
The service runs only installed workflows.

## Requests and records

A *request* is a spec, params, and inputs, given by keyword:

```python
result = client.compute(IOFQ, params=IofQParams(reduction=Reduction(bins=100)),
                        inputs={'sample': dataset(run=60339)})     # submit, then wait
client.output(result, 'iofq')            # raises if the record failed
client.output(result, 'iofq', select={'Q': slice(0, 10)})
```

`params` is an instance of the params model or a dict of its fields: `IofQParams(reduction=Reduction(bins=100))` and `{'reduction': {'bins': 100}}` give the same request.
It may be left out when every parameter has a default.
`inputs` is a dict by name, and each value is a reference (see References), or a list of references for a list field.

Submitting a request returns a *record*.
The record holds the request with every value filled in, defaults included, and the names of its outputs: `record.request.params`, in JSON's types, and `record.request.inputs`, as references.
It holds no output values.
Running the same request again makes a new record.
Its *status* is `pending`, then `completed`, `failed`, or `cancelled`.

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
`Request(IOFQ, params=..., inputs=...)` makes a request without submitting it, and `request.upstream()` lists the outputs of other records that it reads.
A caller without the workflow package gives `SpecId(name='sans-iofq', version=1)` in place of `IOFQ`.

**Validation.** In the request phase, at submission, the backend checks the params model, the names and the required inputs, that each value of the inputs is a reference or a list of references, readable and fitting its input (see References), and the table rules (see Combining runs: tables).
In the call phase, it checks each input by its type once, when it reads the data, and runs the inputs model's own validators.
A request that cannot run is refused at submission with a `SubmitError` naming the field at fault.
If one request of a call is refused, none is submitted.
A request that fails in the call phase makes a failed record.
Validation in the request phase never proves that a workflow will succeed.

## References

An input is given by *reference*: to an output of a record, `record.ref(name)`; to a dataset, `dataset(...)` (see Datasets); or to an output of an accumulator, `acc.ref(name)` (see Stages and accumulators).
A reference to a pending record is allowed, so a chain is submitted without waiting.
`BEAM_CENTRE` takes the input `direct_beam`, a file, and returns `centre: BeamCentre`:

```python
centre = client.submit(BEAM_CENTRE, inputs={'direct_beam': dataset(run=60330)})   # pending
result = client.submit(IOFQ, inputs={'sample': dataset(run=60339),
                                     'beam_centre': centre.ref('centre')})
```

The backend runs the second request once the first has completed.
A request never references another request of the same `submit` call.
A file is always given by reference: `dataset(path=...)` names a file by its path, and a string where a file is expected is refused.
Every value in the inputs of a request is a reference or a list of references, and params hold none.

A reference *fits* an input when the type of the data it names can be the input's value.
`| None` is removed on both sides.
Then the data fit when the output's type, with its `NewType`s removed one by one, reaches the input's type or a subclass of it, or when the input's type, with its `NewType`s removed, reaches the output's type or a superclass of it:

| Type of the output | Type of the input | Fits |
|---|---|---|
| `BeamCentre` | `BeamCentre \| None` | yes |
| `IofQ` | `sc.DataArray`, as an export takes it | yes |
| `sc.DataArray` | `Transmission`, a `NewType` of `sc.DataArray` | yes: a role narrows a general output |
| `IofQ` | `Transmission` | no: two `NewType`s of `sc.DataArray` |
| a dataset | `SampleFile` | yes: a dataset fits a file input |

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment.
A *member* names one part of a label, such as one sample or one temperature.
Both are optional and given at submission, not in the request.

```python
client.compute(IOFQ, params={'reduction': {'bins': 50}}, inputs={'sample': run},
               label='iofq', member='250K')
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # dict keys become the members
client.latest('iofq', member='250K')                  # the newest record of this member
client.records(label='iofq')                          # oldest first; without label=, every record of the proposal
{r.member: r for r in client.records(label='iofq')}   # the newest of each member: later records replace earlier ones
```

## Templates

A *template* is a spec, its fixed values, and its blanks, the names of the parameters and inputs left open.
Stages, accumulators, and automatic reduction are built from templates.
A blank is a whole parameter or input, never a field of a group.
`Template` takes the fixed values by side, as a request does, and all but the spec by keyword:

```python
template = Template(IOFQ, params={'reduction': Reduction(bins=100)}, blanks=('sample',))
```

`Template` validates each fixed value by its own field, and checks that each fixed reference is readable and fits its input.
A required parameter or input may be a blank.
A check that reads several fields is therefore a model validator, never a field validator reading `info.data`.
The params model's own validators run on each request made from the template.

A template can also be made from any request's values.
A name in `blanks` drops the value given for it:

```python
final = client.latest('iofq', member='250K')    # the final reduction of one sample
beamtime = Template(final.request.spec, params=final.request.params,
                    inputs=final.request.inputs, blanks=('sample',))   # its sample left open
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
centre = client.compute(BEAM_CENTRE, inputs={'direct_beam': dataset(run=60330)})
result = client.submit(IOFQ, inputs={'sample': dataset(run=60339),
                                     'beam_centre': centre.ref('centre')})
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
client.submit(IOFQ, inputs={'sample': dataset(run=60341)}, label='night', persist=('iofq',))   # only the store keeps iofq
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
A spec takes them as *tables*, next to the values that all runs share.
A table is a set of list fields of equal length, pushed together.
Its *index* is the field that names it, such as the sample runs.
Its other *columns* are attached to the index, such as a correction of each run.
Index and columns are parameters or inputs.
Each position is a *row*.
As in a pandas DataFrame, the index values of a table are distinct.

A *multi-run spec* is made from the *single-run spec* of the same reduction by naming the fields that become tables:

```python
IOFQ_MULTI = IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
```

Each field named becomes a `list` on its own side, and is the index of a table.
An index loses `| None`: `sample: SampleFile` becomes `list[SampleFile]` with at least one row, and `can: CanFile | None = None` becomes `list[CanFile]`, empty by default, so a can left out is a table without rows.
An index is `X`, or `X | None = None`, and `tabulate` refuses any other.
The validators and constraints of a field named check each row.
The other fields, `reduction` and `beam_centre`, and the outputs model are those of `IOFQ`.
A request that gives every row is a plain request:

```python
result = client.compute(IOFQ_MULTI, inputs={'sample': [dataset(run=611), dataset(run=612)],
                                             'can': [dataset(run=614)]})
```

**Columns.** A value that differs per run, such as a motor position, is in the run's file.
A value that the file lacks or has wrong is a column next to the run, so the request and its record hold it.
`SANS` is a spec like `IOFQ`, with the parameter `sample_geometry: SampleGeometry | None`, a group that replaces the geometry in the sample's file.
Its multi-run spec attaches that parameter to the sample runs, with `tables` as a dict from each index to its columns:

```python
SANS_MULTI = SANS.tabulate(name='sans-multi', version=1,
                           tables={'sample': ('sample_geometry',), 'can': ()})
```

An attached column keeps its type as written: `sample_geometry` becomes `list[SampleGeometry | None]`.
A column whose type allows `None` and that a request or a push leaves out is `None` in each row.
A column of any other type is given with its index every time.

A spec whose outputs differ from those of a single-run spec, such as `STITCH`, a stitch of all angles, or that has no single-run spec, such as `VOLUME`, which sums counts over runs, declares its tables itself: `WorkflowSpec(..., tables={'runs': ()})`.
Its required index carries `Field(min_length=1)`, as one that `tabulate` makes, such as `runs: list[RunFile] = Field(min_length=1)`, so the model, its schema, and the backend agree that the table has a row.
A list field makes a table only when `tables` names it; otherwise it is a value like any other.

The *table rules* are: the columns of a table have equal length, a table with a required index has a row, and the index values are distinct.

**Single-run and multi-run specs.** With one row in each table whose index is given, and none where it is left out, a request of `IOFQ_MULTI` is a request of `IOFQ`, and gives the same outputs (the one-row symmetry, see Symmetries).
The binding's driver handles structure below a row, such as detector banks, and decides what is summed ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

## Stages and accumulators

A plain request loads the run again for each value of `reduction.bins`.
Adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators keep what such work computes in memory between requests.
Each needs a binding that serves it (see What a binding provides).
A stage needs a binding that makes stages, such as a `PipelineBinding`, which computes the fixed part once.
An accumulator needs a binding that makes drivers, such as a `DriverBinding`, whose driver adds each run without reducing the earlier ones.
Making a stage or an accumulator over a binding that does not serve it is refused.
A client keeps a stage or an accumulator until it releases it or ends.

**Stage.** A *stage* is a template that the backend keeps for a client:

```python
tune = client.stage(Template(IOFQ, inputs={'sample': dataset(run=60339)}, blanks=('reduction',)))
for bins in (50, 100, 200):
    client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='iofq')   # the first call loads the run
```

A call through the stage takes the values of the blanks by side, as a request does.
It fills the blanks and makes the record of the plain request.
The first call has the binding make its stage from the fixed values.
The binding then computes what depends only on them, and later calls compute only the rest.
If staging fails, the stage *stops*.
The first call and the calls waiting to run fail, and later calls are refused, each with the reason.

**Accumulator.** An *accumulator* is a template whose blanks are tables, each named by its index.
The backend keeps it for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)):

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={'reduction': Reduction(bins=100)},
                                   blanks=('sample', 'can')))
iofq.push(sample=[dataset(run=611)])                       # one row
iofq.push(sample=[dataset(run=612), dataset(run=613)])     # two rows of one table
iofq.push(can=[dataset(run=614)])
client.output(iofq, 'iofq')                                # the output of state 3
```

A push names one table through its index, and gives the index and its columns by name, as lists of equal length.
Its rows enter one state.
Rows of different tables have nothing to do with each other, so a push never names two tables.
Values that belong to one run are columns of one table:

```python
sans = client.accumulator(Template(SANS_MULTI, blanks=('sample', 'can')))
sans.push(sample=[dataset(run=615)], sample_geometry=[SampleGeometry(thickness=0.2)])
sans.push(sample=[dataset(run=616)])                       # sample_geometry is None in this row
```

A push of no rows is refused, and so is a push of an attached column without its index.
A template fixes a whole table or leaves it blank, and a push into a table the template fixes is refused.
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
| `client.submit(EXPORT, inputs={'result': acc.ref('iofq')})` | a record of the `EXPORT` request. The submission also makes the record of state n, whose outputs the request reads without a copy. | from submission until the request's workflow has returned |
| `client.freeze(acc)` | the record of state n (see Freeze) | no push follows |
| `client.provenance(acc)` | the provenance of the plain request of state n, at once | no |

No client keeps the record of a state that a submission makes ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)), and `client.output` of it raises.
A push or a template may reference no accumulator ([ADR 0003](adr/0003-accumulators-add-in-place.md)).
Only the client that made an accumulator pins it.

**Validation.** Templates, stage calls, pushes, and states are checked in the two phases, as a plain request is (see Requests and records), each on what is new:

| What | Request phase | Call phase |
|---|---|---|
| a template | each fixed value by its own field; each fixed reference readable and fitting; the table rules on fixed tables | each fixed input by its type, once, when the stage or the driver is made |
| a stage call | the params model on the fixed and the given parameters; each given reference readable and fitting; the table rules on given tables | each given input by its type |
| a push | each literal by its column's type; each reference readable and fitting its column; the table rules on the table with the push | each data value by its column's type, when the push is added |
| a state, at a pin | the params model on the state's params; each required index has a row | (none) |

The inputs model's own validators run on plain requests only: a stage holds its fixed data in the form its binding made of them, and an accumulator's rows go to the driver one push at a time, so neither makes a whole inputs instance.
A data value that fails its check at a push stops the accumulator, as any failing push does.

A push refuses an index value that the table already holds, since pushing a run twice is usually a mistake, such as running a notebook cell again.
Index values are compared as the record holds them.
A dataset compares by the identity the dataset source gives it, so `dataset(run=611)` and `dataset(path=...)` of the same file are one value.
A reference to an output compares by record, output, and key, and a literal by equality.
Running again a cell that computes a record and pushes its output is not caught: each run pushes a reference to a new record.
A push checks this against the pushes logged, since a pin that refused the repeat would refuse every later state.
A plain request with a repeated index value is refused too, including one in a template.

The accumulator *stops* if

- adding a push fails,
- a record that a push references does not complete, or
- the binding fails to make the driver.

Later pushes and pins are refused with the reason, and the records of states it did not reach fail.

**Adding waits for readers.** The example uses the toy specs `VOLUME`, which sums counts over its table `runs`, and `CUT`, which takes the value at `position.index` of its input `volume` ([user-stories.md](user-stories.md)):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push(runs=[run])                 # returns once logged; the backend adds it later
    cut = client.submit(CUT, params={'position': Position(index=0)},
                        inputs={'volume': volume.ref('counts')})
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
iofq.push(sample=[dataset(run=615)])
exported = client.submit(EXPORT, inputs={'result': iofq.ref('iofq')})   # pins the state after this push
client.release(iofq)                                           # the push is added, and the export runs
```

The pushes up to the last pinned state are still added, and later pushes are never added.
The driver is dropped once the readers of its states are done.

## What a binding provides

A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec:

```python
single = PipelineBinding(IOFQ, make_pipeline)            # from ess.spec.pipeline
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
[plans/binding-jobs.md](plans/binding-jobs.md) describes bindings in more depth.

**The protocol.** The backend calls a binding through three protocols of `ess.spec.binding`, one per client action:

```python
binding.compute(params, inputs)                    # Binding: a plain request
call = binding.make_stage(fixed, blanks)           # StageBinding: at a stage's first call
call({'reduction': Reduction(bins=50)})            # the blanks' values; once per call through the stage
driver = binding.make_accumulator(fixed, blanks)   # AccumulatorBinding: when an accumulator opens
driver.push('sample', {'sample': [path_611]})      # once per push: one table, by its index
driver.outputs()                                   # once per state that is read
```

`compute` gets instances of the params and inputs models, and returns one of the outputs model.
A stage or an accumulator is made from some values only, so `make_stage` and `make_accumulator` get the values by name, params and inputs in one mapping:

```python
{'reduction': Reduction(bins=2), 'beam_centre': centre,               # a parameter as its group, an input as its data
 'sample': [path_611, path_612], 'sample_geometry': [None, geometry]} # each column of a table under its own name
```

`fixed` holds the fixed values, and `blanks` names the others, a table by its index.
A parameter or an input in neither takes the spec's default.
`compute`, `make_stage`, and the callable it returns may be called from several threads at once, and none modifies the values it is given.
The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.

**A pipeline.** `PipelineBinding` takes a function that makes the sciline pipeline from the fixed values, by name, so that the graph may depend on them.
The function does not get the blanks, so a field that changes the graph cannot be a blank.
Each parameter and input sets the sciline key of its type as written, without `Annotated` metadata, such as `Reduction`, `SampleFile`, or `CanFile | None`.
Each output is computed as the key of its type without `| None`, such as `IofQ`.
A table sets the key of each of its columns to its list.
A workflow with keys of its own names them with `keys=`:

```python
PipelineBinding(IOFQ, make_pipeline,
                keys={'sample': Filename[SampleRun], 'can': Filename[BackgroundRun],
                      'iofq': BackgroundSubtractedIofQ})
```

For a parameter or an input, `None` in `keys=` sets no key, for a field that `make_pipeline` uses itself.
A stage computes what does not depend on the blanks once, through `sciline.Stage`, and each call computes the rest.
A plain request is a stage with no blanks, called once.
The author of the pipeline promises that it is pure: it modifies no input, and returns the same results for the same inputs.
`binding.keys` and `binding.pipeline(fixed)` give the keys and the graph of a `PipelineBinding`, so that the driver of the multi-run spec reuses them ([roles/toy_sans.py](roles/toy_sans.py)).
A driver passes `pipeline` the fixed values it is made with: every value that is not a column of a table, defaults filled in.
So a field that is a column of the multi-run spec cannot shape the graph, as a blank cannot.

**A driver.** A *driver* is an object with `push(table, columns)` and `outputs()`, as in [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
The workflow package writes it, for one spec, and it holds everything an accumulator keeps.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
`DriverBinding` makes a driver from the fixed values that are not columns of a table, and pushes the rows of each table in `fixed`, one push per table.
A table without rows is not pushed, so a driver never gets a push of no rows.
A plain request is an accumulator with no blanks, read once.
For the toy SANS reduction, a driver sums each run's numerator and denominator per table, and divides once:

```python
class IofQDriver:
    def __init__(self, fixed):                    # the fixed values that are not columns of a table
        self._bins = fixed['reduction'].bins
        self._sums = {}                           # per table: numerator and denominator

    def push(self, table, columns):
        for file in columns[table]:               # the index: a file per row
            parts = numerator_and_denominator(file, self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else [a + b for a, b in zip(old, parts)]

    def outputs(self):                            # raises without a sample row
        sample_num, sample_den = self._sums['sample']
        result = sample_num / sample_den
        if 'can' in self._sums:
            can_num, can_den = self._sums['can']
            result = result - can_num / can_den
        return IofQOutputs(iofq=IofQ(result))
```

A workflow that needs every row at once, such as `STITCH`, writes a driver that keeps its rows and computes over all of them at each read.
The driver promises, and the backend does not check:

- `outputs()` gives the outputs of every row pushed so far, however the rows were grouped into pushes, and whatever the order of pushes into different tables.
- Within a table, the rows keep the order in which they were pushed.
- `push` does not modify a value it is given. A driver that adds in place copies the first contribution, so that adding never changes a value that another record returned.
- `outputs()` may share memory with the driver's state; the caller reads them before the next push.
- An output that the spec declares optional may be `None`.
- A driver may refuse a state, such as one with no sample row yet, by raising in `outputs()`. The read fails, and later pushes may make the state valid.
- A failing `push` stops the accumulator.

The backend calls `push` and `outputs` from one thread at a time, and pushes only once the readers of the state before are done.
Code that calls a driver itself keeps the same order:

```python
driver = binding.make_accumulator(fixed, blanks=('runs',))
for run in runs:
    driver.push('runs', {'runs': [run]})
    counts = driver.outputs().counts      # may be the array the next push adds into
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
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.datasets.watch(Selector(scan='17')), 300):   # the scan's 300 runs
    volume.push(runs=[run])                                 # added once the previous cut is copied
    show(client.output(volume, 'counts', select={'q': 0}))
total = client.freeze(volume)
```

The binding of `VOLUME` is a driver that sums, so the loop holds one volume and keeps one record, from `freeze`.

**A loop over records** reduces each dataset in a request of its own.
`client.output` waits for its record, so the loop shows each result before it takes the next dataset:

```python
for run in client.datasets.watch(Selector(role='sample')):
    record = client.submit(IOFQ, inputs={'sample': run})
    show(client.output(record, 'iofq'))
```

## Batch and automatic reduction

[automatic-reduction.md](automatic-reduction.md) states these rules in full.
`apply` fills a template once per dataset: the dataset itself is the value of the template's blank input.
A *lookup* fills further blank inputs, such as the latest can run before each sample run.
`apply` returns the requests keyed by the value of a metadata field (`member_field`):

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('sample', 'can')), samples, client.datasets,
                 member_field='temperature', lookup=cans)
client.submit(requests, label='iofq', persist=True)
```

A *rule* is plain data: a name, a template, a selector, a label, and optionally a lookup, a series, and the outputs it persists.
The *trigger loop* is a controller that fills the template for each new dataset the selector matches, and submits it under the label, persisted.
With a *series*, one request takes all matching datasets so far with the same value of a metadata field, as the rows of the template's blank table.
An example is all angles of one sample at ESTIA, which the toy spec `STITCH` stitches, over its table `runs`:

```python
rule = Rule('reflectivity', Template(STITCH, inputs={'reference': reference}, blanks=('runs',)),
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
| one-row | the single-run spec ↔ the multi-run spec, with one row in each table whose index is given and none where it is left out | the outputs | the workflow package | that its driver computes what its pipeline computes |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record; the `sciline.Stage` of a `PipelineBinding` for the outputs | a pure pipeline |
| arrival | rows pushed one at a time, or in groups ↔ the same rows in one plain request | the outputs of state n | the driver | the driver's promises |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver | the driver's promises |
| placement | where a request runs | the record and its outputs | the framework | |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries a workflow package keeps, by comparing two calls with the same inputs:

- `check_one_row(single, multi, params, inputs)`: the binding of the multi-run spec, given the one-row request made from `params` and `inputs`, gives the outputs of the binding of the single-run spec. It is the only check against an independent reference.
- `check_caching(binding, fixed, calls)`: each call through a stage gives the plain request. For a `PipelineBinding` it mostly tests `ess.spec`.
- `check_arrival_and_order(make_driver, fixed, tables)`, with `tables` as `{index: {column: list}}`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push per table gives. It takes the function that makes the driver, so a package tests its driver without a spec, and the fixed values: every value that is not a column of a table, defaults included, since the check does not fill them in. Both sides go through the driver, so it does not prove the outputs are right.

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

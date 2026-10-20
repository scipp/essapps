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
    curves[run] = reduce_iofq(run, can=60338, bins=100)
```

The same with the framework:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, params={'reduction': Reduction(bins=100),
                                               'sample': dataset(run=run),
                                               'can': dataset(run=60338)})
client.wait(records)
client.output(records[60339], 'iofq')
```

`client` connects to the framework (see Client and backend).
`IOFQ` is the spec of the I(Q) reduction, which names its params and outputs (see Specs and bindings).
`dataset(run=run)` names the raw data of a run (see Datasets), here as the parameter `sample`, and `dataset(run=60338)` the empty-can run that each reduction subtracts.
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
| spec | a workflow's name, version, params, and outputs |
| params, outputs | the two models of a spec: everything the workflow takes, and its results (see Specs and bindings) |
| parameter | a field of the params model |
| inline value | the value itself in the request, such as `Reduction(bins=100)`; the record holds its JSON form. A request gives each value in one of two forms: inline, or by reference. A type has a *JSON form* when it is not a file, holds no file, and pydantic makes a JSON schema of it with the params model's config. |
| reference | names data that exist elsewhere: a dataset, an output of a record, or an output of an accumulator |
| group | a pydantic model, such as `Reduction`, the settings of one reduction step: the usual type of a parameter given inline. A group that holds a file, such as `Sample`, a run and the values given next to it, is given *field by field* (see Specs and bindings). |
| data value | a value of a type without a JSON form: a file, or an object such as a scipp array. It is given by reference only. |
| fits | a reference fits a parameter when the type of the data it names can be the parameter's value (see References) |
| binding | what the backend calls to compute a spec's requests (see What a binding provides) |
| request | a spec and its params, each value given inline or by reference |
| request phase, call phase | the *request phase* is what the client and the backend do with a request before any data is read: validate, log, record, pin. The *call phase* starts when the backend reads data for the binding. |
| logged | recorded by the backend in the proposal's *history*. `submit` and the other calls that start work return once logged. |
| record | a request as the backend logged it. A record never changes, only its status. |
| label, member | names under which records are found later |
| template | a spec with *fixed values* for some parameters and *blanks* for the others |
| fixed value | the value a template gives a parameter, as a request does |
| blank | a parameter that a template leaves open, a table among them |
| store, persist | the *store* keeps outputs outside the backend's memory. To *persist* an output is to have it written there. |
| dataset source | where a backend finds datasets |
| table, row | a *table* is a list parameter whose *rows* an accumulator takes a push at a time (see Combining runs: tables) |
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
A spec has two pydantic models:

| Model | Holds | In a request | When the workflow runs |
|---|---|---|---|
| params | everything the workflow takes: settings, files, data | each value given inline or by reference | the values, and the data the references name |
| outputs | results | (none) | the data the workflow returns |

```python
class Reduction(BaseModel):                       # a group
    bins: int = 100

class BeamCentre(BaseModel):                      # a group: inline, or an output of a first reduction
    x: float
    y: float

SampleFile = NewType('SampleFile', Path)          # a NeXus file of a sample run
CanFile = NewType('CanFile', Path)                # a NeXus file of an empty-can run
Transmission = NewType('Transmission', sc.DataArray)
IofQ = NewType('IofQ', sc.DataArray)

class Sample(BaseModel):                          # a group that holds a file
    run: SampleFile
    transmission: Transmission | None = None

class IofQSettings(BaseModel):                    # the parameters that all runs share
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre | None = None

class IofQParams(IofQSettings):
    sample: Sample
    can: CanFile                                  # the empty-can run

class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
    iofq: IofQ

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

Whether a value is given inline or by reference is the request's choice, per value.
The parameter's type decides which of the two it accepts, with `| None` removed and, for a list parameter, its element type taken:

| A parameter's type | Example | Inline | By reference |
|---|---|---|---|
| a file: `Path`, pydantic's `FilePath` or `DirectoryPath`, or a `NewType` of one | `SampleFile` | no | to a dataset |
| has a JSON form | `Reduction`, `BeamCentre` | yes | to an output that fits |
| a group that holds a file | `Sample` | field by field | to a dataset, for its one required file |
| anything else | `Transmission` | no | to an output that fits |

A file is given by reference to a dataset only, so that the dataset source checks access and the record holds the dataset's identity.
A type is a file when pydantic's JSON schema of it is a string with a path format: `"format": "path"` for `Path` and a `NewType` of it, `"file-path"` for `FilePath`, `"directory-path"` for `DirectoryPath`.
The access check on the service depends on it, so an author types each file as one of these.
A `NewType` of `str` used for a file is a string: it is given inline, and no dataset source checks it.
A group that holds a file, such as `Sample`, is given *field by field*: each of its fields inline or by reference, as a parameter is, so its file comes from a dataset.
A reference to a dataset stands for the whole group: its one required file, and its other fields at their defaults.
`t611` is the record of a transmission reduction of run 611, whose output `transmission` is a `Transmission`:

```python
{'sample': {'run': dataset(run=611), 'transmission': t611.ref('transmission')}}
{'sample': dataset(run=611)}                      # the run, and the transmission None
```

The module that defines a spec imports pydantic, `ess.spec`, and scipp for the types of data values, but no workflow package.
A workflow that takes nothing has the params model `NoParams`.
A request computes every output its spec declares.

**Rules.** The models are ordinary pydantic models: the framework reads what it needs from the field types.
A value *round-trips* when its JSON form reads back as the value.
`WorkflowSpec` checks these rules when it is made:

- Each parameter is a type, possibly optional, or a list of one.
- Each output is a type, possibly optional, or a list or a dict of one.
- A parameter holds a file only as a file, possibly optional, a list of files, or a group that holds a file as a field. A file anywhere else, such as in a tuple, a `TypedDict`, a dataclass, or a type alias `type Files = list[Path]`, is refused, since no reference to a dataset stands for it.
- A group holds a file only as one of its fields, typed as a file, possibly optional. Such a group follows the rules on defaults and on the params model below. A field of it that is a list or a dict has a JSON form, since no reference stands for a list.
- A default is a value, never a reference: of a type with a JSON form, a value that round-trips and whose JSON form is neither an `@ref` object nor a list holding one; of any other type, `None` or `[]`.
- A table has no default (see Combining runs: tables).
- The params model has no field validators, serializers, computed fields, or aliases, and no `default_factory` that takes the other values. A list parameter carries only length constraints, such as `Field(min_length=2)`. The params model may have model validators, but the framework never calls them.

Each parameter is validated alone, in every phase, since a template, a stage call, or a push gives some values only.
Like the params model, a group given field by field is never validated whole.
A check of one value belongs to the value's type, such as a field validator of a group, and so does the JSON form of a value, which the framework stores and reads back.
A check across parameters, such as a transmission mode that requires a transmission run, is the binding's or the driver's, and fails when the request runs.
A binding may run the model validators, `IofQParams.model_validate(dict(params))`.

**Defaults.** A default never changes which quantity an output is.
`reduction` has a default, a setting, and so has the transmission of a `Sample`, `None`, which takes the one in the run's file: the output is I(Q) either way.
The can run has none.
I(Q) without the can subtracted is another quantity, so it is another spec, not `IOFQ` with `can=None`.
A request or a template that leaves out the can is then refused, instead of computing the other quantity.

**Binding.** A binding is what the backend calls to compute a spec's requests.
The workflow author makes it from a pipeline, a driver, or a function, each for one spec.
The simplest binding is a function that takes an instance of the spec's params model and returns an instance of its outputs model:

```python
def iofq(params: IofQParams) -> IofQOutputs:
    ...                                     # params.reduction.bins; params.sample.run is a local path
    return IofQOutputs(iofq=result)

draft = function_binding(IOFQ, iofq)        # from ess.spec
```

The binding never sees a reference: the backend reads the data of each reference before it calls the binding.
Each value is its type, whichever form the request gave it in: a group as the model, a file as a local path, any other data value as the object, such as a `Transmission`.
The backend passes every parameter, with the defaults of the model filled in.
Defaults belong in the model only.
A binding must not modify the values it gets or return an output that shares memory with one of them.
`ess.spec.testing` checks this for values backed by numpy.
A sciline pipeline, or a driver that accumulates rows, is bound the same way (see What a binding provides).

`local` can also bind a spec to code defined in the notebook, such as `bind=[draft]`, since that code runs in the user's own process.
The service runs only installed workflows.

## Requests and records

A *request* is a spec and its params, given by keyword:

```python
result = client.compute(IOFQ, params={'reduction': Reduction(bins=100),
                                      'sample': dataset(run=60339),
                                      'can': dataset(run=60338)})      # submit, then wait
client.output(result, 'iofq')            # raises RecordFailed if the record failed
client.output(result, 'iofq', select={'Q': slice(0, 10)})
```

`params` is a dict by name: each value given inline or as a reference, or a list of these for a list parameter.
An inline value is the value, such as `Reduction(bins=100)`, or its JSON form, `{'bins': 100}`: both give the same request.
`params` may also be an instance of the params model, if every value is given inline; a spec with a file takes a dict.
It may be left out when every parameter has a default.

Submitting a request returns a *record*.
The record holds the request with every value filled in, defaults included, and the names of its outputs: `record.request.params` holds the JSON form of each inline value, and each reference.
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
`Request(IOFQ, params=...)` makes a request without submitting it, and `request.upstream()` lists the outputs of other records that it reads.
A caller without the workflow package gives `SpecId(name='sans-iofq', version=1)` in place of `IOFQ`.

**Validation.** In the request phase, at submission, the backend checks the names and the required parameters, each inline value by its field from its JSON form, each reference readable and fitting its field (see References), and the table rules (see Combining runs: tables).
An inline value must round-trip, since the log holds its JSON form: a number that is not finite does not.
In the call phase, it checks each value it reads by its field, once, and makes the params instance from the validated values.
A request that cannot run is refused at submission with a `SubmitError`.
Its `errors` list each reason as a `FieldError`, as pydantic's `ValidationError.errors()` does: the place of the field at fault, `loc`, such as `('sample', 2, 'transmission')`, the reason, `msg`, and for a call with several requests the request's key or index, `entry`.
If one request of a call is refused, none is submitted.
A request that fails in the call phase makes a failed record.
Validation in the request phase never proves that a workflow will succeed.

## References

A value is given by *reference*: to an output of a record, `record.ref(name)`; to a dataset, `dataset(...)` (see Datasets); or to an output of an accumulator, `acc.ref(name)` (see Stages and accumulators).
A reference to a pending record is allowed, so a chain is submitted without waiting.
`BEAM_CENTRE` takes the parameter `direct_beam`, a file, and returns `centre: BeamCentre`.
The same beam centre is typed in by one user and computed by another:

```python
result = client.submit(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338),
                                     'beam_centre': BeamCentre(x=0.012, y=-0.003)})   # inline
centre = client.submit(BEAM_CENTRE, params={'direct_beam': dataset(run=60330)})       # pending
result = client.submit(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338),
                                     'beam_centre': centre.ref('centre')})            # a reference
```

The backend runs the last request once the one before has completed.
A request never references another request of the same `submit` call.
A file is always given by reference: `dataset(path=...)` names a file by its path, and a string where a file is expected is refused.
`record.ref(output, key)` names one element of a list or dict output: by its index for a list, `key='2'`, and by its key for a dict.
A key that the output lacks fails in the call phase.

A reference stands for a value, for one row of a list parameter, or for one field of a group given field by field, such as `sample.run`: never for a whole list, and never for part of any other group.
Its JSON form is an object with the key `@ref`, which names its kind:

```json
{"@ref": "dataset", "dataset": "run:loki/60339"}
{"@ref": "output", "record": "r17", "output": "centre"}
{"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
```

The framework looks for references at three places only: the top of each value, the top of each element of a value that is a JSON array, and each field of an object at one of these two places, such as `sample.run` or `sample[2].transmission`.
At these places it refuses an `@ref` object that the spec does not allow as a reference: an element of a parameter that is not a list, such as a `tuple` of groups, whose JSON is an array; a field of a group given inline whole, such as `reduction.bins`; or the JSON form of an inline value, such as a group with extra fields that holds the key `@ref`.
Deeper inside a value, `@ref` is plain data.
So the framework finds the references of a request without the spec.

A reference *fits* a parameter when the type of the data it names can be the parameter's value.
`| None` is removed on both sides.
A reference with a key names one element, of the output's element type, and a list parameter takes each reference by its element type.
Then the data fit when the output's type, with its `NewType`s removed one by one, reaches the parameter's type or a subclass of it, or when the parameter's type, with its `NewType`s removed, reaches the output's type or a superclass of it:

| Type of the output | Type of the parameter | Fits |
|---|---|---|
| `BeamCentre` | `BeamCentre \| None` | yes |
| `list[TimeRange]`, with a key | `TimeRange`, a row of `time_range: list[TimeRange]` | yes |
| `IofQ` | `sc.DataArray`, as an export takes it | yes |
| `sc.DataArray` | `Transmission`, a `NewType` of `sc.DataArray` | yes: a role narrows a general output |
| `IofQ` | `Transmission` | no: two `NewType`s of `sc.DataArray` |
| a dataset | `SampleFile`, also as the field `run` of a `Sample` | yes: a dataset fits a file only |

A generic alias, such as a sciline scope `SampleSurfaceNormal[SampleRun]`, fits only itself.

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment.
A *member* names one part of a label, such as one sample or one temperature.
Both are optional and given at submission, not in the request.

```python
client.compute(IOFQ, params={'reduction': {'bins': 50}, 'sample': run, 'can': can},
               label='iofq', member='250K')
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # dict keys become the members
client.latest('iofq', member='250K')                  # the newest record of this member
client.records(label='iofq')                          # oldest first; without label=, every record of the proposal
{r.member: r for r in client.records(label='iofq')}   # the newest of each member: later records replace earlier ones
```

## Templates

A *template* is a spec, its fixed values, and its blanks, the names of the parameters left open.
Stages, accumulators, and automatic reduction are built from templates.
A blank is a whole parameter, never a field of a group.
`Template` takes the fixed values as a request does, and all but the spec by keyword:

```python
template = Template(IOFQ, params={'reduction': Reduction(bins=100), 'can': dataset(run=60338)},
                    blanks=('sample',))
```

`Template` validates each fixed inline value by its own field, and checks that each fixed reference is readable and fits its field.
A required parameter may be a blank.

A template can also be made from any request's values.
A name in `blanks` drops the value given for it:

```python
final = client.latest('iofq', member='250K')    # the final reduction of one sample
beamtime = Template(final.request.spec, params=final.request.params,
                    blanks=('sample',))         # its sample left open
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
`client.output` reads only the same values, and raises `NotReadable` for any other.
So another client, even one in the same process, reads a value only once it is persisted.

```python
centre = client.compute(BEAM_CENTRE, params={'direct_beam': dataset(run=60330)})
result = client.submit(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338),
                                     'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request keeps the centre until it has run
client.output(result, 'iofq')       # allowed: this client asked for the record
client.output(centre, 'centre')     # raises NotReadable: this client released it
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
client.submit(IOFQ, params={'sample': dataset(run=60341), 'can': dataset(run=60338)},
              label='night', persist=('iofq',))             # only the store keeps iofq
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
samples = client.datasets.list(Selector(role='sample'))       # dataset references, usable as values
for run in client.datasets.watch(Selector(scan='17')): ...   # existing ones first, then new ones
client.datasets.metadata(run)['sample']                       # the source's current values
```

## Combining runs: tables

Many reductions combine several runs, such as the sample and can runs of SANS.
A spec takes them as *tables*, next to the values that all runs share.
A table is a list parameter whose *rows* an accumulator takes a push at a time, such as the sample runs.
Each row is given inline or by reference, as its type allows.
The rows of a table are distinct.

A *multi-run spec* combines the runs of the *single-run spec* of the same reduction.
Its params model shares the other parameters, `reduction` and `beam_centre`, with `IofQParams` through the base model `IofQSettings`, and each table is a list of the single-run type:

```python
class IofQMultiParams(IofQSettings):
    sample: list[Sample] = Field(min_length=1)
    can: list[CanFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
```

`tables` names the tables.
A table has no default, so a request gives every table, `[]` for one without rows; `Field(min_length=1)` states that it needs a row.
The validators and constraints of the element type check each row.
A request that gives every row is a plain request:

```python
result = client.compute(IOFQ_MULTI, params={'sample': [dataset(run=611), dataset(run=612)],
                                             'can': [dataset(run=614)]})
```

**A row of several values.** A value that differs per run, such as a motor position, is in the run's file.
A value that the file lacks or has wrong is a field of the row's group, next to the run, so the request and its record hold it.
Each field of a row is given in its own form.
Here the transmission of run 611 is the output of `t611`, and run 612 takes the default, `None`:

```python
client.compute(IOFQ_MULTI, params={'sample': [{'run': dataset(run=611), 'transmission': t611.ref('transmission')},
                                              dataset(run=612)],
                                   'can': [dataset(run=614)]})
```

A multi-run spec may have outputs of its own, such as `STITCH`, a stitch of all angles, or no single-run spec, such as `VOLUME`, which sums counts over runs.
It is written the same way: `runs: list[RunFile] = Field(min_length=1)`, with `WorkflowSpec(..., tables=('runs',))`, so the model, its schema, and the backend agree that the table has a row.
A list parameter makes a table only when `tables` names it; otherwise it is a value like any other.

The *table rules* are: the number of rows of a table meets its length constraints, such as a table that needs a row, and the rows of a table are distinct.

**Single-run and multi-run specs.** With one row in each table, a request of `IOFQ_MULTI` is a request of `IOFQ`, and gives the same outputs (the one-row symmetry, see Symmetries).
The specs agree since `IofQMultiParams` shares the other parameters of `IofQParams` and each of its tables is a list of the single-run type; `check_one_row` checks it.
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
tune = client.stage(Template(IOFQ, params={'sample': dataset(run=60339), 'can': dataset(run=60338)},
                             blanks=('reduction',)))
for bins in (50, 100, 200):
    client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='iofq')   # the first call loads the run
```

A call through the stage takes the values of the blanks, as a request does.
It fills the blanks and makes the record of the plain request.
The first call has the binding make its stage from the fixed values.
The binding then computes what depends only on them, and later calls compute only the rest.
A blank is a whole parameter, and a group is one key of the pipeline (see What a binding provides).
So a stage that tunes the transmission of a run has `sample` as its blank, and loads the run again at each call.
If staging fails, the stage *stops*.
The first call and the calls waiting to run fail, and later calls are refused, each with the reason.

**Accumulator.** An *accumulator* is a template whose blanks are tables.
The backend keeps it for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)):

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={'reduction': Reduction(bins=100)},
                                   blanks=('sample', 'can')))
iofq.push(sample=[dataset(run=611)])                       # one row
iofq.push(sample=[dataset(run=612), dataset(run=613)])     # two rows of one table
iofq.push(sample=[dataset(run=617)], can=[dataset(run=614)])   # rows of two tables
client.output(iofq, 'iofq')                                # the output of state 3
```

A push gives rows of one or more tables, by the name of each, as a list.
Its rows enter one state, so states 1 and 2, which have no can row, are never read here.
The tables of a push stand side by side, as in a request: rows of different tables have nothing to do with each other, and the first sample row is not paired with the first can row.
Values that belong to one run are fields of one row, such as the transmission of run 615, the output of the record `t615`:

```python
iofq.push(sample=[{'run': dataset(run=615), 'transmission': t615.ref('transmission')},
                  dataset(run=616)])                       # 616 with the transmission None
```

A push of no rows is refused.
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
| `client.submit(EXPORT, params={'result': acc.ref('iofq')})` | a record of the `EXPORT` request. The submission also makes the record of state n, whose outputs the request reads without a copy. | from submission until the request's workflow has returned |
| `client.freeze(acc)` | the record of state n (see Freeze) | no push follows |
| `client.provenance(acc)` | the provenance of the plain request of state n, at once | no |

No client keeps the record of a state that a submission makes ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)), and `client.output` of it raises.
A push or a template may reference no accumulator ([ADR 0003](adr/0003-accumulators-add-in-place.md)).
Only the client that made an accumulator pins it.

**Validation.** Templates, stage calls, pushes, and states are checked in the two phases, as a plain request is (see Requests and records), each on what is new:

| What | Request phase | Call phase |
|---|---|---|
| a template | each fixed inline value by its field; each fixed reference readable and fitting; the table rules on fixed tables | each fixed value read, once, when the stage or the driver is made; the rows of fixed tables read distinct |
| a stage call | the same, for the values given; the table rules on given tables | each given value read; the rows of given tables read distinct |
| a push | each inline value by the table's element type, or by its field in a row's group; each reference readable and fitting; the table rules on the table with the push | each row read and checked by the table's element type; the rows read distinct from those read before, before the driver gets them |
| a state, at a pin | the number of rows of each table, by its length constraints | (none) |

A value that fails its check at a push stops the accumulator, as any failing push does.

**Distinct rows.** A push refuses a row that the table already holds, since pushing a run twice is usually a mistake, such as running a notebook cell again.
Rows are compared in two ways.
In the request phase, they are compared whole, as the record holds them: an inline value by its JSON form, a dataset by the identity the dataset source gives it, a reference to an output by record, output, and key, and a group given field by field by each of its fields so.
So `dataset(run=611)` and `dataset(path=...)` of the same file are one row, and the same run with another transmission is another row.
A push checks this against the pushes logged, since a pin that refused the repeat would refuse every later state.
A plain request with a repeated row is refused too, including one in a template.

In the call phase, for a table whose element type has a JSON form, rows are also compared by the JSON form of each row read.
The backend keeps the rows read so far for each blank table, and compares each push's rows with them before the driver gets the rows.
So an inline value and a reference to an output whose value is equal are one row, and a time slice is never summed twice.
`SLICES` is a spec like `IOFQ`, with the table `time_range: list[TimeRange] = Field(min_length=1)`:

```python
slicer = client.compute(FIND_SLICES, params={'log': dataset(run=611)})   # outputs slices: list[TimeRange]
n = len(client.output(slicer, 'slices'))
acc = client.accumulator(Template(SLICES, params={'sample': dataset(run=611)}, blanks=('time_range',)))
acc.push(time_range=[slicer.ref('slices', key=str(i)) for i in range(n)])
acc.push(time_range=[TimeRange(start=0, stop=60)])    # equal to slice 0: the push fails
```

A repeat found in the call phase fails the push, which stops the accumulator.
The client reads how many slices there are, since a reference names one row.
Running again a cell that computes a record and pushes its output pushes a reference to a new record.
Only the call phase catches that repeat, and only for a table whose element type has a JSON form.

The accumulator *stops* if

- adding a push fails,
- a record that a push references does not complete, or
- the binding fails to make the driver.

Later pushes and pins are refused with the reason, and the records of states it did not reach fail.

**Adding waits for readers.** The example uses the toy specs `VOLUME`, which sums counts over its table `runs`, and `CUT`, which takes the value at `position.index` of its parameter `volume` ([user-stories.md](user-stories.md)):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push(runs=[run])                 # returns once logged; the backend adds it later
    cut = client.submit(CUT, params={'position': Position(index=0),
                                     'volume': volume.ref('counts')})
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
exported = client.submit(EXPORT, params={'result': iofq.ref('iofq')})   # pins the state after this push
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
binding.compute(params)                            # Binding: a plain request
call = binding.make_stage(fixed, blanks)           # StageBinding: at a stage's first call
call({'reduction': Reduction(bins=50)})            # the blanks' values; once per call through the stage
driver = binding.make_accumulator(fixed, blanks)   # AccumulatorBinding: when an accumulator opens
driver.push('sample', [Sample(run=path_611)])      # once per table of a push: its rows
driver.outputs()                                   # once per state that is read
```

`compute` gets an instance of the params model, and returns one of the outputs model.
A stage or an accumulator is made from some values only, so `make_stage` and `make_accumulator` get the values by name, each as its type, whichever form the request gave it in:

```python
{'reduction': Reduction(bins=2), 'beam_centre': BeamCentre(x=0.012, y=-0.003),  # a group as the model
 'sample': [Sample(run=path_611), Sample(run=path_612, transmission=t612)]}      # a table as the list of its rows
```

`fixed` holds the fixed values, and `blanks` names the others, a table among them.
A parameter in neither takes the spec's default.
`compute`, `make_stage`, and the callable it returns may be called from several threads at once, and none modifies the values it is given.
The callable may return the same object in several calls, so records made through one stage share what does not depend on the blanks.

**A pipeline.** `PipelineBinding` takes a function that makes the sciline pipeline from the fixed values, by name, so that the graph may depend on them.
The function does not get the blanks, so a field that changes the graph cannot be a blank.
Each parameter sets the sciline key of its type as written, without `Annotated` metadata, such as `Reduction`, `Sample`, or `BeamCentre | None`.
Each output is computed as the key of its type without `| None`, such as `IofQ`.
A table sets the key of its type, a list, to its rows.
A group is one key, so a provider takes it apart, such as one that reads the file `sample.run`.
A workflow with keys of its own names them with `keys=`:

```python
PipelineBinding(IOFQ, make_pipeline,
                keys={'can': Filename[BackgroundRun], 'iofq': BackgroundSubtractedIofQ})
```

Two parameters may share a type, such as two axes of a Q map, if `keys=` names a key for one; or the spec gives each a `NewType`.
For a parameter, `None` in `keys=` sets no key, for a field that `make_pipeline` uses itself.
A stage computes what does not depend on the blanks once, through `sciline.Stage`, and each call computes the rest.
A plain request is a stage with no blanks, called once.
The author of the pipeline promises that it is pure: it modifies no value it is given, and returns the same results for the same values.
`binding.keys` and `binding.pipeline(fixed)` give the keys and the graph of a `PipelineBinding`, so that the driver of the multi-run spec reuses them ([roles/toy_sans.py](roles/toy_sans.py)).
A driver passes `pipeline` the fixed values it is made with: every value that is not a table, defaults filled in.
So a parameter that is a table of the multi-run spec cannot shape the graph, as a blank cannot.

**A driver.** A *driver* is an object with `push(table, rows)` and `outputs()`, as in [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
The workflow package writes it, for one spec, and it holds everything an accumulator keeps.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
`DriverBinding` makes a driver from the fixed values that are not tables, and pushes the rows of each table in `fixed`, one push per table.
A table without rows is not pushed, so a driver never gets a push of no rows.
A plain request is an accumulator with no blanks, read once.
For the toy SANS reduction, a driver sums each run's numerator and denominator per table, and divides once:

```python
class IofQDriver:
    def __init__(self, fixed):                    # the fixed values that are not tables
        self._bins = fixed['reduction'].bins
        self._sums = {}                           # per table: numerator and denominator

    def push(self, table, rows):
        for row in rows:                          # a Sample, or a can run's path
            parts = numerator_and_denominator(row, self._bins)
            old = self._sums.get(table)
            self._sums[table] = parts if old is None else [a + b for a, b in zip(old, parts)]

    def outputs(self):                            # a state read has a sample row and a can row
        sample_num, sample_den = self._sums['sample']
        can_num, can_den = self._sums['can']
        return IofQOutputs(iofq=IofQ(sample_num / sample_den - can_num / can_den))
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
    driver.push('runs', [run])
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
    record = client.submit(IOFQ, params={'sample': run, 'can': dataset(run=60338)})
    show(client.output(record, 'iofq'))
```

## Batch and automatic reduction

[automatic-reduction.md](automatic-reduction.md) states these rules in full.
`apply` fills a template once per dataset: the dataset itself is the value of the template's blank file parameter, or of its blank group that holds one required file, such as `sample`.
A *lookup* fills further blank file parameters, such as the latest can run before each sample run.
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

Provenance lists the record's request, every record that supplied a value by reference (directly or through other records), the datasets they used, and the software versions.
If a value is an output of an accumulator's state, the record listed is the record of that state.
Provenance stops at datasets.

Publishing puts an output in a catalogue with its provenance.
The catalogue entry stays after the proposal's history is dropped.
`publish` reads the value as `client.output` does, so it needs no persist request.

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs.

| Symmetry | The caller may change | Unchanged | Kept by | Given |
|---|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec, with one row in each table | the outputs | the workflow package | that its driver computes what its pipeline computes |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record; the `sciline.Stage` of a `PipelineBinding` for the outputs | a pure pipeline |
| arrival | rows pushed one at a time, or in groups ↔ the same rows in one plain request | the outputs of state n | the driver | the driver's promises |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver | the driver's promises |
| placement | where a request runs | the record and its outputs | the framework | |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries a workflow package keeps, by comparing two calls with the same values:

- `check_one_row(single, multi, params)`: the binding of the multi-run spec, given the one-row request made from `params`, an instance of the single-run spec's params model, gives the outputs of the binding of the single-run spec. It is the only check against an independent reference.
- `check_caching(binding, fixed, calls)`: each call through a stage gives the plain request. For a `PipelineBinding` it mostly tests `ess.spec`.
- `check_arrival_and_order(make_driver, fixed, tables)`, with `tables` as `{table: rows}`: pushing rows one at a time, in groups, in other orders, and reading between pushes gives what one push per table gives. It takes the function that makes the driver, so a package tests its driver without a spec, and the fixed values: every value that is not a table, defaults included, since the check does not fill them in. Both sides go through the driver, so it does not prove the outputs are right.

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

# ESS data-reduction framework: the API

**Status: the design of the API. Implementation starts with the core.**

[../requirements/](../requirements/README.md) states the goals and what we know, assume, and do not know about the problem. [user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it. The first release that the requirements describe combines no runs and tunes no parameters interactively, so it uses neither stages nor accumulators.

This document describes the API we want: what workflow authors, app authors, and notebooks write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in [system.md](system.md), and the system may change them without changing any code shown here.
[adr/](adr/index.md) records the decisions behind both.

The first sections cover what most notebooks need: submitting requests, reading their results, and chaining them.
Later sections add how runs are combined, what saves computation (stages and accumulators), what loops over many datasets look like (drivers), and two sub-designs that build on the core: batch and automatic reduction, and provenance and publication.

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
`IOFQ` is the spec of the I(Q) reduction: it states which parameters the reduction takes and which outputs it returns.
`dataset(run=run)` names the raw data of a run (see Datasets).
`client.submit` returns at once with a *record*; the three reductions run in parallel, possibly on another machine.
`client.wait` blocks until they have finished, and `client.output` reads an output by name.

What the framework adds to the plain loop:

- Each call is kept as a record and can be found later: in the user's process (`local()`) by that process, and on the service by other notebooks and programs too.
- Calls can run elsewhere and in parallel.
- Every result can answer where it came from: which spec, which parameter values, which datasets, which software versions.

## Terms

The table names who writes each thing and who makes one at run time, with these roles:

- *framework*: this package, and `ess.reduce.spec`.
- *workflow author*: writes specs and bindings in a workflow package, such as an ess instrument package.
- *app author*: writes an application on top of the client, such as a batch form, a desktop or web UI, or a driving server.
- *notebook*: a scientist's notebook that uses the client directly.
- *DMSC*: deploys the service and its dataset source, and keeps them running.

The terms this document defines, in the order they appear:

| Term | What it is | Code from | Made by |
|---|---|---|---|
| backend | the process that runs requests and keeps records | framework | DMSC, as the service; or a notebook or app with `local()` |
| client | the object through which a notebook or app talks to one backend; it keeps its stages and accumulators, and in the user's process the values it makes, until it releases them or ends | framework | notebook, app |
| spec | the signature of a workflow: name, version, parameters, outputs; a parameter may be a table | workflow author | workflow author |
| binding | the code that computes a spec, such as a function or a sciline pipeline | workflow author, framework (`ess.apps.pipeline.PipelineBinding`) | workflow author |
| request | a spec and its parameter values | framework | notebook, app |
| record | a request as the backend accepted it, with the names of its outputs; it never changes | framework | backend, at submission |
| reference | an input that points to an output of a record, to a dataset, or to the state of an accumulator | framework | notebook, app |
| label, member | names under which records are found later | | notebook, app |
| template | a spec with values for some parameters; the others (*blanks*) are filled later | framework | notebook, app |
| dataset source | where a backend finds datasets: it resolves names and reads data through it, and answers its clients' queries from it | framework | DMSC; a fake one in tests |
| table, row | a parameter whose value is a list of rows of one flat model; a row is the unit that arrives, such as a run | workflow author | notebook, app |
| stage | a template the backend keeps for a client; what does not depend on the blanks is computed once | framework | notebook, app |
| accumulator | an accumulating workflow the backend keeps for a client: a template whose blanks are tables, into which each push adds one row to one or more of them, and which is read like a record | framework | notebook, app |
| held state | what an accumulator's binding holds between pushes, such as a numerator and a denominator; no spec, call, or record names it | workflow author | backend, when the accumulator opens |
| state | an accumulator after its first n pushes; a read binds to the state at that moment | framework | backend, at each push |

The terms down to template are enough for most work.
The term stage follows sciline (scipp/sciline ADR 0003).
An accumulator here is a whole accumulating workflow; the `Accumulator` of sciline and of `StreamProcessor`, which holds one key, is part of a binding ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

## Client and backend

A client talks to one backend, for one proposal.
`local` makes a backend in this process and a client of it:

```python
client = local(proposal='p1', datasets=..., bind={IOFQ: iofq, ...})
```

`datasets` is the dataset source the backend reads through (see Datasets), and `bind` maps each spec the backend offers to its binding (see Specs and bindings).
Interactive work, such as a notebook, runs in the user's own process with `local()`.
Batch and automatic reduction run on *the service*, a backend that DMSC hosts; `connect('https://reduce.example', proposal='p1')` makes a client of it.
The service is designed and not implemented.

The backend runs requests and keeps records.
The service runs only workflows from installed packages.
A backend in the notebook's process can also run a workflow defined in the notebook (see Binding).

## Specs and bindings

A *workflow* is a computation that a package offers, such as the I(Q) reduction of SANS.

**Spec.** A workflow package declares a spec (`ess.reduce.spec.WorkflowSpec`) for each workflow it offers: a name, a version, a params model, and an outputs model.
Both models are pydantic models; `ess.reduce.spec` provides the field types.
A field whose value is data, such as an array or a file, is a *data field*; in a request its value is a reference to that data (see References), not the data itself.
A field may also be a *table*: a list of rows of one flat model, such as the runs of a sum (see Combining runs).
Notebooks and apps run specs; how the package implements a spec is invisible to them.

```python
class IofQParams(BaseModel):
    run: NexusFile                          # a data field: a raw NeXus file
    bins: int = 100                         # a plain value
    can: NexusFile | None = None            # the empty-can run, if any
    beam_centre: Array() | None = None      # a data field: a scipp array

class IofQOutputs(BaseModel):
    iofq: Array(ArraySpec(dims=('Q',), unit='dimensionless'))

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

A request computes every output its spec declares; it cannot ask for other intermediate results.
To make an intermediate result available, the author declares it as an output.
To inspect any other intermediate result, a scientist runs the package's sciline workflow directly in a notebook.

**Binding.** The package provides the code behind each spec, called its binding.
The simplest binding is a function that takes the parameters by name and returns the outputs by name:

```python
def iofq(run, bins, can, beam_centre) -> dict:
    ...
    return {'iofq': result}
```

The backend passes every parameter, with the defaults of the params model filled in.
Defaults belong in the params model only; a default in the binding would never be used, and could disagree with the model.

A sciline pipeline becomes a binding by naming the sciline key that each parameter sets and the key that computes each output:

```python
from ess.apps.pipeline import PipelineBinding

PipelineBinding(pipeline,
                params={'run': Filename[SampleRun], 'bins': QBins,
                        'can': Filename[BackgroundRun], 'beam_centre': BeamCenter},
                outputs={'iofq': BackgroundSubtractedIofQ})
```

The backend calls a binding in two steps, so that a stage (see Stages and accumulators) computes what stays the same once:

```python
call = binding.stage(fixed, blanks)   # fixed: {name: value} that stay the same; blanks: names left open
call(bins=50)                         # values for the blanks; called once per request through the stage
```

A request outside a stage is the case with no blanks: `binding.stage(values, ())` and then one call without arguments.
A function binding computes everything in each call.
A `PipelineBinding` computes what does not depend on the blanks once, through `sciline.Stage`, and reuses it in later calls.

A binding of a spec with tables may also provide `accumulator(fixed)`, which an accumulator needs (see Stages and accumulators).
It returns an object that holds the accumulator's held state ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

```python
held = binding.accumulator(fixed)        # every value but the tables, data read; computes what depends only on them
held.push({'runs': {'run': run_611}})    # adds one row to each named table, data read
held.outputs(['normalized'])             # {'normalized': ...}, computed from the held state
```

After rows are pushed, `outputs` gives what the plain request over those rows gives, with the same other values.
The workflow author promises this, as `StreamProcessor` asks its users to promise that a workflow is linear in its dynamic keys.
`push` takes the rows of one push, one per table, so that a binding can add them at once.
It may modify the held state in place, but not the rows.
`outputs` may return part of the held state, not a copy, and may leave out an output the spec declares optional.
It must not modify what an earlier call for the same state returned, since running readers still use it.
The backend calls both from one thread at a time.
`combine(operation)` is such a binding, for a spec whose only parameter is one table and whose outputs are the rows' fields, each combined with `operation`.
`PipelineBinding` will accumulate with `StreamProcessor` ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)); this is designed and not implemented.

A backend in the notebook's process can bind a spec to code defined in the notebook, for example to try out a change to a workflow:

```python
local(proposal='p1', datasets=..., bind={IOFQ: draft})
```

Records made with such a binding say that the code was bound in the notebook.

## Requests and records

A *request* is a spec and its parameter values.
Submitting a request returns a *record*: the request with every value filled in, defaults included, and the names of its outputs.
A record never changes, and running the same request again makes a new record.
What happens to it is its *status*: `pending`, then once `completed`, `failed`, or `cancelled`.
The client asks the backend for it, so it is always current.

```python
result = client.compute(IOFQ, {'run': dataset(run=60339), 'bins': 100})   # submit, then wait
result.request.params                    # every value, defaults included
client.status(result)                    # 'completed'
client.output(result, 'iofq')            # raises if the record failed
```

| Call | Does |
|---|---|
| `client.submit(...)` | submits, returns the records |
| `client.compute(...)` | `submit`, then `wait`; returns the records |
| `client.status(records)` | the status of each record now |
| `client.wait(records)` | blocks until finished; returns the status of each, does not raise for failures |
| `client.failure(records)` | why each record failed, or `None` |
| `client.cancel(records)` | ends unfinished records as `cancelled` |
| `client.as_completed(records)` | yields records one at a time, in the order they finish (see Drivers) |

Each takes one request or record, a list, or a dict, and returns the same shape.

A request uses only the spec's name and version, so a caller that does not have the workflow package installed gives them directly:

```python
client.compute(SpecId(name='sans-iofq', version=1), {'run': dataset(run=60339), 'bins': 100})
```

The backend validates the values against its own copy of the spec.
A request that cannot run is refused at submission with a `SubmitError` naming the field at fault, and no record is made.
Reasons are an invalid value, an unknown spec version, an unknown run number, or a reference the submitter may not read.
Requests submitted together are checked together: if one is invalid, none is submitted.

## References

An input is given by *reference*: to an output of a record, to a dataset (see Datasets), or to the state of an accumulator (see Stages and accumulators).
A reference to a record that has not finished yet is a valid input, so a chain is submitted without waiting:

```python
centre = client.submit(BEAM_CENTRE, {'run': dataset(run=60330)})   # pending
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
```

`BEAM_CENTRE` is a spec with an output `centre`; `centre.ref('centre')` refers to that output of the pending record.
The backend runs the second request once the first has completed.
The first output is kept at least until the second request has run (see How long records and values are kept).
An output can be passed to a parameter if both are data fields of the same format and, where both declare an `ArraySpec`, the two are equal: the same dims, unit, coordinates with their units, and whether the data is binned.
Otherwise the request is refused at submission.

Each step of a chain is its own call.
`Request(spec, params)` makes a request without submitting it, so that the independent requests of one step go in one call:

```python
centre = client.submit(BEAM_CENTRE, {'run': centre_run})            # pending
samples = client.submit({name: Request(IOFQ, {'run': run, 'beam_centre': centre.ref('centre')})
                         for name, run in sample_runs.items()})     # pending records, same keys
```

A request references records and datasets, never another request of the same call.
Each call is checked on its own: if the samples are refused, the beam-centre record stays.

Chain requests where the intermediate result is worth having as a result of its own, such as a beam centre or a vanadium normalization.
Each step is a separate record.
To avoid recomputing within a single workflow, use a stage instead (see Stages and accumulators).

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment; `client.latest` returns the newest.
A *member* splits a label further, one per sample or temperature.
Both are optional and are given at submission, not in the request, since they do not change the result.
A record shows both.

```python
client.compute(IOFQ, {'run': run, 'bins': 50}, label='iofq', member='250K')
client.submit({'250K': a, '260K': b}, label='iofq')       # with a label, dict keys become the members
client.latest('iofq', member='250K')
client.latest('iofq')                    # the newest record of any member
client.records(label='iofq')             # oldest first; without a label, every record of the proposal
{r.member: r for r in client.records(label='iofq')}      # the newest record of each member
```

The loop from the start, with labels, so that the curves are found later: in the user's process by that process, and on the service by other notebooks too:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, {'run': dataset(run=run), 'bins': 100},
                                 label='iofq', member=str(run))
```

A label holds no values; it only names records.

## Templates

A *template* is a spec, some parameter values, and the names of the parameters left open, its *blanks*.
Batches, stages, accumulators, and rules are built from templates.
A template can be made from any request's values; naming a field as a blank drops the value given for it.

```python
template = Template(IOFQ, params={'bins': 100}, blanks=('run',))
final = client.latest('iofq', member='250K')   # a request to reuse
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
```

Templates are frozen dataclasses; `dataclasses.replace` makes a changed copy.

## How long records and values are kept

The backend keeps two kinds of things with different lifetimes:

| | What | Kept |
|---|---|---|
| record | what ran, with which inputs, and what came of it | until its proposal has been idle for days to weeks |
| output value | the data an output holds, such as an I(Q) array | one rule for each deployment, see below |

Records are the proposal's history.
They are read long after the request ran: a batch's failures are read the next morning, a rule's progress by another user or program.
A proposal is idle while none of its clients is open and none of its records is pending; once it has been idle for a retention period of days to weeks, its records are dropped as a whole.
A result needed for longer is published (see Provenance and publication).

**In the user's process** (`local()`, [ADR 0002](adr/0002-the-client-is-the-lifetime.md)), output values are kept in memory, and only while something keeps them.
Two things do:

- **the client that made its record, until the client releases it or ends;**
- **a pending request that reads it, until the request has run.**

Nothing else keeps a value: not a record object, not a reference, not a label.
A client also keeps its stages and accumulators until it releases them or ends (see Stages and accumulators).

| Call | Does |
|---|---|
| `client.release(what)` | releases records (one, a list, or a dict), a stage, or an accumulator |
| `client.close()` | ends the client, which releases everything it keeps |
| `with client:` | closes the client at the end of the block |

```python
centre = client.compute(BEAM_CENTRE, {'run': dataset(run=60330)})
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request still reads the centre
client.output(result, 'iofq')       # kept: this client made the record
client.output(centre, 'centre')     # raises: the value is not kept
```

Releasing and ending stop no work: pending requests still run.
A released record that is still pending drops its values once it completes and no pending request reads them.
Any later call of a client that has ended raises `ClientEnded`.
A client made with `local()` owns its backend: closing the client also closes the backend, which waits until no record is pending.
A client that is never closed ends with its process.
Reading an output whose value is not kept raises an error, and a request that references it is refused at submission; the record itself remains.

**On the service** ([ADR 0005](adr/0005-the-service-writes-every-output.md)), every output is written to a file when its record completes, and the record names the file.
`client.output` and references read the file.
The service keeps no value for a client, so there is no release of values.
The files lie in an area per proposal that the framework owns, and are dropped with the proposal's history.
The one state the service holds between requests is an accumulator.
Each accumulator runs as its own job on the cluster, with a memory size and a deadline that its client declares when it opens it.
Releasing it or ending its client ends it early, and otherwise its deadline ends it.
The client may extend the deadline.
This is designed and not implemented.

```python
client.submit(requests, label='night')         # each output written when its record completes
morning = connect(url, proposal='p1')          # the next day, any process
night = morning.records(label='night')
morning.output(night[0], 'iofq')               # read from the file the record names
```

What lasts beyond the proposal's history is what `publish` puts in a catalogue (see Provenance and publication).

## Datasets

**Dataset.** A request names a dataset by what a person knows, such as a run number.
The backend resolves it, and the record names the dataset's identity, not what was typed.
The record holds none of the dataset's metadata, such as the sample name; metadata is read from the dataset source when asked (see below).
So a correction made in the source shows at the next read, and the record still names the same dataset.

```python
dataset(run=60339)
dataset(path='/home/user/data/run1.h5')
dataset(pid='20.500.12269/vanadium')     # for example a result published elsewhere
```

**Dataset source.** A backend finds datasets through its *dataset source*, which DMSC deploys with it, for example one backed by the facility's data catalogue.
The backend resolves names and reads data through it.
Listing datasets, waiting for new ones, and reading their metadata are queries to the same source, made through the client as `client.datasets`.
A client sees only the datasets its proposal may read.
So an application or a driver finds the same datasets that the backend resolves names against, and needs no source of its own.
A test gives the backend a fake dataset source.
A *selector* picks datasets by metadata.
A dataset has a kind, such as raw, derived, mask, or calibration; a selector matches raw datasets unless it names another kind.

```python
datasets = client.datasets
samples = datasets.list(Selector(role='sample'))
for run in datasets.watch(Selector(scan='17')): ...   # existing ones first, then new ones, each once
datasets.metadata(run)['sample']                        # the source's current values
```

## Combining runs: tables

Many reductions combine several runs into one result.
SANS sums the sample runs and the can runs, with numerators and denominators summed apart and divided once.
A rotation scan adds the counts of each angle into one volume.

A spec takes the runs to combine as *tables*, next to the parameters that all runs share.
A table is a list of *rows* of one flat model, declared as `list[Row]` in `ess.reduce.spec`; a form shows it with one row per run and one column per field.

```python
class SampleRun(BaseModel):                 # one row
    run: NexusFile
    transmission: NexusFile | None = None   # the run's own transmission run, if any

class CanRun(BaseModel):
    run: NexusFile

class SansIofQParams(BaseModel):
    sample_runs: list[SampleRun]            # a table
    can_runs: list[CanRun]                  # a table
    beam_centre: Array()                    # shared by all runs
    direct_beam: Array()

SANS_IOFQ = WorkflowSpec(..., params=SansIofQParams, outputs=IofQOutputs)
```

Each row of a request is a dict with a value per field: a plain value, a dataset, or a reference to an output of a record.
A row's fields are values or data fields, never another model or table; `ess.reduce.spec` refuses a spec that nests deeper.

A row is the outermost level of the reduction, the unit that arrives: a run, or the runs that belong together, such as a run and its own transmission run.
Rows of different tables are independent, and pairing runs into rows is the application's job.
Any structure below a row, such as detector banks, angle settings read from a log, or sections of a large file, belongs to the binding.
The binding loops over it inside the work per run, so that this work is done once per row and not once per bank ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

### One sum, two ways

A package offers one spec for a sum, such as I(Q) with tables of sample and can runs.
A client computes it as a plain request over the tables, or as an accumulator over them:

```python
rows = [{'run': run} for run in (r611, r612)]

# 1. a plain request over the table
client.compute(NORMALIZE, {'runs': rows, 'scale': 2.0})

# 2. an accumulator over the table, see Stages and accumulators
total = client.accumulator(Template(NORMALIZE, params={'scale': 2.0}, blanks=('runs',)))
for row in rows:
    total.push({'runs': row})
client.output(total, 'normalized')
```

Both give the same outputs: the workflow author promises that the accumulator over the rows pushed so far gives what the plain request over those rows gives.
Which quantity is summed changes the result: summing counts and normalizing once is not the same as averaging normalized curves.
The binding decides it, and the quantity must be linear in the runs; for esssans it is the numerator and denominator in Q.

A table may also hold references to outputs of records.
`combine(operator.add)` binds a spec whose only parameter is such a table and whose outputs are the rows' fields, each summed; story S4 sums the outputs of two records this way.

## Stages and accumulators

A plain request computes everything from its inputs.
In interactive work this repeats work: tuning `bins` reloads the same run for each value, and adding one run to a sum of a hundred reduces all hundred again.
Stages and accumulators let the backend keep such values in memory between requests.
A client makes them and keeps them until it releases them or ends (see How long records and values are kept).
Records made through a stage, and records that read an accumulator, are ordinary records.

**Stage.** A *stage* is a template that the backend keeps for a client.
The backend computes what does not depend on the blanks once, such as loading the run, and each call through the stage computes only the rest:

```python
tune = client.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))   # loads the run once
for bins in (50, 100, 200):
    client.compute(tune, {'bins': bins}, label='iofq')
```

`client.stage` checks the template's values as it would check a request's, with the blanks left out, so a template that a request would refuse is refused here.
It resolves dataset names when the stage is made, and `tune.template` holds the values as resolved; a later correction in the catalogue does not change what the stage computes with.
A call through the stage fills only the blanks.
A record made through a stage is the record of the plain request with the blanks filled; it does not mention the stage.
What the stage computed is a cache: the backend may drop it at any time, and the next call computes it again and makes the same record.
How the backend calls the binding for a stage is in Specs and bindings.

**Accumulator.** An *accumulator* is an accumulating workflow that the backend keeps for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
It opens from a template whose blanks are tables, and takes rows one push at a time:

```python
iofq = client.accumulator(Template(SANS_IOFQ, params={'beam_centre': ..., 'direct_beam': ...},
                                   blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611)}})   # the dict one row of the request takes
iofq.push({'can_runs': {'run': dataset(run=614)}})
iofq.push({'sample_runs': {'run': dataset(run=612)},    # one row in each of two tables
           'can_runs': {'run': dataset(run=615)}})       # reduces runs 612 and 615 only
client.output(iofq, 'iofq')                              # what the plain request over these rows gives
```

As a stage keeps what stays the same between calls, an accumulator keeps what stays the same between pushes: what it computed from the fixed values, and its *held state*, such as the numerators and denominators summed so far.
The held state is private to the binding: no spec, call, or record names it.
The outputs are the spec's outputs, computed from the held state when they are read.

- `client.accumulator` checks the template as `client.stage` does, with the tables left out. Its blanks must be one or more of the spec's tables, and the spec's binding must provide `accumulator(fixed)` (see Specs and bindings). A plain request over the tables works with any binding.
- Opening waits for the records the template references, refuses them unless they have completed, and reads them once. `iofq.template` holds the values as resolved, as a stage's template does.
- `push({table: row, ...})` adds one row to each table it names, and the rows enter one state. A key that is not one of the accumulator's tables is refused. Each row is checked by its table's row model, as the request over that one row would check it. It waits for the records the rows reference to finish, and refuses them unless they have completed.
- Rules on a whole table, such as its length, and the params model's own validators apply to the plain request over all rows pushed so far. A push that the plain request would not yet accept is added, and its state cannot be read until a later push makes the request acceptable. A push is refused if the plain request gives a fixed value other than the one the accumulator opened with, as a validator of the params model may.
- A row that references an accumulator is refused, and so is a stage or an accumulator whose template references one, since it would hold back every push for as long as it lives. Accumulators meet in a request.
- If adding the rows of a push fails, the push is refused and the accumulator takes no more pushes or reads, since the binding may hold part of them.

**Reads.** An accumulator is read as a record is ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

```python
client.output(iofq, 'iofq')                                 # as client.output(record, 'iofq')
client.output(iofq)                                         # every output it returned, by name
client.provenance(iofq)                                     # that of the plain request over the rows so far
exported = client.submit(EXPORT, {'data': iofq.ref('iofq')})   # bound when submitted
iofq.refs()                                                 # a reference to every output, by name
```

Every read binds to the accumulator's *state* at that moment: the state after the pushes so far.
It gives what the same call gives on the record of the plain request over those rows, in push order.

- A reference binds when its request is submitted. The record holds `{'accumulator': id, 'output': name, 'upto': n}`, the output after the first `n` pushes. All references to one accumulator in one submission bind to the same state, and a reference to an earlier state is refused.
- `client.output` returns a copy, so that, like a record's output, the value does not change afterwards. A request reads the output itself.
- A state is read only if its plain request would be accepted. A read of any other state, such as one with nothing pushed, or with no row in a table that needs one, is refused with the reason that request would be refused. Only the client that opened an accumulator reads it.
- `client.submit(iofq)` raises `TypeError`. A record of a state is a request of a spec that copies what it reads, such as `COPY` in Drivers.

**Pushes wait for readers.** An accumulator holds one state, and its binding may add each row to it in place, so that a push needs no second copy of a large volume.
A request reads the outputs of that state, not a copy.
So a push waits until the readers of the current state that came before it have run.
Readers are the requests that reference the state and were accepted before the push, including those cancelled while they run, and `client.output` calls in progress.
No reader waits for a push, so the wait ends.
A read made while a push waits or adds blocks until the push is done, and binds to the state after it.
A long reader holds back the next push, so the driver decides how often it looks.
The outputs of a state are computed when the state is first read for them: once while the state has readers, and only those asked for.
Releasing the accumulator, or ending its client, waits for nothing; its held state is dropped once its readers have run.

## Drivers

The backend runs requests and keeps records; it does not decide what to run.
Deciding over time what to submit, what to push into an accumulator, and what to release is the job of a driver: ordinary code that uses the client.
A notebook is a driver, and so is an application or a long-running driving server.
Drivers never run in the backend.

**A batch** submits many requests at once.
`apply` fills a template for each dataset and returns the requests as a dict.
The keys are the value of a metadata field of each dataset, here the temperature; submitting under a label makes them the members.
`apply` reads metadata through `client.datasets` and builds plain data; it submits nothing.
The notebook that submits the requests is the driver.

```python
requests = apply(template, samples, datasets, member_field='temperature')
records = client.submit(requests, label='scan')
records['250K']
```

**A loop over arrivals** waits for new datasets.
This one adds each run of a rotation scan to a volume as it arrives, and cuts through the volume after each run (story D7):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.datasets.watch(Selector(scan='17')), 1000):   # the scan's 1000 runs
    volume.push({'runs': {'run': run}})                  # waits until the previous cut has run
    client.submit(CUT, {'data': volume.ref('counts'), 'index': 0}, label='cut', member='17')
total = client.compute(COPY, {'data': volume.ref('counts')})   # a record of the last state
```

Each push reduces the new run and adds it to the volume.
Each cut binds to the state after its push, and the next push waits until that cut has run, since the push adds to the volume the cut reads.
So the loop holds one volume, not one per run or per pending cut.
On the service, the volume is a job of its own, and each run is reduced in that job ([ADR 0005](adr/0005-the-service-writes-every-output.md)).

A loop that reduces each dataset in a request of its own uses `client.as_completed`.
It consumes a generator of records in a thread, so submitting does not wait for the loop body, and yields each record once it has finished.

## Batch and automatic reduction

This sub-design builds on the core. It needs from it only that records show their label and member, and `client.datasets`.
[automatic-reduction.md](automatic-reduction.md) describes it.

A *lookup* fills further blanks per dataset.
For example, a sample needs the empty-can run measured most recently before it:

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, datasets, lookup=cans)
client.submit(requests, label='iofq')
```

A *rule* is plain data: a name, a template, a selector, and a label, and optionally a lookup or a series.
It says: for each new dataset the selector matches, fill the template and submit it under the label.

With a *series*, one request takes all matching datasets with the same value of a metadata field so far, in run order.
At ESTIA, a sample is measured at several angles, one run each; with `series='sample'`, each new angle submits a request that stitches all angles of that sample measured so far, with the sample as the member:

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
TriggerLoop(client, rules=[rule]).run()
```

The *trigger loop* is the driver for rules. It runs in a driving server, which has its own API to add, replace, and list rules.
Like any driver, it opens accumulators through its client, such as one for a sum that grows with each new dataset under a rule.
It reads which datasets it has handled from the records under each rule's label, so a restarted loop needs no memory of its own.
The label belongs to the rule: any record under it counts as handled, failed or not, so manual work uses labels of its own.
Templates and rules are plain data; the core keeps no store of them, and records do not name them.

## Provenance and publication

This sub-design builds on the core.

`client.provenance(record)` is plain data: the record's request, the records it read through all its inputs, the states of accumulators it read on the way, the datasets they read, and the software versions.
Each state of an accumulator is given as the plain request over the rows pushed before it was read: the accumulator's template with its tables filled in push order.
It stops at datasets: what lies behind a dataset, raw or published, belongs to the dataset's source.

```python
provenance = client.provenance(result)
provenance.datasets(), provenance.records(), provenance.accumulated, provenance.software
pid = client.publish(result.ref('iofq'), 'scicat')     # the output and its provenance
```

Publishing puts an output in the catalogue with its provenance; that entry, not the record, is what lasts.
Records say what ran, including a workflow bound in a notebook's own backend; publishing is not refused on that account.
Superseding a published entry with a correction is the catalogue's job.
Recomputing in a record's environment comes later.

## Guarantees

- A record holds the spec, every parameter value including defaults, and its inputs by reference; a reference to an accumulator names the state it read by its number of pushes. A record never changes; its status changes once, from pending to finished.
- A stage never changes what a record says: a record made through a stage is the record of the plain request. A read of an accumulator gives what the same call gives on the record of the plain request over the rows pushed so far, in push order.
- Every connection between requests is a reference. A value passed in memory is the referenced output itself, so a workflow must not modify its inputs. Nor may it return an output that shares memory with an accumulator's output it reads, such as a slice of it, since the next push may change that output in place.
- A record's outputs do not depend on how they were computed: through a stage, from an accumulator, or on another machine. Values may differ in rounding where the order of adding differs.
- The provenance of a record reaches every dataset it read, through all its inputs and the states of accumulators it read, with their parameter values and software versions.
- A proposal's records are kept until the proposal has been idle for the retention period, and then dropped as a whole. A published entry answers what produced it without access to the records.
- Output values are kept as stated in How long records and values are kept; releasing a value or ending a client stops no work.

## Left to the system

Not part of this API, and not visible in the code of notebooks, apps, or workflow packages:

- how history is stored, and how outputs are stored, copied, dropped, and located ([system.md](system.md))
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- where a stage is kept and computes, and on which node the job of an accumulator runs
- the file format of each output type, and the folder layout within a proposal's area (scipp/essapps#23)
- how the service notices a client whose process ended without closing it (scipp/essapps#34)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes, for example to reduce a finished scan again quickly, needs a merge of two held states. Neither `StreamProcessor` nor the binding protocol offers one, and no requirement needs it yet: runs arrive over hours, and a finished scan can be reduced again in one job ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** A request over fewer rows is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Views.** Reading part of an output, such as one cut through a volume, quickly and without making a record. The form waits for the plotting work.
5. **Accumulators on the service.** How a client declares an accumulator's memory size and deadline, and how a request that reads two accumulators gets both states in one job ([ADR 0005](adr/0005-the-service-writes-every-output.md)).
6. **Stages.** Whether stages stay a concept of their own, since the part of an accumulator computed once from its fixed values is what a stage caches (scipp/essapps#35).

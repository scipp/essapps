# ESS data-reduction framework: the API

**Status: the design of the API. Implementation starts with the core.**

[../requirements/](../requirements/README.md) states the goals and what we know, assume, and do not know about the problem. [user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it. The first release that the requirements describe combines no runs and tunes no parameters interactively, so it uses neither stages nor accumulators.

This document describes the API we want: what workflow authors, app authors, and notebooks write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in [system.md](system.md), and the system may change them without changing any code shown here.
[adr/](adr/index.md) records the decisions behind both.
[three-ways-to-run-a-spec.html](three-ways-to-run-a-spec.html) summarizes specs, stages, accumulators, and the symmetries between them on two slides.

The first sections cover what most notebooks need: submitting requests, reading their results, and chaining them.
Later sections add how runs are combined, what saves computation (stages and accumulators), what loops over many datasets look like (drivers), the symmetries that the framework and workflow authors keep, and two sub-designs that build on the core: batch and automatic reduction, and provenance and publication.

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

- Each call is kept as a record and can be found later: in the user's process (`local()`) by that process, and on the service by other notebooks and programs too. Its values are kept for the client that asked for it, and for everyone once persisted.
- Calls can run elsewhere and in parallel.
- Every result can answer where it came from: which spec, which parameter values, which datasets, which software versions.

## Terms

The table names who writes each thing and who makes one at run time, with these roles:

- *framework*: essdispatch (`ess.dispatch`) and essspec (`ess.spec`), in this repository.
- *workflow author*: writes specs and bindings in a workflow package, such as an ess instrument package.
- *app author*: writes an application on top of the client, such as a batch form, a desktop or web UI, or a driving server.
- *notebook*: a scientist's notebook that uses the client directly.
- *DMSC*: deploys the service and its dataset source, and keeps them running.

The terms this document defines, in the order they appear:

| Term | What it is | Code from | Made by |
|---|---|---|---|
| backend | the process that runs requests and keeps records | framework | DMSC, as the service; or a notebook or app with `local()` |
| client | the object through which a notebook or app talks to one backend; it keeps its stages and accumulators, and the values of the records it asks for, until it releases them or ends | framework | notebook, app |
| spec | the signature of a workflow: name, version, parameters, outputs; a parameter may be a table | workflow author | workflow author |
| binding | the code that computes a spec, such as a function or a sciline pipeline | workflow author, framework (`ess.spec.pipeline.PipelineBinding`) | workflow author |
| request | a spec and its parameter values | framework | notebook, app |
| record | a request as the backend accepted it, with the names of its outputs; it never changes | framework | backend, at submission |
| reference | an input that points to an output of a record, to a dataset, or to an output of an accumulator | framework | notebook, app |
| selection | the part of an array output that `client.output(..., select=...)` copies, by dimension | framework | notebook, app |
| label, member | names under which records are found later | | notebook, app |
| template | a spec with values for some parameters, its *fixed values*; the others (*blanks*) are filled later | framework | notebook, app |
| persist, store | to persist an output is to write it to the store, which then keeps it; nothing else is written | framework | notebook, app, at submission or later |
| dataset source | where a backend finds datasets: it resolves names and reads data through it, and answers its clients' queries from it | framework | DMSC; a fake one in tests |
| table, row | a parameter whose value is a list of rows of one flat model; a row is one run, or the runs that belong together | workflow author | notebook, app |
| single-run spec, multi-run spec | two specs of one reduction: one takes one run of each kind (sample, can), the other a table of runs of each kind; with one row per table, the multi-run spec gives the outputs of the single-run spec | workflow author | workflow author |
| plain request | a request with every value given, computed without a stage or an accumulator | framework | notebook, app |
| stage | a template the backend keeps for a client; what depends only on its fixed values is computed once | framework | notebook, app |
| accumulator | a template whose blanks are tables, which the backend keeps for a client | framework | notebook, app |
| push | one step that adds one row to each of one or more tables of an accumulator | framework | notebook, app |
| state | an accumulator after its first n pushes; its outputs are those of its plain request, the template with each table filled by the rows of these pushes | framework | backend, at each push |
| held state | what an accumulator keeps between pushes to compute the outputs of a state: what the binding accumulates, such as summed numerators and denominators, or else the rows pushed so far; no spec, call, or record names it | workflow author, framework | backend, when the accumulator opens |
| accumulating binding | a binding that adds each push to a held state of its own, such as a numerator and a denominator; for any other binding, the backend keeps the rows | workflow author | workflow author |
| read | `client.output` on an accumulator, submitting a request that references one, or freezing it; a read *pins* the state at that moment, and a submission makes a record of that state | framework | notebook, app |
| freeze | ending an accumulator and turning its last state into a record, without a copy | framework | notebook, app |

The terms down to template are enough for most work.
The term stage follows sciline (scipp/sciline ADR 0003).
The `Accumulator` class of sciline and of `StreamProcessor`, which holds one key, is part of a binding, not an accumulator in the sense above ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

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

**Spec.** A workflow package declares a spec (`ess.spec.WorkflowSpec`) for each workflow it offers: a name, a version, a params model, and an outputs model.
Both models are pydantic models; `ess.spec` provides the field types.
A field whose value is data, such as an array or a file, is a *data field*; in a request its value is a reference to that data (see References), not the data itself.
A field may also be a *table*: a list of rows of one flat model, such as the runs of a sum (see Combining runs).
Notebooks and apps run specs; how the package implements a spec is invisible to them.

```python
class IofQParams(BaseModel):
    run: NexusFile                          # a data field: a raw NeXus file
    bins: int = 100                         # a plain value
    can: NexusFile | None = None            # the empty-can run, if any
    beam_centre: Array() | None = None      # a data field: a scipp array
    direct_beam: Array() | None = None

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
def iofq(run, bins, can, beam_centre, direct_beam) -> dict:
    ...
    return {'iofq': result}
```

The backend passes every parameter, with the defaults of the params model filled in.
Defaults belong in the params model only; a default in the binding would never be used, and could disagree with the model.

A sciline pipeline becomes a binding by naming the sciline key that each parameter sets and the key that computes each output:

```python
from ess.spec.pipeline import PipelineBinding

PipelineBinding(pipeline,
                params={'run': Filename[SampleRun], 'bins': QBins,
                        'can': Filename[BackgroundRun], 'beam_centre': BeamCenter,
                        'direct_beam': DirectBeam},
                outputs={'iofq': BackgroundSubtractedIofQ})
```

How the backend calls a binding for a stage or an accumulator is in What a binding provides, at the end of Stages and accumulators.

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
client.output(result, 'iofq', select={'Q': slice(0, 10)})   # a copy of part of it
```

`select=` picks part of an array output by dimension name, an index or a slice for each dimension it names, and copies only that part.
It makes no record.
It is refused for an output that is not an array, for a dimension the output lacks, and for an index out of range.
On the service, only the selected part leaves the backend.

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

An input is given by *reference*: to an output of a record, to a dataset (see Datasets), or to an output of an accumulator (see Stages and accumulators).
A reference to an output of a record must name a value kept for the submitting client (see How long records and values are kept).
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
final = client.latest('iofq', member='250K')   # a record whose request to reuse
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
```

Templates are frozen dataclasses; `dataclasses.replace` makes a changed copy.

## How long records and values are kept

The backend keeps two kinds of things with different lifetimes:

| | What | Kept |
|---|---|---|
| record | what ran, with which inputs, and what came of it | until its proposal has been idle for days to weeks |
| output value | the data an output holds, such as an I(Q) array | while something keeps it, see below |

Records are the proposal's history.
They are read long after the request ran: a batch's failures are read the next morning, a rule's progress by another user or program.
A proposal is idle while none of its clients is open, none of its records is pending, and none of its writes is pending; once it has been idle for a retention period of days to weeks, its records are dropped as a whole.
A result needed for longer is published (see Provenance and publication).

**Values.** A record and its inputs never change, but its values come and go ([ADR 0002](adr/0002-the-client-is-the-lifetime.md)).
A value exists while something keeps it:

- **the client that asked for the record**, with `submit`, `compute`, or `freeze` without `persist=`, until it releases the record or ends;
- **a pending request that reads it**, until the request has run;
- **a push whose rows reference it**, until the push is added, and an accumulator whose template references it, until its held state has opened;
- **an accumulator whose held state keeps the rows pushed into it**, for the values those rows reference, until the held state is dropped (see What a binding provides);
- **the store**, once a client has asked to persist the value (see Persist).

Nothing else keeps a value: not a record object, not a reference, not a label.
A value that is gone does not come back: running the same request again makes a new record.
A client also keeps its stages and accumulators until it releases them or ends (see Stages and accumulators).

**What a client may read.** A submission reads only values kept for it: values its own client keeps, persisted values, datasets, and the current state of its own client's accumulators.
A request that references any other value is refused at submission, also while that record is still pending.
`client.output` reads the same values, and raises for any other; the record stays.
So another client, also one in the same process, reads a value only once it is persisted.

| Call | Does |
|---|---|
| `client.release(what)` | releases records (one, a list, or a dict), a stage, or an accumulator |
| `client.close()` | ends the client, which releases everything it keeps |
| `with client:` | closes the client at the end of the block |

```python
centre = client.compute(BEAM_CENTRE, {'run': dataset(run=60330)})
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request still reads the centre
client.output(result, 'iofq')       # kept: this client asked for the record
client.output(centre, 'centre')     # raises: nothing keeps it for this client
```

**Work that nothing keeps is cancelled.** When nothing keeps a pending record's values any more, because its client released it or ended and no pending request reads it, the record finishes as `cancelled`.
Cancelling a request lets go of what it reads, so this passes along a chain.
A workflow that is already running is not interrupted: its record is cancelled at once, and its outputs are dropped when it returns.
Any later call of a client that has ended raises `ClientEnded`.
A client made with `local()` owns its backend: closing the client also closes the backend, which waits until the pending records with a persist request are written.
A client that is never closed ends with its process.

**Persist.** Nothing is written unless persisted, in the user's process and on the service ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md)).
To *persist* an output is to write it to the *store*, which then keeps it:

```python
client.persist(result)                              # every output of a record this client keeps
client.persist(result, 'iofq')                      # only the outputs named
client.submit(requests, label='night', persist=True)            # each written when it completes
client.submit(requests, label='night', persist=('iofq',))       # only iofq; the other outputs are dropped
```

- `client.persist` adds the store as a keeper of a record the client keeps. The client keeps its own hold until it releases the record, so reads stay in memory until then.
- `persist=` at submission hands the records to the store: the client does not keep them. The outputs named are kept until written, and the others are dropped when the record completes. Batch reduction and the trigger loop submit this way, so their clients keep nothing.
- Once a persist request is logged, every client of the proposal can read and reference the values it names. Reads wait for the write.
- A failed write fails the persist request, not the record. `client.output` of the value, and the requests that reference it, fail with the write's reason. A client that still keeps the record can ask again.
- Persisting an output that is already persisted does nothing.
- Persisted values are dropped with the proposal's history at the latest. A value dropped earlier, for example to free disk space, is read as a value that nothing keeps.

```python
client.submit(requests, label='night', persist=True)
morning = connect(url, proposal='p1')               # the next day, any process
night = morning.records(label='night')
morning.output(night[0], 'iofq')                    # read from the store
```

**The two deployments.** In the user's process, values that are not persisted are kept in the process's memory, and the store is given to `local(store=...)`; without one, `persist` is refused.
In the first release, only tests pass a store, a fake one in memory.
On the service, values that are not persisted are kept in the service's memory under a cap per client, a client that vanished ends when its lease runs out, and the store is an area per proposal that the framework owns.
The service is designed and not implemented.

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
SANS sums the sample runs and the can runs, with numerators and denominators summed separately and divided once.
A rotation scan adds the counts of each angle into one volume.

A spec takes the runs to combine as *tables*, next to the parameters that all runs share.
A table is a list of *rows* of one flat model, declared as `list[Row]` in `ess.spec`; a form shows it with one row per run and one column per field.

```python
class SampleRow(BaseModel):                 # one row
    run: NexusFile
    transmission: NexusFile | None = None   # the run's own transmission run, if any

class CanRow(BaseModel):
    run: NexusFile

class MultiIofQParams(BaseModel):
    sample_runs: list[SampleRow] = Field(min_length=1)   # a table, with at least one row
    can_runs: list[CanRow] = []             # a table; empty if there is no can run
    bins: int = 100                         # shared by all runs
    beam_centre: Array() | None = None
    direct_beam: Array() | None = None

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, ..., params=MultiIofQParams, outputs=IofQOutputs)
```

The `transmission` column is hypothetical: esssans uses one transmission run for all runs of a sample ([sans](../requirements/sans.md)).
It shows a row with two columns.

Each row of a request is a dict with a value per field: a plain value or a reference, as for any parameter.
A row's fields are values or data fields, never another model or table; `ess.spec` refuses a spec that nests deeper.
A request that gives every row is a plain request over the tables:

```python
shared = {'beam_centre': ..., 'direct_beam': ...}
result = client.compute(IOFQ_MULTI, {**shared, 'can_runs': [{'run': dataset(run=614)}],
                                     'sample_runs': [{'run': dataset(run=611)}, {'run': dataset(run=612)}]})
```

A row is the outermost level of the reduction: one run, or the runs that belong together, such as a run and its own transmission run.
Rows of different tables are independent, and pairing runs into rows is the application's job.
Any structure below a row, such as detector banks, angle settings read from a log, or sections of a large file, belongs to the binding.
The binding loops over it inside the work per run, so that each run is loaded and reduced once, not once per bank ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).

Which quantity is summed changes the result: summing counts and normalizing once is not the same as averaging normalized curves.
The binding decides it.
The same rows can also be given one push at a time, through an accumulator (see Stages and accumulators).

**Single-run and multi-run specs.** `IOFQ_MULTI` is a *multi-run spec*: it takes a table of runs of each kind, sample and can.
`IOFQ` (see Specs and bindings) is the *single-run spec* of the same reduction: it takes one run of each kind.
Combining runs needs the multi-run spec, and only a spec with a table can take rows through an accumulator.
A package may offer the single-run spec as well.
It is the simpler form for one run, and it matches the package's sciline workflow, which reduces one run of each kind.
The two must agree: with one row in each table, the multi-run spec gives the outputs of the single-run spec.
An empty table stands for an optional run left out: `'can_runs': []` matches `'can': None`.

```python
client.compute(IOFQ, {'run': dataset(run=611), 'can': dataset(run=614)})
client.compute(IOFQ_MULTI, {'sample_runs': [{'run': dataset(run=611)}],
                            'can_runs': [{'run': dataset(run=614)}]})      # the same outputs
```

- Both declare the same outputs model. The single-run spec does not output partial results of each run, such as a numerator and a denominator, for a later request to sum. Summing belongs to the binding of the multi-run spec ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
- The workflow author promises that the two agree, and checks it in the package's tests (see Symmetries).
- Records of the two specs are separate: the same run reduced with `IOFQ` and with `IOFQ_MULTI` makes two records of two specs.

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
It resolves dataset names when the stage is made, and `tune.template` holds the values as resolved; a later correction in the dataset source does not change what the stage computes with.
A call through the stage fills only the blanks.
A record made through a stage is the record of the plain request with the blanks filled; it does not mention the stage.
What the stage computed is a cache: the backend may drop it at any time, and the next call computes it again and makes the same record.

A stage works on a multi-run spec too: with the rows fixed and `bins` a blank, each call rebins the runs it loaded once.
An accumulator cannot do this.
It has no blanks besides its tables: every other value, such as `bins`, is fixed when it opens.
An accumulating binding uses `bins` before it sums the runs, so a new `bins` needs a new accumulator, with every row pushed again.
Tuning a value over a fixed set of runs is what a stage is for.

**Accumulator.** An *accumulator* is a template whose blanks are tables, which the backend keeps for a client ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
Each *push* adds one row to one or more of its tables:

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params=shared, blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611)}})   # the dict one row of the request takes
iofq.push({'can_runs': {'run': dataset(run=614)}})
iofq.push({'sample_runs': {'run': dataset(run=612)},    # one row in each of two tables
           'can_runs': {'run': dataset(run=615)}})
client.output(iofq, 'iofq')                              # what the plain request over these rows gives
```

The accumulator after its first n pushes is a *state*.
The plain request of a state is the template with each table filled by the rows of these pushes, in push order.
Between pushes, an accumulator keeps what it computed once from the fixed values, and its *held state*, such as the numerators and denominators summed so far.
If the binding does not accumulate, the held state is the list of rows pushed so far (see What a binding provides).
No spec, call, or record names the held state.

- `client.accumulator` checks the template as `client.stage` does, with the tables left out, and types each fixed value by its own field and that field's validators. Its blanks must be one or more of the spec's tables. A template with no blank, with a blank that is not a table, or that references an accumulator is refused. A binding may refuse to open too ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)). It needs the values read to decide, so it refuses after the call has returned, and the accumulator stops.
- Opening returns once the backend has checked and logged it, as `client.submit` does. It is refused if a record the template references is not kept for the client, has failed, or was cancelled, and does not wait for one that is pending. The backend reads these records once, when it opens the held state after they have completed. `iofq.template` holds the values as resolved, as a stage's template does.
- `push({table: row, ...})` adds one row to each table it names, and the rows enter one state. A key that is not one of the accumulator's tables is refused. Each row is checked by its table's row model, as the request over that one row would check it, and may not reference an accumulator. The push is refused if a record the rows reference is not kept for the client, has failed, or was cancelled, and does not wait for one that is pending. It returns once logged, and the backend adds it later (see Adding waits for readers).
- Checks on a whole table, such as its length, and the params model's own validators apply when a state is read, not at a push. A table that needs two rows takes them one push at a time, and a state whose plain request would be refused cannot be read (see Reads).
- Validators that read more than one value, such as the params model's own or those of a table field, must not change a value, for example derive a fixed value from the rows: the held state was given the values typed one by one. The workflow author promises this; it is not checked.
- If adding the rows of a push fails, the accumulator stops, since the binding may hold part of them. It stops too if a record the rows reference fails or is cancelled, or if the held state fails to open. Later pushes and reads are then refused with the reason, and the records of states it did not reach fail with it, and so do the requests that read them.

**One sum, two ways.** Every spec with a table can be given its rows both ways: all in one plain request, or over time through an accumulator.
The caller chooses how rows arrive.
Both give the same outputs: a state gives what its plain request gives.
The binding chooses how a state is computed, which the caller sees only in cost:

- A binding that accumulates adds each push to its held state, so a read computes the outputs without reducing earlier runs again. For a sum, its held state must hold quantities whose sums over runs give what one request over all runs computes from, such as a numerator and a denominator, not a normalized curve. For esssans these are the numerator and denominator in Q.
- For any other binding, the held state keeps the rows, and a read computes the plain request over all of them. A joint fit, such as the scale factors of reflectometry angles, cannot add one run at a time; an accumulator over it fits again over the rows so far at each read.

**Rows and tables: two examples.** [ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md) ("Two examples") works through both in full.

*One table with two columns.*
Suppose a package lets each sample run have its own transmission run, which esssans does not (see Combining runs).
The two are then fields of one row.
One push hands both to the binding, which needs them at once to reduce the run.
Two tables, one of runs and one of transmission runs, could be paired wrongly.

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={**shared, 'can_runs': [{'run': dataset(run=614)}]},
                                   blanks=('sample_runs',)))
iofq.push({'sample_runs': {'run': dataset(run=611), 'transmission': dataset(run=610)}})
iofq.push({'sample_runs': {'run': dataset(run=613), 'transmission': dataset(run=612)}})
```

*Two independent tables.*
The sample runs and the can runs are two tables, each a blank, pushed as their runs arrive, as in the first example of this section.
One push may add a row to each table, and the rows enter one state.
A state with no can run is refused if the spec requires one, as its plain request would be; `IOFQ_MULTI` does not.
A binding that accumulates keeps what it holds for the sample apart from what it holds for the can, so that the order of pushes does not change a state.
A binding that cannot do this refuses to open, and the accumulator stops.

**Reads.** An accumulator is read as a record is ([ADR 0003](adr/0003-accumulators-add-in-place.md)):

```python
client.output(iofq, 'iofq')                                 # a copy of what client.output(record, 'iofq') gives
client.output(iofq, 'iofq', select={'Q': slice(0, 10)})     # a copy of part of it
client.output(iofq)                                         # every output it returned, by name
client.provenance(iofq)                                     # that of the plain request over the rows so far
exported = client.submit(EXPORT, {'data': iofq.ref('iofq')})   # pinned when submitted
iofq.refs()                                                 # a reference to every output, by name
```

A *read* is a call that pins the accumulator's state: `client.output`, submitting a request that references the accumulator, `client.freeze`, or `client.provenance`.
Every read pins the state at that moment: the state after the pushes logged so far.
It gives what the same call gives on the record of the plain request over those rows, in push order.
Pinning never waits for a push to be added.
A request that reads a state the accumulator has yet to reach waits for it, as it waits for a record it references.
`client.output` waits for it as for a pending record, and `client.provenance` returns at once.

- `client.output` of an accumulator copies the output, or the part that `select` names, and makes no record. It holds back the next push only while it copies.
- A submission pins the state of each accumulator its requests reference, and makes a record of that state's plain request, logged with the submission ([ADR 0008](adr/0008-a-read-of-a-state-is-a-record.md)). The records of the requests reference that record's outputs: `exported.request.params['data']` is `OutputRef(record=..., output='iofq')`. A record never names an accumulator.
- The record of a state is computed from the held state, as a call through a stage is from what the stage holds. Its outputs are what the held state returns, and a request reads them in place, so a read costs no second copy of the held state.
- The record of a state is a record like any other. No client asked for it, so no client keeps it: its outputs are kept for the requests of its submission until they have run, and then dropped. So no later request may reference it, and `client.output` of it raises; the accumulator is read instead. Nothing writes it.
- A state is counted in pushes, not rows, since a push may add a row to each of several tables. In the first example of this section, the state after 2 pushes is the plain request with sample run 611 and can run 614, and the state after 3 adds sample run 612 and can run 615.
- A read is refused if the plain request of the state would be refused, with that request's reason, such as no row in a table that needs one; a state with nothing pushed is no exception. The first read of a state validates its plain request, at a cost that grows with the number of rows, so a driver that reads after every push pays it at every push. Only the client that opened an accumulator reads it.
- `client.provenance` reads no output, so it holds back no push.
- `client.submit(iofq)` raises `TypeError`.
- A request reads at most one accumulator, and a row or a template reads none ([ADR 0003](adr/0003-accumulators-add-in-place.md), which lists the cases). To use an output of a finished accumulator in another, a template or row references the record that `freeze` returns. Sample and background runs that arrive at the same time are two tables of one accumulator.

**Adding waits for readers.** An accumulator keeps one held state, and its binding may add each push to it in place, so that a push needs no second copy of a large volume.
Since a request reads the outputs of the state itself, a push is added only once nothing keeps the state before it.
Adding it also waits for the records its rows reference to complete.
The state is kept by its pending reads: the records of that state, until the requests that read them have run, including those cancelled while they run; a freeze, until its record has finished; and `client.output` calls in progress.
Every wait is for work logged before the waiter: a request waits for records submitted before it and for pushes logged before its submission, and a push waits for the reads of the state before it, all logged before the push, since only the submission that made the record of a state reads it. So no wait goes round in a circle.
The backend adds the pushes of one accumulator on its workers, one at a time, in the order they were logged.
A long reader holds back the next addition, but not the driver.
The outputs of a state are computed all at once, the first time the state is read, and kept until the next push is added; no output is computed for a state that no one reads.

**Freeze.** `client.freeze(acc)` ends the accumulator and returns the record of its last state, the state after the pushes logged before it:

```python
total = client.freeze(iofq)              # no copy, no more pushes
client.output(total, 'iofq')             # read as any record's output
client.freeze(volume, persist=True)      # the store keeps it, not the client
```

- The record's values are the outputs of the held state, computed once and not copied. The rest of the held state is dropped once the record has completed.
- The client keeps the record, unless `persist=` hands it to the store, as at submission.
- A push logged after the freeze is refused. Once the record has completed, a read of the accumulator is refused and names the record.
- The plain request of the last state is checked, as a read checks it. If it would be refused, the freeze is refused and the accumulator stays as it was.
- If the record fails or is cancelled, the accumulator takes no pushes, but can still be read and frozen again, unless it has stopped.

**Releasing.** Releasing an accumulator drops the client's hold on it, so a driver may release an accumulator right after its last read:

```python
iofq.push({'sample_runs': {'run': dataset(run=613)}})         # returns once logged
exported = client.submit(EXPORT, {'data': iofq.ref('iofq')})   # pins the state after this push
client.release(iofq)                                           # the push is added, and the export runs
client.output(exported, 'text')
```

Releasing the accumulator, or ending its client, waits for nothing.
The pinned reads keep their states and the pushes up to them, and the held state is dropped once its readers have run.
Pushes that no pinned read needs are dropped.
A released accumulator takes no more pushes or reads.
Work that nothing keeps any more is cancelled: releasing `exported` too would cancel it, and with it the record of the state it reads.

**On the service**, the outputs of the record of a state are part of the held state, held where the held state is.
How the service holds an accumulator's held state, bounds its memory, and ends it is open (open question 5).

### What a binding provides

The backend calls a binding in two steps, so that a stage computes what stays the same once:

```python
call = binding.stage(fixed, blanks)   # fixed: {name: value} that stay the same; blanks: names left open
call(bins=50)                         # values for the blanks; called once per request through the stage
```

A plain request is the case with no blanks: `binding.stage(values, ())` and then one call without arguments.
A function binding computes everything in each call.
A `PipelineBinding` computes what does not depend on the blanks once, through `sciline.Stage`, and reuses it in later calls.

A binding of a spec with tables may also provide `held_state(fixed)`, which makes the held state of an accumulator; such a binding *accumulates*:

```python
held = binding.held_state(fixed)         # fixed: every value but the tables, with data fields loaded
held.push({'runs': {'run': run_611}})    # one row for each named table, with data fields loaded
held.outputs()                           # {'normalized': ..., ...}: every output, from the held state
```

- After rows are pushed, `outputs` gives what the plain request over those rows gives, with the same fixed values. The workflow author promises this, as `StreamProcessor` asks its users to promise that a workflow is linear in its dynamic keys.
- `push` takes the rows of one push, one per table, so that a binding can add them at once. It may modify the held state in place, but not the rows.
- `outputs` returns every output, but may leave out one the spec declares optional, and may return part of the held state, not a copy. The backend calls it once for each state that is read, and keeps what it returns until the next push.
- The backend calls `push` and `outputs` from one thread at a time, and never `outputs` while a push adds.

`combine(operation)` is such a binding, for a spec whose only parameter is one table and whose outputs are the rows' fields, each combined with `operation`.
Story S4 ([user-stories.md](user-stories.md)) sums the outputs of two records with `combine(operator.add)`, in a plain request.

For a binding without `held_state(fixed)`, the framework makes a held state that keeps the rows pushed so far, with data fields loaded.
A read computes the plain request over them, through a stage of the binding made when the accumulator opens:

```python
call = binding.stage(fixed, tables)        # once, when the accumulator opens; tables: its blanks
call(sample_runs=[row_611, row_612], can_runs=[row_614])   # once per state read, with every row so far
```

The outputs are those of the plain request; only the cost differs.
A `PipelineBinding` computes what depends only on the fixed values once, through `sciline.Stage`.
Each read repeats the work per run for every row so far.
In the user's process, the held state keeps the value of every row, so a row that references an output of a record keeps that value while the accumulator lives, even after the client releases the record.
A binding avoids both costs by providing `held_state(fixed)`.
`PipelineBinding` provides none; accumulating with `StreamProcessor` is designed and not implemented (scipp/essapps#40).

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
for run in islice(client.datasets.watch(Selector(scan='17')), 300):    # the scan's 300 runs
    volume.push({'runs': {'run': run}})                  # added once the previous cut is copied
    show(client.output(volume, 'counts', select={'q': 0}))   # copies the cut; makes no record
total = client.freeze(volume, persist=True)              # the last state as a record, written
```

`VOLUME` is a toy spec of the stories ([user-stories.md](user-stories.md), Toy specs); its binding has a held state of its own.
Each push reduces the new run and adds it to the volume.
Each cut pins the state after its push, and the next push is added once that cut has been copied, since the push adds to the volume the cut reads.
So the loop holds one volume, not one per run or per cut, and makes one record, at the end.
`client.output` waits until the push is added, so the loop runs at the pace of the additions.
A cut that is kept, or a fit of the growing volume, is a request that references `volume.ref('counts')`; it reads the state in place and makes a record of it.

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
client.submit(requests, label='iofq', persist=('iofq',))
```

A batch application submits with `persist=`, so its client keeps nothing and the results are found the next day (see Persist).

A *rule* is plain data: a name, a template, a selector, and a label, and optionally a lookup, a series, and the outputs it persists, all of them by default.
It says: for each new dataset the selector matches, fill the template and submit it under the label, persisted.
A record that the template references, such as a beam centre, must be persisted, since the trigger loop's client reads it.

With a *series*, one request takes all matching datasets with the same value of a metadata field so far, in run order, each as a row `{'run': dataset}` of the template's table blank.
At ESTIA, a sample is measured at several angles, one run each; with `series='sample'`, each new angle submits a request that stitches all angles of that sample measured so far, with the sample as the member:

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
TriggerLoop(client, rules=[rule]).run()
```

The *trigger loop* is the driver for rules. It runs in a driving server, which has its own API to add, replace, and list rules.
It submits with `persist=`, so its client keeps nothing.
It reads which datasets it has handled from the records under each rule's label, so a restarted loop needs no memory of its own.
The label belongs to the rule: any record under it counts as handled, failed or not, so manual work uses labels of its own.
Templates and rules are plain data; the core keeps no store of them, and records do not name them.
Whether a rule pushes into an accumulator is open ([automatic-reduction.md](automatic-reduction.md)).

## Provenance and publication

This sub-design builds on the core.

`client.provenance(record)` is plain data: the record's request, the records it read through all its inputs, the datasets they read, and the software versions.
A state of an accumulator read on the way is one of these records: the plain request over the rows pushed before it was read, the accumulator's template with its tables filled in push order ([ADR 0008](adr/0008-a-read-of-a-state-is-a-record.md)).
It stops at datasets: what lies behind a dataset, raw or published, belongs to the dataset's source.

```python
provenance = client.provenance(result)
provenance.datasets(), provenance.records(), provenance.software
pid = client.publish(result.ref('iofq'), 'scicat')     # the output and its provenance
```

Publishing puts an output in the catalogue with its provenance; that entry, not the record, is what lasts.
`publish` reads the value as `client.output` does, so it needs no persist request.
Records say what ran, including a workflow bound in a notebook's own backend; publishing is not refused on that account.
Superseding a published entry with a correction is the catalogue's job.
Recomputing in a record's environment comes later.

## Symmetries

Each symmetry names a change that a caller may make without changing a result.
The framework keeps some by construction; the others are promises of the workflow author.

| Symmetry | The caller may change | Unchanged | Kept by |
|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec with one row per table | the outputs | the workflow author, if the package offers both specs |
| caching | a call through a stage ↔ the plain request with the blanks filled | the record and its outputs | the framework makes the same record; the workflow author promises that a call through a stage computes the same outputs (`PipelineBinding` does through `sciline.Stage`) |
| arrival | rows pushed into an accumulator one at a time ↔ the same rows in one plain request | the outputs of state n | the workflow author, if the binding accumulates; the framework, if it does not |
| order | the order of pushes into different tables, and whether rows of different tables come in one push | the outputs once the same rows are in | the workflow author, if the binding accumulates: it keeps what it holds for each table apart (see Two independent tables); the framework, if it does not |
| placement | where a request runs: the user's process, the service, which worker | the record and its outputs | the framework |

Arrival, order, and placement hold up to rounding where the order of adding differs.
Each promise of the workflow author compares two calls with the same inputs, so a package can test it without knowing the correct outputs.
`ess.spec.testing` has a check for each: `check_one_row`, `check_caching`, and `check_arrival_and_order`, which pushes the same rows in several orders.
Caching and arrival differ in two ways that the symmetries leave open.
A call through a stage makes a record, while a push makes no record; a submission that reads a state makes a record of the state's plain request.
Arrival keeps the outputs, not the cost: if the binding does not accumulate, each read computes the per-run part of every row again.

## Guarantees

- A record holds the spec, every parameter value including defaults, and its inputs by reference. A record references only outputs of records and datasets; a request that reads an accumulator references the record of the state its submission pinned ([ADR 0008](adr/0008-a-read-of-a-state-is-a-record.md)). A record never changes; its status changes once, from pending to finished.
- A stage never changes what a record says: a record made through a stage is the record of the plain request (caching). A read of an accumulator gives what the same call gives on the record of the plain request over the rows pushed so far, in push order (arrival). This rests on two promises of the workflow author: a held state gives what the plain request gives (What a binding provides), and validators that read more than one value change none (Stages and accumulators).
- Every connection between requests is a reference. A value passed in memory is the referenced output itself, so a workflow must not modify its inputs. Nor may it return an output that shares memory with an input, such as a slice of it: a workflow cannot tell whether an input is a record's value or an accumulator's state, which the next push may change in place. `ess.spec.testing` checks this for values backed by numpy.
- A record's outputs do not depend on how they were computed: through a stage, from an accumulator, or on another machine. Values may differ in rounding where the order of adding differs.
- The provenance of a record reaches every dataset it read, through all its inputs, with their parameter values and software versions.
- A proposal's records are kept until the proposal has been idle for the retention period, and then dropped as a whole. A published entry answers what produced it without access to the records.
- Output values are kept as stated in How long records and values are kept. A submission reads only values kept for its client, so whether it is accepted does not depend on how far other work has come. Work that nothing keeps is cancelled. Nothing is written unless persisted.

## Left to the system

Not part of this API, and not visible in the code of notebooks, apps, or workflow packages:

- how history is stored, and how outputs are stored, copied, dropped, and located ([system.md](system.md))
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- where a stage is kept and computes, and where the held state of an accumulator is held
- the format of each persisted output type, and the layout of the store within a proposal's area (scipp/essapps#23)
- how the service notices a client whose process ended without closing it, and how it sets the cap per client (scipp/essapps#27, scipp/essapps#34)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes, for example to reduce a finished scan again quickly, needs a merge of two held states. Neither `StreamProcessor` nor the binding protocol offers one, and no requirement needs it yet: runs arrive over hours, and a finished scan can be reduced again in one job ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** A request over fewer rows is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Looks that compute.** `select=` only indexes. A projection, or a thick slice summed, would run a spec next to the value and return its result without a record. The form waits for the plotting work. So does a look at the latest state already added, without waiting for the pushes logged so far.
5. **Accumulators on the service.** How the service holds a held state, bounds its memory, and ends it. One job per accumulator on the cluster, with a memory size and a deadline that its client declares, fits a spectroscopy volume of hundreds of GB; whether it fits other accumulators is open ([ADR 0005](adr/0005-nothing-is-written-unless-persisted.md), Open; scipp/essapps#27).
6. **Persist at submission.** `persist=` makes the store the only keeper; `client.persist` adds it next to the client. Whether `persist=` should also keep the values for the client, or both calls need more options, is settled once stories use them.
7. **Values modified in place.** In the user's process, `client.output` of a record returns the value itself. A notebook that modifies it in place breaks the promise every workflow makes, and changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.

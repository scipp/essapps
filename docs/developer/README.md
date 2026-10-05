# ESS data-reduction framework: the API

**Status: the design of the API. Implementation starts with the core.**

[../requirements/](../requirements/README.md) states the goals and what we know, assume, and do not know about the problem. [user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.

This document describes the API we want: what workflow authors, app authors, and notebooks write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in [system.md](system.md), and the system may change them without changing any code shown here.
[adr/](adr/index.md) records the decisions behind both.

The first sections cover what most notebooks need: submitting requests, reading their results, and chaining them.
Later sections add what saves computation in interactive work (stages and accumulators), what loops over many datasets look like (drivers), and two sub-designs that build on the core: batch and automatic reduction, and provenance and publication.

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

- Each call is kept as a record and can be found later, by this notebook or another one.
- Calls can run elsewhere and in parallel.
- Every result can answer where it came from: which spec, which parameter values, which datasets, which software versions.

## Terms

The table names who writes each thing and who makes one at run time, with these roles:

- *framework*: this package, and `ess.reduce.spec`.
- *workflow author*: writes specs and bindings in a workflow package, such as an ess instrument package.
- *app author*: writes an application on top of the client, such as a batch form, a desktop or web UI, or a driving server.
- *notebook*: a scientist's notebook that uses the client directly.
- *DMSC*: deploys a hosted backend and its dataset source, and keeps them running.

The terms this document defines, in the order they appear:

| Term | What it is | Code from | Made by |
|---|---|---|---|
| backend | the process that runs requests and keeps records | framework | DMSC; or a notebook or app with `local()` |
| client | the object through which a notebook or app talks to one backend; it keeps what it makes until it releases it or ends | framework | notebook, app |
| spec | the signature of a workflow: name, version, parameters, outputs | workflow author | workflow author |
| binding | the code that computes a spec, such as a function or a sciline pipeline | workflow author, framework (`ess.apps.pipeline.PipelineBinding`) | workflow author |
| request | a spec and its parameter values | framework | notebook, app |
| record | a request as the backend accepted it, with the names of its outputs; it never changes | framework | backend, at submission |
| reference | an input that points to an output of a record, or to a dataset | framework | notebook, app |
| label, member | names under which records are found later | | notebook, app |
| template | a spec with values for some parameters; the others (*blanks*) are filled later | framework | notebook, app |
| dataset source | where a backend finds datasets: it resolves names and reads data through it, and answers its clients' queries from it | framework | DMSC; a fake one in tests |
| spec over a table | a spec whose only parameter is a list of elements of one model, such as a sum over runs | workflow author | workflow author |
| stage | a template the backend keeps for a client; what does not depend on the blanks is computed once | framework | notebook, app |
| accumulator | a spec over a table the backend keeps for a client, to which elements are pushed one at a time, such as a running sum | framework | notebook, app |
| snapshot | a record of an accumulator's current value, kept until the next push | framework | backend, when an accumulator is submitted |

The rows down to template are enough for most work.
The terms stage and accumulator follow sciline (scipp/sciline ADR 0003).

## Client and backend

A client talks to one backend, for one proposal.
`local` makes a backend in this process and a client of it:

```python
client = local(proposal='p1', datasets=..., bind={IOFQ: iofq, ...})
```

`datasets` is the dataset source the backend reads through (see Datasets), and `bind` maps each spec the backend offers to its binding (see Specs and bindings).
A client of a hosted backend, `connect('https://reduce.example', proposal='p1')`, is planned and not implemented.

The backend runs requests and keeps records.
A hosted backend runs only workflows from installed packages.
A backend in the notebook's process can also run a workflow defined in the notebook (see Binding).

## Specs and bindings

A *workflow* is a computation that a package offers, such as the I(Q) reduction of SANS.

**Spec.** A workflow package declares a spec (`ess.reduce.spec.WorkflowSpec`) for each workflow it offers: a name, a version, a params model, and an outputs model.
Both models are pydantic models; `ess.reduce.spec` provides the field types.
A field whose value is data, such as an array or a file, is a *data field*; in a request its value is a reference to that data (see References), not the data itself.
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
A binding of a spec over a table, such as `combine(operator.iadd)`, may also provide `accumulator()`.
An accumulator needs it (see Stages and accumulators); [system.md](system.md) describes that protocol.

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

An input is given by *reference*: to an output of a record, or to a dataset (see Datasets).
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

The loop from the start, with labels, so that another notebook finds the curves:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, {'run': dataset(run=run), 'bins': 100},
                                 label='iofq', member=str(run))
```

A label holds no values; it only names records.

## Templates

A *template* is a spec, some parameter values, and the names of the parameters left open, its *blanks*.
Batches, stages, and rules are built from templates.
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
| output value | the data an output holds, such as an I(Q) array | while a client keeps it, see below |

Records are the proposal's history.
They are read long after the request ran: a batch's failures are read the next morning, a rule's progress by another user or program.
A proposal is idle while none of its clients is open and none of its records is pending; once it has been idle for a retention period of days to weeks, its records are dropped as a whole.
A result needed for longer is published (see Provenance and publication).

Output values are large, so the backend keeps a value only while something keeps it.
Two things do:

- **the client that made its record, until the client releases it or ends;**
- **a pending request that reads it, until the request has run.**

Nothing else keeps a value: not a record object, not a reference, not a label.
A snapshot's value also ends at the next push into its accumulator, or when the accumulator is released, even while its client keeps it (see Stages and accumulators).
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

A value that must outlive its client, such as a beam centre for tomorrow's batch, must be saved.
How to save is not designed yet.
Reading an output whose value is not kept raises an error, and a request that references it is refused at submission; the record itself remains.
What lasts beyond the proposal is what `publish` puts in a catalogue (see Provenance and publication).

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

## Combining runs: specs over a table

Many reductions combine several runs into one result.
The counts of each angle of a rotation scan are summed into one volume.
A normalization sums numerators and denominators over runs and divides only at the end.

What one run contributes is an *element*, such as an array of counts; a model declares its fields.
The combining step is a spec whose only parameter is a list of elements, a *table* in `ess.reduce.spec`: a form shows it with one row per element and one column per field.
Such a spec is a *spec over a table*.

```python
class Counts(BaseModel):
    counts: Array(ArraySpec(dims=('Q', 'energy_transfer'), unit='counts'))

class VolumeParams(BaseModel):
    angles: list[Counts]                                                      # a table

VOLUME = WorkflowSpec(name='volume', ..., params=VolumeParams, outputs=Counts)   # bound to a sum

angles = [client.submit(ANGLE, {'run': r}) for r in scan]                    # one run per angle
volume = client.submit(VOLUME, {'angles': [a.refs('counts') for a in angles]})
client.submit(CUT, {'data': volume.ref('counts'), 'energy_transfer': 2.0})
```

Each element of a request is a dict with a reference per field, which `record.refs(...)` gives.
An element's fields are values or data fields, never another model or table; `ess.reduce.spec` refuses a spec that nests deeper.

The outputs are whatever the combination gives.
A sum gives the fields of the element, as VOLUME does, so a sum can be summed again.
A mean gives other fields, and a sum of 32-bit integer counts may give 64-bit integers so that it does not overflow:

```python
MEAN = WorkflowSpec(name='mean', ..., params=VolumeParams, outputs=MeanCounts)
```

A spec over a table connects specs whose authors did not plan for each other, as long as their fields match.

A reduction with a sum in the middle splits into three specs.
A package builds CONTRIBUTE and FINALIZE from the parts of one sciline `split`, cut at the keys that PARTS_SUM sums:

```text
run 611 ── CONTRIBUTE ──┐
run 612 ── CONTRIBUTE ──┼── PARTS_SUM ── FINALIZE ── I(Q)
run 613 ── CONTRIBUTE ──┘
```

```python
class NormalizationParts(BaseModel):           # one element of PARTS_SUM
    numerator: Array(ArraySpec(dims=('Q',), unit='counts'))
    denominator: Array(ArraySpec(dims=('Q',), unit='counts'))

class PartsSumParams(BaseModel):
    parts: list[NormalizationParts]

CONTRIBUTE = WorkflowSpec(name='sans-contribute', ..., params=ContributeParams, outputs=ContributeOutputs)
PARTS_SUM = WorkflowSpec(name='sans-parts-sum', ..., params=PartsSumParams, outputs=NormalizationParts)
FINALIZE = WorkflowSpec(name='sans-finalize', ..., params=FinalizeParams, outputs=IofQOutputs)
```

`ContributeOutputs` has the fields `numerator` and `denominator`, and may have more, such as a transmission per run, which are not summed.
`FinalizeParams` has the data fields `numerator` and `denominator`; FINALIZE does not know that they are sums.
A FINALIZE may read several sums, for example one over sample runs and one over background runs.
Each element of a PARTS_SUM request holds the numerator and denominator of one run, so the two cannot be paired with different runs.

Which quantity is summed changes the result: summing counts and normalizing once is not the same as averaging normalized curves.
The workflow author decides this for the specs the package ships; whoever connects specs from different packages, in a notebook or an app, decides it for that chain.

### One sum, three ways

```python
# 1. one request of a spec that sums its runs internally
client.compute(NORMALIZE, {'runs': [r611, r612], 'scale': 2.0})   # NORMALIZE: such a spec

# 2. a chain of requests
parts = [client.submit(CONTRIBUTE, {'run': r}) for r in (r611, r612)]
total = client.submit(PARTS_SUM, {'parts': [p.refs('numerator', 'denominator') for p in parts]})
client.compute(FINALIZE, {**total.refs(), 'scale': 2.0})     # refs(): every output, by name

# 3. the same chain through an accumulator, see Stages and accumulators
```

Ways 2 and 3 give the same values. Way 1 makes one record of a different spec.
NORMALIZE takes a scalar next to its list of runs, so it is not a spec over a table, and it cannot be an accumulator.
The framework allows all three; a package decides which specs it offers.

## Stages and accumulators

A plain request computes everything from its inputs.
In interactive work this repeats work: tuning `bins` reloads the same run for each value, and adding one run to a sum of a hundred sums all hundred again.
Stages and accumulators let the backend keep such values in memory between requests.
A client makes them and keeps them until it releases them or ends (see How long records and values are kept).
Records made through them are ordinary records.

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

**Accumulator.** An *accumulator* is a spec over a table that the backend keeps for a client.
A request of a spec over a table needs the whole table at submission; an accumulator takes one element at a time and does not combine the earlier ones again.
A push takes the same dict that one element of the request takes.
As a stage keeps what stays the same between calls, an accumulator keeps what stays the same between pushes: the combination of the elements so far, such as their sum.
Any spec over a table can be an accumulator if its binding provides `accumulator()` (see Specs and bindings).

```python
total = client.accumulator(PARTS_SUM)
total.push({'numerator': record.ref('numerator'),    # an element: a reference per field
            'denominator': record.ref('denominator')})
total.push(record.refs('numerator', 'denominator'))   # the same, where the names match
total.push({'numerator': other.ref('counts'), 'denominator': other.ref('monitor')})
```

A push is refused if a required field of the element is missing or a field is extra.
A push combines every field or none, so the outputs of a snapshot cover the same runs; one accumulator per field would not guarantee that.
The referenced outputs may have any name; `record.refs(...)` is short for the case where they are named like the fields.
Without names, `refs()` references every output.
The push waits for the records it references to finish and refuses them unless they have completed.
A push is checked when made, as a request over that one element would be.
A push that references a snapshot is refused: pushing a snapshot into an accumulator is not supported.
Accumulators meet in a request instead, such as a FINALIZE that reads two sums.

**Snapshot.** To use the combined value as the input of another request, submit the accumulator.
This makes a *snapshot*: a record whose output is the current combined value, completed at once.
Like any record, its outputs can be referenced.
A snapshot takes no label or member; the requests that read it do.

Way 3 of the sum above:

```python
total = client.accumulator(PARTS_SUM)
for run in (r611, r612):
    total.push(client.compute(CONTRIBUTE, {'run': run}).refs('numerator', 'denominator'))
first = client.compute(FINALIZE, {**client.compute(total).refs(), 'scale': 2.0}, label='sum')

c613 = client.compute(CONTRIBUTE, {'run': r613})
total.push(c613.refs('numerator', 'denominator'))              # c611 and c612 are not summed again
added = client.compute(FINALIZE, {**client.compute(total).refs(), 'scale': 2.0}, label='sum')
```

A snapshot's value is the output of the plain request over the elements pushed so far, in push order.
For the second snapshot, that request is PARTS_SUM over `{'parts': [c.refs('numerator', 'denominator') for c in (c611, c612, c613)]}`, the request that way 2 makes; `c611` and `c612` are the CONTRIBUTE records of runs `r611` and `r612`.
A snapshot's record names the accumulator and how many elements it covers, not the elements themselves; provenance lists them:

```python
snapshot = client.compute(total)
snapshot.submitted                       # what a record ran: a Request, or here a Snapshot
                                         # Snapshot(spec=sans-parts-sum/v1, accumulator=total.id, upto=3)
client.provenance(snapshot).records()    # [c611, c612, c613]: the records it read, in push order
```

**Adding in place.** An accumulator may add each element into the value it holds, as `combine(operator.iadd)` does, so that a push needs no memory for a second combined value.
A snapshot's output is that value itself, not a copy, and several snapshots between two pushes share it.
So the next push would change it, and the snapshot rule is:

> A snapshot's value is kept until the next push into its accumulator, or until the accumulator is released.

A push first ends the snapshots taken since the previous push: from then on a request that references one is refused at submission, and reading one raises; the records stay.
The push then waits until the requests that read those snapshots, submitted before the push, have run, and only then adds the element.
No request waits for a push, so this wait ends; a long request that reads a snapshot holds back the next push.
Releasing the accumulator or ending the client ends its snapshots in the same way, but waits for nothing: their value is dropped once the requests that read them have run.

```python
total = client.accumulator(PARTS_SUM)
total.push(c611.refs('numerator', 'denominator'))
snapshot = client.submit(total)
normalized = client.submit(FINALIZE, {**snapshot.refs(), 'scale': 2.0})   # reads the snapshot's value
numerator = client.output(snapshot, 'numerator')                           # a copy
total.push(c612.refs('numerator', 'denominator'))                          # waits until FINALIZE has run
client.output(snapshot, 'numerator')                                       # raises: the value ended at the push
client.output(normalized, 'normalized')                                    # from the value before the push
```

`client.output` of a snapshot returns a copy, so a value read in a notebook does not change at the next push.
A request that reads a snapshot reads the value itself; it is not copied.
The backend keeps no earlier state of an accumulator: what it keeps past the next push are the outputs of the requests that read the snapshot, such as a cut or FINALIZE above.

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
This one reduces each angle of a rotation scan as its run arrives, and keeps a volume of the angles finished so far:

```python
volume = client.accumulator(VOLUME)
angles = (client.submit(ANGLE, {'run': run}) for run in datasets.watch(Selector(scan='17')))
for angle in client.as_completed(angles):                       # in the order they finish
    volume.push(angle.refs())                                   # ANGLE outputs only counts
    client.release(angle)                                       # the volume holds the sum
    snapshot = client.submit(volume)
    client.submit(CUT, {'data': snapshot.ref('counts'), 'energy_transfer': 2.0},
                  label='cut', member='17')
```

The generator `angles` submits a request for each run as it arrives.
`as_completed` consumes it in a thread, so submitting does not wait for the loop body.
It yields each record once it has finished, so the angles are reduced in parallel while the loop pushes one at a time.
`VOLUME` adds in place, and each snapshot shares the volume's value, so the loop holds one volume, not one per angle or per pending cut.
Each push waits until the previous cut has run, since the cut reads the volume the push adds to.
Releasing each angle once it is pushed keeps the client from keeping a thousand angles.

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
In reflectometry, a sample is measured at several angles, one run each; with `series='sample'`, each new angle submits a request that stitches all angles of that sample measured so far, with the sample as the member:

```python
rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
TriggerLoop(client, rules=[rule]).run()
```

The *trigger loop* is the driver for rules. It runs in a driving server, which has its own API to add, replace, and list rules.
Like any driver, it keeps what it makes through its client, such as an accumulator for a sum that grows with each new dataset under a rule.
It reads which datasets it has handled from the records under each rule's label, so a restarted loop needs no memory of its own.
The label belongs to the rule: any record under it counts as handled, failed or not, so manual work uses labels of its own.
Templates and rules are plain data; the core keeps no store of them, and records do not name them.

## Provenance and publication

This sub-design builds on the core.

`client.provenance(record)` is plain data: what the record ran (its request or snapshot), the records it read through all its inputs, the datasets they read, and the software versions.
It stops at datasets: what lies behind a dataset, raw or published, belongs to the dataset's source.

```python
provenance = client.provenance(result)
provenance.datasets(), provenance.records(), provenance.software
pid = client.publish(result.ref('iofq'), 'scicat')     # the output and its provenance
```

Publishing puts an output in the catalogue with its provenance; that entry, not the record, is what lasts.
Records say what ran, including a workflow bound in a notebook's own backend; publishing is not refused on that account.
Superseding a published entry with a correction is the catalogue's job.
Recomputing in a record's environment comes later.

## Guarantees

- A record holds the spec, every parameter value including defaults, and its inputs by reference; a snapshot holds its accumulator and how many elements it covers. A record never changes; its status changes once, from pending to finished.
- A stage never changes what a record says: a record made through a stage is the record of the plain request. A snapshot's value is the value of the plain request over the elements it covers, in push order.
- Every connection between requests is a reference. A value passed in memory is the referenced output itself, so a workflow must not modify its inputs. Nor may it return an output that shares memory with a snapshot it reads, such as a slice of it, since the snapshot's value changes at the next push.
- A record's outputs do not depend on how they were computed: through a stage or an accumulator, on another machine, or as a tree over many processes. Values may differ in rounding where the order of combining differs.
- The provenance of a record reaches every dataset it read, through all its inputs, with their parameter values and software versions.
- A proposal's records are kept until the proposal has been idle for the retention period, and then dropped as a whole. A published entry answers what produced it without access to the records.
- Output values are kept as stated in How long records and values are kept; releasing a value or ending a client stops no work.

## Left to the system

Not part of this API, and not visible in the code of notebooks, apps, or workflow packages:

- how history is stored, and how outputs are stored, copied, dropped, and located ([system.md](system.md))
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- whether a request of a spec over a table with many elements is computed in parts on many processes, such as a tree of partial sums, and how that is configured. The record is the same. Computing in parts needs the author to declare that grouping does not change the result, as for a sum (open question 1).
- where a stage or an accumulator is kept and computes, for example next to a desktop application; whether a value passed in memory is also written; and how a client whose process is gone is ended
- how access across proposals is enforced

## Open questions

1. **Grouping.** How an author declares that grouping does not change the result of a spec over a table, which a tree of partial sums over a plain request needs. Only a spec whose outputs have the fields of its element can declare it, since the result of each part is combined again; a mean cannot.
2. **Removing an element.** A request of the spec over fewer elements is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each element.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Views.** Reading part of an output, such as one cut through a volume, quickly and without making a record. The form waits for the plotting work.

# ESS data-reduction framework: the API

**Status: the design of the API. Implementation starts with the core.**

[scoping.md](scoping.md) states the goals. [user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.

This document describes the API we want: what workflow authors, app authors, and notebooks write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in [system.md](system.md), and the system may change them without changing any code shown here.
[adr/](adr/index.md) records the decisions behind both.

The first sections cover what most notebooks need: submitting requests, reading their results, and chaining them.
Later sections add what saves computation in interactive work (sessions), what loops over many datasets look like (drivers), and two sub-designs that build on the core: batch and automatic reduction, and provenance and publication.

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
- *operator*: deploys a hosted backend and its dataset source.

The terms this document defines, in the order they appear:

| Term | What it is | Code from | Made by |
|---|---|---|---|
| backend | the process that runs requests and keeps records | framework | operator; or a notebook or app with `local()` |
| client | the object through which a notebook or app talks to one backend | framework | notebook, app |
| spec | the signature of a workflow: name, version, parameters, outputs | workflow author | workflow author |
| binding | the code that computes a spec, such as a function or a sciline pipeline | workflow author, framework (`PipelineBinding`) | workflow author |
| request | a spec and its parameter values | framework | notebook, app |
| record | a request as the backend accepted it, with the names of its outputs; it never changes | framework | backend, at submission |
| reference | an input that points to an output of a record, or to a dataset | framework | notebook, app |
| label, member | names under which records are found later | | notebook, app |
| template | a spec with values for some parameters; the others (*blanks*) are filled later | framework | notebook, app |
| dataset source | where a backend finds datasets: it resolves names and reads data through it, and answers its clients' queries from it | framework | operator; a fake one in tests |
| accumulator spec | a spec that combines a list of values into one, such as a sum | workflow author | workflow author |
| session | a `with` block in which the backend keeps intermediate values in memory | framework | notebook, app |
| stage | a template in a session; what does not depend on the blanks is computed once | framework | notebook, app |
| accumulator | a running combination in a session, such as a sum, to which records are added one at a time | framework | notebook, app |
| snapshot | a record of an accumulator's current value | framework | backend, when an accumulator is submitted |
| driver | code that decides over time what to submit, such as a loop over new datasets | framework (`apply`, `TriggerLoop`), app author, notebook | notebook, app |

The rows down to template are enough for most work.
Stages and accumulators are both called *holders*, since both hold values in a session.
The terms stage, accumulator, and driver follow sciline (scipp/sciline ADR 0003).

## Client and backend

A client talks to one backend, for one proposal:

```python
client = connect('https://reduce.example', proposal='p1')   # a hosted backend
client = local(proposal='p1')                               # a backend in this process
```

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
def iofq(run, bins, beam_centre) -> dict:
    ...
    return {'iofq': result}
```

The backend passes every parameter, with the defaults of the params model filled in.
Defaults belong in the params model only; a default in the binding would never be used, and could disagree with the model.

A sciline pipeline becomes a binding by naming the sciline key that each parameter sets and the key that computes each output:

```python
PipelineBinding(pipeline,
                params={'run': Filename[SampleRun], 'bins': QBins, 'beam_centre': BeamCenter},
                outputs={'iofq': BackgroundSubtractedIofQ})
```

A backend in the notebook's process can bind a spec to code defined in the notebook, for example to try out a change to a workflow:

```python
local(proposal='p1', bind={IOFQ: draft})
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
The first output is kept at least until the second request has read it (see How long records and values are kept).
An output can be passed to a parameter if both are data fields of the same format and, where both declare an `ArraySpec`, the same dims and unit.
Otherwise the request is refused at submission.

Several requests that refer to each other can be submitted in one call.
`Request(spec, params)` makes a request without submitting it.
A reference to a request in the same call becomes a reference to its record:

```python
centre = Request(BEAM_CENTRE, {'run': centre_run})
samples = {name: Request(IOFQ, {'run': run, 'beam_centre': centre.ref('centre')})
           for name, run in sample_runs.items()}
records = client.submit({'centre': centre, **samples})    # pending records, same keys
```

Chain requests where the intermediate result is worth having as a result of its own, such as a beam centre or a vanadium normalization.
Each step is a separate record.
To avoid recomputing within a single workflow, use a stage instead (see Sessions).

## Labels and members

A *label* names a sequence of records, such as all I(Q) reductions of an experiment; `client.latest` returns the newest.
A *member* splits a label further, one per sample or temperature.
Both are optional and are given at submission, not in the request, since they do not change the result.
A record shows both.

```python
client.compute(IOFQ, {'run': run, 'bins': 50}, label='iofq', member='250K')
client.submit({'250K': a, '260K': b}, label='iofq')       # with a label, dict keys become the members
client.latest('iofq', member='250K')
client.members('iofq')                   # {member: latest record}
client.labels()
client.records(label='iofq', since=monday, until=friday)
```

The loop from the start, with labels, so that another notebook finds the curves:

```python
for run in (60339, 60340, 60341):
    client.submit(IOFQ, {'run': dataset(run=run), 'bins': 100}, label='iofq', member=str(run))
```

A label holds no values; it only names records.

## Templates

A *template* is a spec, some parameter values, and the names of the parameters left open, its *blanks*.
Batches, stages, and rules are built from templates.
A template can be made from any request's values; naming a field as a blank drops the value given for it.

```python
template = Template(IOFQ, params={'bins': 100}, blanks=('run',))
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))  # reuse a tuned request
```

Templates are frozen dataclasses; `dataclasses.replace` makes a changed copy.

## How long records and values are kept

The backend keeps two kinds of things with different lifetimes:

| | What | Kept |
|---|---|---|
| record | what ran, with which inputs, and what came of it | for a retention period |
| output value | the data an output holds, such as an I(Q) array | only while something holds it, see below |

Records are kept for a retention period, together with the older records they depend on.
They are kept long because they are read long after the request ran: a batch's failures are read the next morning, a rule's progress by another user or program.
Output values are large, so an output value is kept only while

1. a pending request reads it,
2. a record in a client holds it, like a dask future holds its value,
3. a stage or accumulator in a session holds it (see Sessions), or
4. it has been saved.

A value that must outlive its client, such as a beam centre for tomorrow's batch, must be saved.
How to save is not designed yet.
Reading an output that is no longer kept raises an error, and a request that references it is refused at submission; the record itself remains.
What lasts beyond retention is what `publish` puts in a catalogue (see Provenance and publication).

## Datasets

**Dataset.** A request names a dataset by what a person knows, such as a run number.
The backend resolves it, and the record names the dataset's identity, not what was typed.

```python
dataset(run=60339)
dataset(path='/home/user/data/run1.h5')
dataset(pid='20.500.12269/vanadium')     # for example a result published elsewhere
```

**Dataset source.** A backend finds datasets through its *dataset source*, which the operator deploys with it, for example one backed by the facility's data catalogue.
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

## Combining runs: accumulator specs

Many reductions combine several runs into one result.
The counts of each angle of a rotation scan are summed into one volume.
A normalization sums numerators and denominators over runs and divides only at the end.

An *accumulator spec* is a spec for the combining step.
It is built around an *element model*: the fields of one value that is combined, such as an array of counts.
The params of an accumulator spec hold one list per element field; its outputs have the fields of the element model.
Since output and element have the same fields, a combined value can be combined again.

```python
class Counts(BaseModel):
    counts: Array(ArraySpec(dims=('Q', 'energy_transfer'), unit='counts'))

VOLUME = AccumulatorSpec(name='volume', version=1, element=Counts)          # bound to a sum

angles = [client.submit(ANGLE, {'run': r}) for r in scan]                    # one run per angle
volume = client.submit(VOLUME, {'counts': [a.ref('counts') for a in angles]})
client.submit(CUT, {'data': volume.ref('counts'), 'energy_transfer': 2.0})
```

An accumulator spec connects specs whose authors did not plan for each other, as long as their fields match.

An author may declare that the result does not depend on how the elements are grouped, as for a sum (how is open, see Open questions).
The backend may then compute a request over many elements in parts, on many processes; the record is the same.

A reduction with a sum in the middle splits into three specs.
A package derives them from its sciline `Aggregation`, sciline's description of such a split:

```text
run 611 ── CONTRIBUTE ──┐
run 612 ── CONTRIBUTE ──┼── PARTS_SUM ── FINALIZE ── I(Q)
run 613 ── CONTRIBUTE ──┘
```

```python
class NormalizationParts(BaseModel):           # the element model of PARTS_SUM
    numerator: Array(ArraySpec(dims=('Q',), unit='counts'))
    denominator: Array(ArraySpec(dims=('Q',), unit='counts'))

CONTRIBUTE = WorkflowSpec(name='sans-contribute', ..., params=ContributeParams, outputs=ContributeOutputs)
PARTS_SUM = AccumulatorSpec(name='sans-parts-sum', version=1, element=NormalizationParts)
FINALIZE = WorkflowSpec(name='sans-finalize', ..., params=FinalizeParams, outputs=IofQOutputs)
```

`ContributeOutputs` has the fields `numerator` and `denominator`, and may have more, such as a transmission per run, which are not summed.
`FinalizeParams` has the data fields `numerator` and `denominator`; FINALIZE does not know that they are sums.
Position i of each list in a PARTS_SUM request refers to the same run.

Which quantity is summed changes the result: summing counts and normalizing once is not the same as averaging normalized curves.
The workflow author decides this for the specs the package ships; whoever connects specs from different packages, in a notebook or an app, decides it for that chain.

### One sum, three ways

```python
# 1. a spec that sums internally over a list parameter
client.compute(NORMALIZE, {'runs': [r611, r612], 'scale': 2.0})   # NORMALIZE: such a spec

# 2. a chain of requests
parts = [client.submit(CONTRIBUTE, {'run': r}) for r in (r611, r612)]
total = client.submit(PARTS_SUM, {'numerator': [p.ref('numerator') for p in parts],
                                  'denominator': [p.ref('denominator') for p in parts]})
client.compute(FINALIZE, {**total.refs(), 'scale': 2.0})     # refs(): every output, by name

# 3. the same chain through an accumulator, see Sessions
```

Ways 2 and 3 give the same values. Way 1 makes one record of a different spec.
The framework allows all three; a package decides which specs it offers.

## Sessions: stages and accumulators

Without a session, every request computes everything from its inputs.
In interactive work this repeats work: tuning `bins` reloads the same run for each value, and adding one run to a sum of a hundred sums all hundred again.
A session lets the backend keep such values in memory between requests.

A *session* is a `with` block.
In it, a notebook makes stages and accumulators, together called *holders*.
Holders in one session share a process, so values pass between them in memory.
Ending the session releases them once the requests made through them have run.
Records made in a session are ordinary records and outlive it.

A session runs in a backend process unless placed elsewhere, for example next to a desktop application with `client.session(where='local')`; its records still go to the backend.

**Stage.** A *stage* is a template held in a session.
The backend computes what does not depend on the blanks once, such as loading the run, and each call through the stage computes only the rest:

```python
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))   # loads the run once
    for bins in (50, 100, 200):
        client.compute(tune, {'bins': bins}, label='iofq')
```

A record made through a stage is the record of the plain request with the blanks filled; it does not mention the stage.

For a stage, the backend calls the binding in two steps:

```python
call = binding.stage(fixed, blanks)   # fixed: {name: value} that stay the same; blanks: names left open
call(bins=50)                         # values for the blanks; called once per request through the stage
```

A request outside a stage is the case with no blanks: `binding.stage(values, ())` and then one call without arguments.
A function binding computes everything in each call.
A `PipelineBinding` computes what does not depend on the blanks once, through `sciline.Stage`, and reuses it in later calls.

**Accumulator.** An *accumulator* holds the combined value of the elements pushed into it so far.
A request of an accumulator spec needs the whole list at submission; an accumulator takes one element at a time and does not combine the earlier ones again.
The accumulator spec's binding must provide `accumulator()`, as `combine(operator.add)` does; [system.md](system.md) describes the protocol.

```python
total = session.accumulator(PARTS_SUM)
total.push(record)          # pushes record's outputs named like the element's fields
```

A push takes the record's outputs named like the element's fields; its other outputs are not pushed.
The push waits for the record to finish and refuses it unless it has completed.
A push is checked when made, as a request over that one element would be.

**Snapshot.** To use the combined value as the input of another request, submit the accumulator.
This makes a *snapshot*: a record whose output is the current combined value, completed at once.
Like any record, its outputs can be referenced.
A snapshot takes no label or member; the requests that read it do.

Way 3 of the sum above:

```python
with client.session() as session:
    contribute = session.stage(Template(CONTRIBUTE, blanks=('run',)))
    finalize = session.stage(Template(FINALIZE, params={'scale': 2.0},
                                      blanks=('numerator', 'denominator')))
    total = session.accumulator(PARTS_SUM)

    for run in (r611, r612):
        total.push(client.compute(contribute, {'run': run}))
    first = client.compute(finalize, client.compute(total).refs(), label='sum')

    total.push(client.compute(contribute, {'run': r613}))      # r611 and r612 are not reduced again
    added = client.compute(finalize, client.compute(total).refs(), label='sum')
```

A snapshot's value is the output of the plain request over the elements pushed so far, in push order.
For the second snapshot this is `PARTS_SUM(numerator=[c611.numerator, c612.numerator, c613.numerator], denominator=[...])`, the request that way 2 makes, where `c611` is the CONTRIBUTE record of run `r611`.
A snapshot's record names the accumulator and how many elements it covers, not the elements themselves; provenance lists them:

```python
snapshot = client.compute(total)
snapshot.submitted                       # what a record ran: a Request, or here a Snapshot
                                         # Snapshot(spec=sans-parts-sum/v1, accumulator=total.id, upto=3)
client.provenance(snapshot).records()    # [c611, c612, c613]: the records it read, in push order
```

## Drivers

The backend runs requests and keeps records; it does not decide what to run.
Deciding over time what to submit, what to push into an accumulator, and what to keep in a session is the job of a *driver*: ordinary code that uses the client.
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
with client.session() as session:
    volume = session.accumulator(VOLUME)
    angles = (client.submit(ANGLE, {'run': run}) for run in datasets.watch(Selector(scan='17')))
    for angle in client.as_completed(angles):                   # in the order they finish
        volume.push(angle)
        client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'energy_transfer': 2.0},
                      label='cut', member='17')
```

The generator `angles` submits a request for each run as it arrives.
`as_completed` consumes it in a thread, so submitting does not wait for the loop body.
It yields each record once it has finished, so the angles are reduced in parallel while the loop pushes one at a time.

## Batch and automatic reduction

This sub-design builds on the core. It needs from it only that records show their label and member, and `client.datasets`.
[automatic-reduction.md](automatic-reduction.md) describes it.

A *lookup* fills further blanks per dataset.
For example, a sample needs the empty-can run measured most recently before it:

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, datasets, lookup=cans)
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
It reads which datasets it has handled from the records under each rule's label, so a restarted loop needs no memory of its own.
Templates and rules serialize to JSON; the core keeps no store of them, and records do not name them.

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
Corrections that supersede a published entry, and recomputing in a record's environment, come later.

## Guarantees

- A record holds the spec, every parameter value including defaults, and its inputs by reference; a snapshot holds its accumulator and how many elements it covers. A record never changes; its status changes once, from pending to finished.
- A stage never changes what a record says: a record made through a stage is the record of the plain request. A snapshot's value is the value of the plain request over the elements it covers, in push order.
- Every connection between requests is a reference. A value passed in memory is the referenced output itself, so a workflow must not modify its inputs.
- A record's outputs do not depend on how they were computed: through holders, on another machine, or as a tree over many processes. Values may differ in rounding where the order of combining differs.
- The provenance of a record reaches every dataset it read, through all its inputs, with their parameter values and software versions.
- Records are kept for a retention period, together with the older records they depend on. A published entry answers what produced it without access to the records.
- An output's value is kept only while a pending request reads it, a record in a client holds it, or a holder in a session holds it, or once it is saved. Ending a session releases its holders once the requests made through them have run.

## Left to the system

Not part of this API, and not visible in the code of notebooks, apps, or workflow packages:

- how history is stored and for how long, and how outputs are stored, copied, dropped, and located ([system.md](system.md))
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- how a request of an accumulator spec over many elements is split into parts, such as a tree of partial sums, and how that is configured
- where a session's process runs by default, whether a value passed in memory is also written, and how a session whose client is gone is ended
- how access across proposals is enforced

## Open questions

1. **Grouping.** How an author declares that grouping does not change the result of an accumulator spec, which a tree of partial sums over a plain request needs.
2. **Sessions.** Whether a holder can exist without a session that a notebook or app opened; how the trigger loop owns one, for a sum that grows with each new dataset under a rule; the name and values of the placement argument.
3. **Removing an element.** A request of the accumulator spec over fewer elements is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each element.
4. **Labels and members** on records, `member_field`, and `client.members` are tentative.
5. **Views.** Reading part of an output, such as one cut through a volume, quickly and without making a record. The form waits for the plotting work.

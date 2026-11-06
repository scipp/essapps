# ESS data-reduction framework

**Status: the in-process backend (`local()`) implements this design, including freeze, `select=`, and persist to a fake store. Not implemented: the service, `client.publish`, and software versions in provenance. The first release combines no runs and tunes no parameters interactively, so it needs neither stages nor accumulators.**

This page gives an overview of the framework: its components, what each promises the others, and the ideas that client code is built from.
The details live one level down:

| To learn | Read |
|---|---|
| the architecture, as a crash course | [tours/layers.html](tours/layers.html) |
| how to write a workflow, a client, or the framework, on a running example | [roles/](roles/README.md) |
| how to write a spec: field types, files, references, unions, defaults | [packages/essspec/docs/params.md](../../packages/essspec/docs/params.md) |
| how to write a binding: a function, a sciline pipeline, or a driver | [packages/essspec/docs/bindings.md](../../packages/essspec/docs/bindings.md) |
| stages, accumulators, and their symmetries | [accumulators.md](accumulators.md) |
| batch and automatic reduction | [automatic-reduction.md](automatic-reduction.md) |
| the rules behind each call: what is kept and readable, when a call returns, what it raises | [`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py) |
| the shapes and shortcuts of the client | the docstrings of `ess.apps.client` |
| how the code is built | the module docstrings |
| why it is so | the [ADRs](adr/index.md), and those of [essspec](../../packages/essspec/docs/developer/adr/) |
| what it must do | [the requirements](../requirements/README.md), [user-stories.md](user-stories.md), [system-stories.md](system-stories.md) |
| how other systems do it | [prior-art/](prior-art/README.md) |

## From a for loop

A notebook without the framework:

```python
curves = {}
for run in (60339, 60340, 60341):
    curves[run] = reduce_iofq(run, can=60338, transmission=transmission, bins=100)
```

The same with the framework:

```python
records = {}
for run in (60339, 60340, 60341):
    records[run] = client.submit(IOFQ, params={'reduction': Reduction(bins=100),
                                               'sample': file(run=run),
                                               'can': file(run=60338),
                                               'transmission': t.ref('transmission')})
client.wait(records)
client.output(records[60339], 'iofq')
```

`IOFQ` is the *spec* of the I(Q) reduction: it names the reduction's parameters and outputs.
`file(run=run)` names the raw data of a run.
`t` is the record of an earlier transmission reduction, and `t.ref('transmission')` passes its output to each reduction.
`client.submit` returns a *record* at once, and the three reductions run in parallel, possibly on another machine.

What the framework adds to the plain loop:

- Each call is kept as a record. The same process finds it later, and on the service, other notebooks and programs too.
- Output values are kept for the client that asked, and for every client of the proposal once persisted.
- Calls can run elsewhere and in parallel.
- Every output can answer where it came from: which spec, which parameter values, which files, which software versions.

## Components

```
  controller: a notebook, an app, the trigger loop      decides what to run
        │  client: submit, push, output, release, ...
        ▼
  backend ───── file source: files and their metadata
        │  └─── store: persisted outputs
        │  binding: compute, make_stage, make_accumulator
        ▼
  workflow package: specs, and a pipeline, driver, or function per spec
```

The framework is two packages ([ADR 0007](adr/0007-packages-split-by-dependencies.md)).
`ess.spec` holds what a workflow package needs: specs, bindings, and checks for tests.
`ess.apps` holds the client, the backend, and apps built on them, such as the forms of `ess.apps.panel`.

**Spec.** A workflow package declares a spec, `ess.spec.WorkflowSpec`, for each workflow: a name, a version, and two pydantic models, *params* and *outputs*.
The spec is all that a client, a form, and the backend know of a workflow; none of them imports the workflow's code.
A parameter is written inline, as a file, or as a reference to another record's output, as its type says ([params.md](../../packages/essspec/docs/params.md)).

**Binding.** A *binding* is what the backend calls to compute a spec's requests.
The workflow author makes it from one of three things, and each serves some of the client's calls:

| The author provides | Plain request | Stage | Accumulator |
|---|---|---|---|
| a sciline pipeline, `PipelineBinding` | yes | yes | refused |
| a driver, `DriverBinding` | yes | refused | yes |
| a function, `function_binding` | yes | refused | refused |

The binding gets an instance of the params model, with each file as a local path and each reference replaced by the data it names, and returns an instance of the outputs model.
It promises not to modify what it gets, and not to return an output that shares memory with it.
[bindings.md](../../packages/essspec/docs/bindings.md) shows how to write each.

**Backend.** The *backend* runs requests and keeps records, for one or more proposals (an experiment at ESS).
There are two deployments:

```python
client = local(proposal='p1', files=..., bind=[iofq, ...], store=...)   # in the user's process
client = connect('https://reduce.example', proposal='p1')              # the service, hosted by DMSC
```

`local` is for interactive work, and can bind a spec to code defined in the notebook.
Batch and automatic reduction run on the *service*, which runs only workflows from installed packages.
The backend decides when and where a request runs; the caller sees the same records and outputs either way.

**Client.** A *client* is one connection to one backend, for one proposal.
Every call that starts work returns once the backend has *logged* it in the proposal's history; the work happens later.

**File source.** The backend finds files, and their metadata, through its *file source*, which DMSC deploys with it.
A client sees only the files its proposal may read:

```python
samples = client.files.list(Selector(role='sample'))
for run in client.files.watch(Selector(scan='17')): ...   # existing ones first, then new ones
```

**Store.** The *store* keeps persisted outputs outside the backend's memory, for every client of the proposal ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

**Controller.** The backend does not decide what to run.
A *controller* is client code that decides over time what to submit, push, and release, such as a notebook, an app's UI, or the trigger loop of automatic reduction.
Controllers never run in the backend:

```python
for run in client.files.watch(Selector(role='transmission')):
    record = client.submit(TRANSMISSION, params={'run': run, 'empty_beam': file(run=60336)})
    show(client.output(record, 'transmission'))
```

## Requests and records

A *request* is a spec and its params.
Submitting it returns a *record*: the request as the backend logged it, with every value filled in, defaults included.
A record never changes, only its status: `pending`, then `completed`, `failed`, or `cancelled`.
Running the same request again makes a new record.

```python
result = client.compute(IOFQ, params={...})          # submit, then wait
client.output(result, 'iofq')                        # raises RecordFailed if the record failed
client.output(result, 'iofq', select={'Q': slice(0, 10)})   # copies a part; on the service only that part leaves
client.describe(result, 'iofq')                      # {'intensity': ArrayInfo(sizes, unit, coords)}, no data
client.failure(result)                               # why it failed, or None
```

An application learns what an output is from `client.describe`, which gives the sizes, unit, and coordinate names of each array field and reads no array, and then reads the part it shows with `select=`.
A value that was persisted and is no longer in memory is read from the store to be described.
The plot of an array of more than two dimensions has a slider for each dimension but the last two, and moving it reads one cut (`ess.apps.panel.Outputs`; which dimensions get a slider is decided in `ess.apps.plotting`, which needs no widget library).
A workflow package can configure plots for its spec: `App(client, plots={spec_id: {'Centre on image': function}})`, where a function takes an instance of the spec's outputs model and returns a plopp figure.

A request is checked in two phases:

- The *request phase*, at submission, before any data is read: each value is validated by its type, files are resolved and access-checked, and each reference is checked to be readable and to fit its field. A request that cannot run is refused with a `SubmitError`, which names the position of each problem, such as `reduction.bins`.
- The *call phase*, when the backend reads the data and calls the binding. A failure here fails the record.

Validation in the request phase never proves that a workflow will succeed.

**References.** `record.ref(name)` names an output of a record.
A reference to a pending record is allowed, so a chain is submitted without waiting, and the backend runs the request once the record it reads has completed:

```python
centre = client.submit(BEAM_CENTRE, params={'direct_beam': file(run=60330)})   # pending
result = client.submit(IOFQ, params={..., 'beam_centre': centre.ref('centre')})
```

A reference fills only a field that the spec types `Referenced[T]`, and the output must be a `T`.

**Labels and members.** A *label* names a sequence of records, such as all I(Q) reductions of an experiment, and a *member* one part of it, such as one sample.
Both are given at submission, not in the request:

```python
client.submit({'250K': request_250k, '260K': request_260k}, label='iofq')   # keys become members
client.latest('iofq', member='250K')
client.records(label='iofq')                         # oldest first
```

**Templates.** A *template* is a spec with *fixed values* for some parameters and *blanks* for the others.
Stages, accumulators, and automatic reduction are built from templates.
A template can be made from any record, such as the final reduction of one sample with its sample left open:

```python
final = client.latest('iofq', member='250K')
beamtime = Template(final.request.spec, params=final.request.params, blanks=('sample',))
```

## Who keeps a value

A record is kept with the proposal's history.
An output value lives only while something keeps it: the client that asked for it, a pending request that reads it, or the store once persisted.
A client releases what it no longer needs, and work whose outputs nothing would keep is cancelled.
Another client reads a value only once it is persisted ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)):

```python
client.release(centre)              # this client no longer keeps the centre
client.submit(IOFQ, params={...}, persist=('iofq',))  # the store keeps iofq, for every client of the proposal
```

## What changed

A client that shows records, or reacts to them, asks which records changed since a position in the proposal's history, and reads only those.
A record changes when it is submitted, by any client of the proposal, and when it finishes.
The reply holds record IDs and no values, each ID once:

```python
cursor = 0                                  # 0 gives every record of the proposal
while True:
    changes = client.changes(after=cursor, timeout=30)  # waits up to 30 s for a change
    cursor = changes.cursor                 # pass it next time
    for record in client.records(ids=changes.records):
        show(record, client.status(record), client.readable(record))
```

A backend that starts from its history log gives the same changes at the same cursors.
Accumulators and pushes are not in the feed.
Neither is `client.release`, nor any other way for a value to go away: the history does not hold lifetimes, and what a client may read is per client.
So a client that shows outputs asks `client.readable` for the records it shows, and a read can still raise `NotReadable`.

## Stages and accumulators

A *stage* keeps the part of a computation that depends only on its fixed values, so that tuning a parameter does not load the runs again.
An *accumulator* keeps a sum over runs, so that adding a run does not reduce the earlier ones.
Each call through either makes the record of the equivalent plain request ([accumulators.md](accumulators.md)):

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in islice(client.files.watch(Selector(scan='17')), 300):
    volume.push(runs=[run])                                 # one more run in the sum
    show(client.output(volume, 'volume', select={'q': 0}))  # the sum so far
total = client.freeze(volume)                               # the final sum, as a record
```

## Batch and automatic reduction

`apply` fills a template once per file, and a *rule* does so for each new file as it arrives, under a label, persisted ([automatic-reduction.md](automatic-reduction.md)):

```python
rule = Rule('transmission', Template(TRANSMISSION, blanks=('run', 'empty_beam')),
            selector=Selector(role='transmission'), at='run', label='transmission',
            lookup=Lookup({'empty_beam': LastBefore(Selector(role='empty_beam'))}))
TriggerLoop(client, rules=[rule]).run()
```

The loop reads which files a rule has handled from the changes, so a step reads only the records that changed since the last one.

## Provenance and publication

```python
provenance = client.provenance(result)
provenance.files(), provenance.records()
provenance.software                                    # not implemented
pid = client.publish(result.ref('iofq'), 'scicat')     # not implemented: the output and its provenance
```

Provenance lists the record's request, every record that supplied a value by reference, directly or through other records, the files they used, and the software versions.
It stops at files.
Publishing puts an output in a catalogue with its provenance, where it stays after the proposal's history is dropped.

## Left to the system

Not visible in the code of notebooks, apps, or workflow packages:

- how history and outputs are stored, moved, and dropped
- how a run number or path becomes a file identity
- when and where a request runs, and where a stage or a driver lives
- the format of persisted outputs and the store's layout
- how the service notices a vanished client and sets the cap per client (scipp/essapps#27)
- how access across proposals is enforced

## Open questions

1. **Grouping.** Spreading one accumulator over several nodes needs a merge of two drivers. No requirement asks for it ([ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md)).
2. **Removing a row.** Whether an accumulator offers `remove` depends on whether it keeps each row.
3. **Labels and members** on records, and `member_field`, are tentative.
4. **Looks that compute.** `client.describe` and `select=` give an application the sizes of an output and a cut of it. `select=` only indexes. A projection, or a thick slice summed, would run a spec next to the value without making a record; it is still open whether that is a second call or part of `select=`. So is reading the latest state already added, without a pin, and describing an accumulator's state, which `describe` does not take. A configured plot function takes every output of its record, read whole; whether it may take cuts is open.
5. **Accumulators on the service.** How the service holds a driver, bounds its memory, and drops it ([ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md), scipp/essapps#27).
6. **Persist at submission.** Should `persist=` at submission also keep the values for the client? To be decided when a story needs it.
7. **Values modified in place.** In the user's process, a notebook that modifies a value `client.output` returned changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.

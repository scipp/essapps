# ESS data-reduction framework: the API

**Status: the design of the API. Implementation starts with the core.**

[scoping.md](scoping.md) states the goals. [user-stories.md](user-stories.md) holds the stories this API must express, and [system-stories.md](system-stories.md) what the system must provide beyond it.

This document describes the API we want: what users and workflow authors write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in a separate system document, and the system may change them without changing any code shown here.

The core, described here in full, is specs, requests, records, references, labels, datasets, and the holders in a session.
Two sub-designs build on it and get one section each here: batch and automatic reduction, and provenance and publication.

## From a for loop

A notebook today:

```python
curves = {}
for run in (60339, 60340, 60341):
    curves[run] = reduce_iofq(run, bins=100)
```

The same with the framework:

```python
for run in (60339, 60340, 60341):
    client.submit(IOFQ, {'run': dataset(run=run), 'bins': 100}, label='iofq', member=str(run))
```

What changes: each call is kept and can be found later, calls can run elsewhere and in parallel, and every result can answer where it came from.

A client talks to one backend, for one proposal:

```python
client = connect('https://reduce.example', proposal='p1')   # a hosted backend
client = local(proposal='p1')                               # a backend in this process
```

A hosted backend runs only installed workflows.
A backend in the notebook's own process may also run a workflow defined in the notebook, `local(proposal='p1', bind={IOFQ: draft})`, and its records say that the implementation was bound there.

## Overview

| Kind | Terms |
|---|---|
| plain data | spec, request, template, reference, selector |
| durable | record, label |
| queries | dataset source |
| holders, in a session | stage, accumulator |
| policy | driver |

The backend executes requests and keeps records.
A dataset source answers which datasets exist.
Holders live in a backend process for as long as their session.
Drivers are loops written against the client and a dataset source; they run in a notebook, an application, or a long-lived driving server, never in the backend.
The sections below follow sciline's terms (scipp/sciline ADR 0003): stage, accumulator, contribution, driver.

## Specs, requests, records

**Spec.** A workflow package declares a spec (`ess.reduce.spec.WorkflowSpec`): a name, a version, a params model, and an outputs model.
Both models are pydantic models over one vocabulary, in which a field holding data is a reference.
Users run specs; how the package implements a spec is invisible to them.

```python
class IofQOutputs(BaseModel):
    iofq: Annotated[Ref, DataField(Format.SCIPP, ArraySpec(dims=('Q',), unit='dimensionless'))]

IOFQ = WorkflowSpec(name='sans-iofq', version=1, title='I(Q)', description='...',
                    params=IofQParams, outputs=IofQOutputs)
```

An intermediate value is visible only if the author declares it as an output; a request does not select outputs.
Other intermediates are inspected by running the package's workflow in a notebook.

**Request and record.** A request is a spec and its parameter values.
Submitting it returns a record: the request with every value filled in, defaults included, plus status and outputs.
A record's status is `pending`, `completed`, `failed`, or `cancelled`. A finished record never changes; a rerun is a new record.

```python
result = client.compute(IOFQ, {'run': dataset(run=60339), 'bins': 100})   # submit and wait
result.request.params                    # every value, defaults included
result.created, result.status            # a failed record also has result.failure.message
client.output(result, 'iofq')
```

`client.wait(records)` returns failed records rather than raising.
`client.cancel(records)` ends the unfinished ones as `cancelled`.
Both take a record, a list, or a dict of records, like `client.submit`, and `wait` returns the same shape.
A request that cannot run is refused at submission with a `SubmitError` naming the field at fault, before any record exists: an invalid value, an unknown spec version, an unknown run number, or a reference the submitter may not read.

Records are working state for running experiments and are kept for the medium term.
They outlive sessions: most requests run without one, and a batch's failures, a rule's progress, and a beam centre for tomorrow's batch are read later, by other users or programs.
What lasts is what `publish` puts in the catalogue.
Reading an output the system has dropped raises an error, and a request that references it is refused at submission.

**Reference.** An input is named by reference: an output of a record, or a dataset.
An output field fulfils a params field when the two agree.
A reference to a record that has not finished is a valid input, so a chain is submitted without waiting.

```python
centre = client.submit(BEAM_CENTRE, {'run': dataset(run=60330)})   # pending
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
```

Chain where the intermediate result is worth keeping by itself, such as a beam centre or a vanadium normalization.
Outside a session, nothing keeps a value in memory from one request to the next: the second request reads the first one's output back.

`client.submit` takes a request, a list, or a dict of requests, and returns pending records in the same shape.
Requests submitted together are checked together: if one is invalid, none is submitted.
A reference to a request in the same call becomes a reference to its record.

```python
centre = Request(BEAM_CENTRE, {'run': centre_run})
samples = {name: Request(IOFQ, {'run': run, 'beam_centre': centre.ref('centre')})
           for name, run in sample_runs.items()}
records = client.submit({'centre': centre, **samples})    # pending records, same keys
```

**Label.** A label names a sequence of records; the latest is the current value.
A member splits a label, one per sample or temperature.
Label and member are given at submission, not in the request, since they do not change the result; a record shows both.
Submitting a dict of requests under a label makes each key the member of its record.

```python
client.compute(IOFQ, {'run': run, 'bins': 50}, label='iofq', member='250K')
client.submit({'250K': a, '260K': b}, label='iofq')       # members '250K' and '260K'
client.latest('iofq', member='250K')
client.members('iofq')                   # {member: latest record}
client.labels()
client.records(label='iofq', since=monday, until=friday)
```

**Template.** A template is a spec, some values, and blanks. Templates, like rules, are frozen dataclasses; `dataclasses.replace` changes them.
It can be made from any request's values; naming a field as a blank drops the value given for it.

```python
template = Template(IOFQ, params={'bins': 100}, blanks=('run',))
beamtime = Template(final.request.spec, params=final.request.params, blanks=('run',))
```

**View.** A view reads part of an output, such as one cut through a volume, quickly and without making a record.
Its form waits for the plotting work.

## Datasets

**Dataset.** A request names a dataset by what a person knows; the record names the dataset's identity, not what was typed.

```python
dataset(run=60339)
dataset(path='/home/user/data/run1.h5')
dataset(pid='20.500.12269/vanadium')     # for example a result published elsewhere
```

**Dataset source.** Listing datasets, waiting for new ones, and reading their metadata are queries of a dataset source, an object separate from the client.
Forms and drivers take a dataset source next to the client; a test gives them a fake one, and a deployment may serve it as it serves the backend.
A dataset has a kind, such as raw, derived, mask, or calibration, and a selector matches raw datasets unless it names another kind.

```python
datasets = catalogue(instrument='loki', proposal='p1')
samples = datasets.list(Selector(role='sample'))
for run in datasets.watch(Selector(scan='17')): ...   # existing ones first, then new ones, each once
datasets.metadata(run)['sample']                        # the catalogue's current values
```

## Accumulator specs

An accumulator spec combines a list of elements into one value.
It declares an element model, whose fields are data fields.
Its params model has one list per element field, and its outputs model is the element model.
Because the outputs model is the element model, a combined value can be pushed again.
An author may declare that the result does not depend on how the elements are grouped.
The backend may then compute a request over many elements in parts, on many processes; the record is the same.

A package derives three specs from its sciline `Aggregation`: one that computes the contribution of one member, one that accumulates contributions, and one that finalizes the combined value.

```python
class NormalizationParts(BaseModel):           # the element, and PARTS_SUM's outputs model
    numerator: Array(ArraySpec(dims=('Q',), unit='counts'))
    denominator: Array(ArraySpec(dims=('Q',), unit='counts'))

CONTRIBUTE = WorkflowSpec(name='sans-contribute', ..., params=ContributeParams, outputs=ContributeOutputs)
PARTS_SUM = AccumulatorSpec(name='sans-parts-sum', version=1, element=NormalizationParts)
FINALIZE = WorkflowSpec(name='sans-finalize', ..., params=FinalizeParams, outputs=IofQOutputs)
```

`ContributeOutputs` has the fields `numerator` and `denominator`, and may have more, such as a transmission per run, which are not accumulated.
`FinalizeParams` has the data fields `numerator` and `denominator`; FINALIZE does not know that they are sums.
Both fields accumulate from the same members: position i of each list names the same record.

A generic accumulator spec, such as `SUM` from ess.reduce, takes its element model when it is used.
It connects specs whose authors did not plan for each other, when their fields agree:

```python
class Counts(BaseModel):
    counts: Array(ArraySpec(dims=('Q', 'energy_transfer'), unit='counts'))

angles = [client.submit(ANGLE, {'run': r}) for r in scan]                    # one run per angle
volume = client.submit(SUM.of(Counts), {'counts': [a.ref('counts') for a in angles]})
client.submit(CUT, {'data': volume.ref('counts'), 'energy_transfer': 2.0})
```

Where values accumulate changes the result.
It is decided by whoever writes the chain: the package author for the specs it ships, the user for a connection of specs from different packages.

### One sum, three ways

```python
# 1. a spec that sums internally over a list parameter
client.compute(NORMALIZE, {'runs': [r611, r612], 'scale': 2.0})

# 2. a chain of requests
parts = [client.submit(CONTRIBUTE, {'run': r}) for r in (r611, r612)]
total = client.submit(PARTS_SUM, {'numerator': [p.ref('numerator') for p in parts],
                                  'denominator': [p.ref('denominator') for p in parts]})
client.compute(FINALIZE, {**total.refs(), 'scale': 2.0})     # refs(): every output, by name

# 3. the same chain through holders, see below
```

Ways 2 and 3 make the same records; way 1 makes one record of a different spec.
The framework does not prevent any of them; a package decides which specs it offers.

## Holders

A holder keeps something in memory so that the next call computes less.
There are two, and both live in a session.
Holders in one session share a process, so values pass between them in memory.
Ending the session releases them.
A session runs in a backend process unless it is placed elsewhere, for example next to a desktop application with `client.session(where='local')`; its records still go to the backend.

**Stage.** A stage holds a template with everything that does not depend on its blanks computed.
A call through a stage computes only the rest.

```python
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))   # loads the run once
    for bins in (50, 100, 200):
        client.compute(tune, {'bins': bins}, label='iofq')
```

**Accumulator.** An accumulator holds the combined value of the elements pushed into it.
Pushing a record pushes its outputs named like the element's fields; its other outputs are not pushed.
Computing an accumulator makes a record of its accumulator spec over the elements pushed so far.
A pushed record may still be pending; `client.submit(total)` returns the record at once, pending until every element is done.

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

The second `client.compute(total)` makes a record of `PARTS_SUM(numerator=[c611.numerator, c612.numerator, c613.numerator], denominator=[...])`.
It is the record that way 2 makes; the accumulator only computes it faster.

## Drivers

A driver is code that uses the client over time: it decides what to submit, what to push into an accumulator, and what to hold.
A notebook is a driver, and so is an application.
Functions such as `apply` only build requests; the driver is the code that calls them.

**A batch** is one submission of many requests.
`apply` fills a template for each dataset and returns the requests keyed by member, the value of a metadata field that it reads from the dataset source.
It builds plain data and needs no client; the notebook that submits the requests is the driver.

```python
requests = apply(template, samples, datasets, member_field='temperature')
records = client.submit(requests, label='scan')
records['250K']
```

**A loop over arrivals** waits for new datasets.
This one reduces each angle of a rotation scan wherever the backend runs it, and keeps a volume of the angles so far:

```python
with client.session() as session:
    volume = session.accumulator(SUM.of(Counts))
    for run in datasets.watch(Selector(scan='17')):
        volume.push(client.submit(ANGLE, {'run': run}))          # pending; combined once done
        client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'energy_transfer': 2.0},
                      label='cut', member='17')
```

## Batch and automatic reduction

This sub-design builds on the core and needs from it only that records show their label and member, and a dataset source. It is described in [automatic-reduction.md](automatic-reduction.md).

A **rule** is plain data: a template, a selector, and a label.
A lookup fills a blank per dataset, such as the can measured most recently before a sample.
A series collects every dataset with the same value of a field into the template's list blank, in run order, one member per value; each arrival of an angle then stitches all angles of that sample so far.

```python
cans = Lookup(can=LastBefore(Selector(role='can')))
requests = apply(Template(IOFQ, blanks=('run', 'can')), samples, datasets, member_field='run', lookup=cans)

rule = Rule('reflectivity', Template(STITCH, params={'reference': reference}, blanks=('runs',)),
            selector=Selector(role='sample'), series='sample', label='reflectivity')
TriggerLoop(client, datasets, rules=[rule]).run()
```

The trigger loop is the driver for rules. It runs in a driving server, which has its own API to add, replace, and list rules.
It reads which datasets it has handled from the records under each rule's label, so a restarted loop needs no memory of its own.
Templates and rules serialize to JSON; the core keeps no store of them, and records do not name them.

## Provenance and publication

This sub-design builds on the core.

`client.provenance(record)` is plain data: the record's request, the records it read through all its inputs, the datasets they read, and the software versions.
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

- A record holds the spec, every parameter value including defaults, and its inputs by reference. A finished record never changes.
- A holder never changes what a record says. A record made through a stage or an accumulator is the record of the plain request.
- Every connection between requests is a reference. A value passed in memory is a copy of the referenced output.
- A record's outputs do not depend on how they were computed: through holders, on another machine, or as a tree over many processes. Values may differ in rounding where the order of combining differs.
- The provenance of a record reaches every dataset it read, through all its inputs, with their parameter values and software versions.
- Records are kept for the medium term. A published entry answers what produced it without access to the records.
- Only holders keep memory on a user's behalf, and ending their session releases them.

## Left to the system

Not part of this API, and not visible in user code:

- how records and outputs are stored, copied, dropped, and located, and for how long; the store may be as plain as output files with their requests next to them and an index for labels, pending requests, and failures
- how records that share most of their references are stored without repeating them
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- how a request of an accumulator spec over many elements is split into parts, such as a tree of partial sums, and how that is configured
- where a session's process runs by default, whether a value passed in memory is also written, and how a session whose client is gone is ended
- how access across proposals is enforced

## Open questions

1. **Generic accumulator specs.** How the element model appears in a record, so that `SUM.of(Counts)` and `SUM.of(NormalizationParts)` are told apart; and how an author declares that grouping does not change the result. Until decided, the implementation puts the element model's name in the spec's name, `sum[Counts]`.
2. **Sessions.** Whether a holder can exist without a session the user opened; how the trigger loop owns one, for a sum that grows with each new dataset under a rule; the name and values of the placement argument.
3. **Removing a member.** A record of the accumulator spec over fewer parts is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each contribution.
4. **Dataset sources.** Where a notebook gets its dataset source, and whether it must agree with the one the backend uses to resolve names.
5. **Labels and members** on records, `member_field`, and `client.members` are tentative.

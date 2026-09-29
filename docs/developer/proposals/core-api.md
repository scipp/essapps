# Proposal: the core API

**Status: draft for discussion. Nothing here is implemented.**

This document describes the API we want: what users and workflow authors write, and what they can rely on.
It leaves out how the system provides it: how results are stored, how run numbers become dataset identities, how data is moved, and where and in which order things run.
Those belong in a separate system document, and the system may change them without changing any code shown here.

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

## Overview

| Kind | Terms |
|---|---|
| plain data | spec, request, template, rule, reference |
| durable | record, label |
| holders, in a session | stage, accumulator |
| policy | driver |

The backend executes requests and keeps records.
Holders live in a backend process for as long as their session.
Drivers are loops written against the client API; they run in a notebook, an application, or a long-lived driving server, never in the backend.
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

**Request and record.** A request is a spec and its parameter values.
Submitting it returns a record: the request with every value filled in, defaults included, plus status and outputs.
A record never changes. A rerun is a new record.

```python
result = client.run(IOFQ, {'run': dataset(run=60339), 'bins': 100})   # submit and wait
result.request.params                                                  # every value, defaults included
client.output(result, 'iofq')
```

**Reference.** An input is named by reference: an output of a record, or a dataset.
A dataset may be named by what a person knows, such as a run number; the record names the dataset itself.
An output field fulfils a params field when the two agree.
A reference to a record that has not finished is a valid input, so a chain is submitted without waiting.

```python
centre = client.submit(BEAM_CENTRE, {'run': dataset(run=60330)})   # pending
result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
client.provenance(result)                                           # reaches runs 60330 and 60339
```

Chain where the intermediate result is worth keeping by itself, such as a beam centre or a vanadium normalization.
Outside a session, nothing keeps a value in memory from one request to the next: the second request reads the first one's output back.

Requests submitted together are checked together: if one is invalid, none is submitted.

```python
centre = Request(BEAM_CENTRE, {'run': centre_run})
samples = [Request(IOFQ, {'run': r, 'beam_centre': centre.ref('centre')}) for r in sample_runs]
client.submit([centre, *samples])
```

**Label.** A label names a sequence of records; the latest is the current value.
A member splits a label, one per sample or temperature.

```python
client.run(IOFQ, {'run': run, 'bins': 50}, label='iofq', member='250K')
client.latest('iofq', member='250K')
```

**Template.** A template is a request with blanks.

```python
template = Template(IOFQ, params={'bins': 100}, blanks=('run',))
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
client.run(NORMALIZE, {'runs': [r611, r612], 'scale': 2.0})

# 2. a chain of requests
parts = [client.submit(CONTRIBUTE, {'run': r}) for r in (r611, r612)]
total = client.submit(PARTS_SUM, {'numerator': [p.ref('numerator') for p in parts],
                                  'denominator': [p.ref('denominator') for p in parts]})
client.run(FINALIZE, {**total.refs(), 'scale': 2.0})     # refs(): every output, by name

# 3. the same chain through holders, see below
```

Ways 2 and 3 make the same records; way 1 makes one record of a different spec.
The framework does not prevent any of them; a package decides which specs it offers.

## Holders

A holder keeps something in memory so that the next call computes less.
There are two, and both live in a session.
Holders in one session share a process, so values pass between them in memory.
Ending the session releases them.

**Stage.** A stage holds a template with everything that does not depend on its blanks computed.
A call through a stage computes only the rest.

```python
with client.session() as session:
    tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))   # loads the run once
    for bins in (50, 100, 200):
        client.run(tune, {'bins': bins}, label='iofq')
```

**Accumulator.** An accumulator holds the combined value of the elements pushed into it.
Pushing a record pushes its outputs named like the element's fields; its other outputs are not pushed.
Running an accumulator makes a record of its accumulator spec over the elements pushed so far.
A pushed record may still be pending; `client.submit(total)` returns the record at once, pending until every element is done.

```python
with client.session() as session:
    contribute = session.stage(Template(CONTRIBUTE, blanks=('run',)))
    finalize = session.stage(Template(FINALIZE, params={'scale': 2.0},
                                      blanks=('numerator', 'denominator')))
    total = session.accumulator(PARTS_SUM)

    for run in (r611, r612):
        total.push(client.run(contribute, {'run': run}))
    first = client.run(finalize, client.run(total).refs(), label='sum')

    total.push(client.run(contribute, {'run': r613}))      # r611 and r612 are not reduced again
    added = client.run(finalize, client.run(total).refs(), label='sum')
```

The second `client.run(total)` makes a record of `PARTS_SUM(numerator=[c611.numerator, c612.numerator, c613.numerator], denominator=[...])`.
It is the record that way 2 makes; the accumulator only computes it faster.

## Drivers

A driver is code that uses the client over time: it decides what to submit, what to push into an accumulator, and what to hold.
A notebook is a driver, and so is an application.
Functions such as `apply` only build requests; the driver is the code that calls them.

**A batch** is one submission of many requests.
`apply` fills a template for each dataset; the notebook that submits the result is the driver.

```python
requests = client.apply(template, datasets, label='scan')   # plain data, one request per dataset
client.submit(requests)
```

**A loop over arrivals** waits for new datasets.
This one reduces each angle of a rotation scan wherever the backend runs it, and keeps a volume of the angles so far:

```python
with client.session() as session:
    volume = session.accumulator(SUM.of(Counts))
    for run in client.watch(Selector(scan='17')):              # waits for the next dataset
        volume.push(client.submit(ANGLE, {'run': run}))          # pending; combined once done
        client.submit(CUT, {'data': client.submit(volume).ref('counts'), 'energy_transfer': 2.0},
                      label='cut', member='17')
```

**A rule** is plain data: a template, a selector, and optionally a lookup that fills a parameter per dataset, such as the can measured before a sample.
The framework ships one driver for rules, the trigger loop, which runs the loop above for each rule it is given.
It runs in a driving server, not in the backend.

```python
rule = Rule('auto-iofq', template, selector=Selector(role='sample'), label='iofq')
TriggerLoop(client, [rule]).run()
```

## Publishing

Publishing an output puts it in the data catalogue, with its provenance.

```python
pid = client.publish(hist.ref('histogram'), 'scicat')
```

## Guarantees

- A record holds the spec, every parameter value including defaults, and its inputs by reference. It never changes.
- A holder never changes what a record says. A record made through a stage or an accumulator is the record of the plain request.
- Every connection between requests is a reference. A value passed in memory is a copy of the referenced output.
- A record's outputs do not depend on how they were computed: through holders, on another machine, or as a tree over many processes. Values may differ in rounding where the order of combining differs.
- The provenance of any record reaches raw datasets, parameter values, and software versions.
- Only holders keep memory on a user's behalf, and ending their session releases them.
- A published entry answers what produced it without access to the records.

## Left to the system

Not part of this API, and not visible in user code:

- how records and outputs are stored, copied, dropped, and located
- how a run number or file becomes a dataset identity, and how local files are identified
- how data is uploaded or fetched
- when and where a request runs, and how pending inputs are waited for
- how a request of an accumulator spec over many elements is split into parts, such as a tree of partial sums, and how that is configured
- where a session's process runs, and whether a value passed in memory is also written
- how access across proposals is enforced

## Open questions

1. **Names.** "Record" describes storage; "result" is wrong for a failure; "job" is an alternative. `client.run` next to a measurement "run" reads badly in `client.run(IOFQ, {'run': ...})`.
2. **Generic accumulator specs.** How the element model appears in a record, so that `SUM.of(Counts)` and `SUM.of(NormalizationParts)` are told apart; and how an author declares that grouping does not change the result.
3. **Sessions.** Whether a holder can exist without a session the user opened, and how the trigger loop owns one, for a sum that grows with each new dataset under a rule.
4. **Removing a member.** A record of the accumulator spec over fewer parts is always possible. Whether an accumulator offers `remove`, and what it costs, depends on whether it keeps each contribution.

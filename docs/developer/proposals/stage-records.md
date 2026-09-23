# Workflow records and stage records

**Status: implemented in the skeleton.** The code under `packages/essapps/src/ess/apps` follows this document; the open questions at the end are still open.
It changes [records.md](../records.md), [workflow-contract.md](../workflow-contract.md), [stages.md](../stages.md), [aggregation.md](../aggregation.md), and the series part of [rules.md](../rules.md).

## Summary

A record today is one call of a spec with all its parameters.
What actually runs is often a piece of the pipeline: a `sciline.Stage` over the parameters that move, or the contribute or finalize half of a `sciline.Aggregation`.
Because the record cannot say which piece ran, the design needs extra machinery around it: specs cut into contribute and combine, the `carry` declaration, the `SHARED` entry in contributions, and a session that infers stage inputs from successive requests.

This proposal records what runs, in the terms sciline already uses:

- a **workflow record** is a pipeline with parameters set, like a configured `sciline.Pipeline`;
- a **stage record** is one call of a stage cut from it, like one `sciline.Stage.compute`.

A stage names its inputs and its outputs.
An input is either a parameter left unset in the workflow record or an intermediate supplied from outside.
An output is any value the spec exposes, including intermediates.
With that, an aggregation over runs needs no specs of its own and no declaration on the spec.

## The sciline picture it mirrors

```python
pipeline = sans_pipeline()
pipeline[Masks] = masks                     # parameters set: the workflow
pipeline[DirectBeam] = db
pipeline[QBins] = q_bins

pipeline.compute(IofQ)                      # a stage with no inputs

stage = sciline.Stage(pipeline, inputs=[SampleRun], outputs=[Numerator, Denominator])
stage.compute({SampleRun: r1})              # a stage whose input is a parameter

finalize = sciline.Stage(pipeline, inputs=[Numerator, Denominator], outputs=[IofQ])
finalize.compute({Numerator: n, Denominator: d})   # a stage whose inputs are intermediates
```

Everything below is this, with each `compute` call written down as a stage record, and each configured pipeline as a workflow record.

## The two records

```python
class WorkflowRecord(BaseModel, frozen=True):
    id: str                          # hash of the fields below: equal content, equal record
    spec: SpecId
    params: dict[str, Plain]         # the values given; fields may be left unset
    instrument: str
    proposal: str

class StageRequest(BaseModel, frozen=True):
    workflow: WorkflowRecord         # the value itself; the store indexes by its ID
    inputs: dict[str, Plain]         # unset parameters and supplied intermediates, by name
    outputs: tuple[str, ...]         # which exposed values to compute; recorded explicitly
    submitter: str
    label: str | None
    member_key: str | None
    origin: Origin

class StageRecord(BaseModel):
    id: str                          # UUID
    request: StageRequest
    # what happened, as on a record before:
    status: Status
    outputs: dict[str, Any]; stored_outputs: list[OutputRef]
    resolved_params: ...; package_versions: ...; environment: ...; binding: ...
    checksums: ...; derives_from: ...; supersedes: ...; failure: ...; published: ...
    reused: bool
```

In `ess.apps.records`.
A stage request carries its workflow record by value, so a record is complete on its own; the record store indexes stage records by the workflow record's ID.

| | Workflow record | Stage record |
|---|---|---|
| sciline counterpart | `Pipeline` with parameters set | one `Stage.compute` call |
| identity | its content | a UUID |
| mutable | never | status and publication, as today |
| has outputs | no | yes |
| label, member key, supersedes | no | yes |
| created | on first use, reused thereafter | per call |

A workflow record is a value, not a run.
It holds the values given, not the spec's defaults, which apply at run time to fields that neither it nor the stage sets.
Two clients that configure the same spec with the same values get the same workflow record.
All of today's run-level fields move to the stage record.

**Provenance stays the graph of references.**
A stage record references its workflow record, and both may hold references to outputs of other stage records.
To recompute a stage record, the runner rebuilds the pipeline from the workflow record, builds the stage from the input names and the outputs, resolves the inputs, and computes.

## Using it from a client

### A plain run

```python
result = client.run(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
result.ref('iofq')
```

This is shorthand for a workflow record and one stage with no inputs:

```python
wf = client.workflow(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
result = wf.stage().compute()                   # no outputs named: the spec's results
```

`compute` always takes the values of the stage's inputs, and a stage with no inputs takes none.

Scripts that run a workflow once never need more than this.

### Tuning a parameter

A GUI slider, or a notebook loop, declares the stage it needs before the first call:

```python
wf = client.workflow(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db})   # q_bins unset
tune = wf.stage(inputs=['q_bins'], outputs=['iofq'], label='iofq-plot')

for q in (50, 100, 200):
    r = tune.compute({'q_bins': q})       # one stage record per call
    plot(client.view(r.ref('iofq')))
```

The session builds one `sciline.Stage` on the first call and holds it.
The load, the conversion to wavelength, and the masking run once.
Each call writes a stage record `{workflow: wf, inputs: {'q_bins': q}, outputs: ['iofq']}`, which is complete: recomputing it needs nothing from the session.

The handle is the declaration the session needs.
There is no inference from successive requests, and the first call already builds the stage.

### Showing an intermediate

```python
wf = client.workflow(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
det = wf.stage(outputs=['detector_image']).compute()          # an exposed intermediate as output
spectrum = wf.stage(outputs=['wavelength_spectrum']).compute()
client.view(det.ref('detector_image'))
```

The author exposes these values in the spec (see [The spec and the binding](#the-spec-and-the-binding)).
The author does not predict which stages an app will build from them.

### Supplying an intermediate

A beam centre found in one view and used in the next.
In this pipeline the beam centre is an intermediate that a finder computes; here a person supplies it instead:

```python
wf = client.workflow(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db})   # q_bins unset
det = wf.stage(outputs=['detector_image']).compute()
centre = client.run(PICK_CENTRE, {'image': det.ref('detector_image')})   # a person clicks, or another spec runs

reduce = wf.stage(inputs=['beam_centre', 'q_bins'], outputs=['iofq'])
reduce.compute({'beam_centre': centre.ref('centre'), 'q_bins': 100})
```

The stage record says that `beam_centre` was supplied, and which stage record it came from.
The parameters of the finder that `wf` also holds did not affect this result, and the record does not claim they did: it says "this pipeline, cut at `beam_centre`".
Which values a cut makes irrelevant follows from the graph when the binding builds the stage, as in sciline.

### Aggregation over runs: a batch at once

```python
wf = client.workflow(SANS, {'masks': masks, 'direct_beam': db, 'q_bins': 100})   # sample_run unset

contribute = wf.stage(inputs=['sample_run'], outputs=['numerator', 'denominator'], label='members')
members = [contribute.compute({'sample_run': r}) for r in runs]                    # dispatched in parallel

finalize = wf.stage(inputs=['numerator', 'denominator'], outputs=['iofq'], label='total')
total = finalize.compute({
    'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
    'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
})
client.output(total, 'iofq')
```

This is `sciline.Aggregation(pipeline, members=[SampleRun])`: the first stage is its `contribute_stage`, the second its `finalize_stage`.
Outside a session each `compute` is dispatched and returns at once, and the finalize record waits for its members as pending outputs, the scheduling primitive that exists today.

`Accumulate` is the one new reference form.
It says: the value of this input is the accumulation of these outputs.
The framework never adds anything; the binding resolves `Accumulate` with the accumulator the author gave for that value.

**Members cannot disagree on shared parameters.**
Every member and the finalize record reference the same workflow record, so masks and direct beam have one value by construction.
The backend refuses an `Accumulate` whose elements come from stage records of the same spec under a different workflow record, which is a comparison of two IDs and needs no workflow code.
This replaces the `SHARED` entry and the check inside the combine callable.

### A series that grows, in a session

```python
members = []

def add_run(run):
    members.append(contribute.compute({'sample_run': run}))
    return finalize.compute({
        'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
        'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
    })

add_run(r1)
add_run(r2)
total = add_run(r3)
```

Every finalize record lists all current members, so every record is complete as written.
The session holds, beside the finalize stage, one accumulator per accumulated value, addressed by the workflow record, the value's name, and the stage records already pushed.
When a request's elements include everything pushed so far, the session pushes only the new ones.
Adding r3 pushes one contribution, not three.

### Correcting or removing a member

```python
members[1] = contribute.compute({'sample_run': r2_fixed})
total = finalize.compute({
    'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
    'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
})
```

The held accumulator contains the old record for r2, which the request no longer lists.
The session therefore accumulates all three from scratch.
Removing a member works the same way, and nothing is ever subtracted.

A correction that changes a shared parameter, such as better masks, is a new workflow record, and all members run again under it.
That is what sciline does too: a changed pipeline parameter means a new `Aggregation`.

### Two member tables

Sample runs and background runs in ess.sans:

```python
wf = client.workflow(SANS_WITH_BACKGROUND, {'masks': masks, 'direct_beam': db, 'q_bins': 100})
# sample_run and background_run both unset

sample = wf.stage(inputs=['sample_run'], outputs=['sample_numerator', 'sample_denominator'])
background = wf.stage(inputs=['background_run'], outputs=['background_numerator', 'background_denominator'])
s = [sample.compute({'sample_run': r}) for r in sample_runs]
b = [background.compute({'background_run': r}) for r in background_runs]

finalize = wf.stage(
    inputs=['sample_numerator', 'sample_denominator', 'background_numerator', 'background_denominator'],
    outputs=['iofq'],
)
finalize.compute({
    'sample_numerator':       Accumulate(accumulate=[m.ref('sample_numerator') for m in s]),
    'sample_denominator':     Accumulate(accumulate=[m.ref('sample_denominator') for m in s]),
    'background_numerator':   Accumulate(accumulate=[m.ref('background_numerator') for m in b]),
    'background_denominator': Accumulate(accumulate=[m.ref('background_denominator') for m in b]),
})
```

Nothing special is needed: two member stages over one workflow record, one finalize stage.

## The spec and the binding

### What the spec declares

The spec keeps its parameters and outputs.
Its outputs include the intermediates the author exposes, and `intermediates` names them:

```python
class NormalizeOutputs(BaseModel):
    normalized: Array(ArraySpec(dims=('x',)))
    numerator: Array()
    denominator: Array()

NORMALIZE = WorkflowSpec(name='normalize', version=1, params=NormalizeParams,
                         outputs=NormalizeOutputs, intermediates=('numerator', 'denominator'))
```

- A plain run computes the spec's `results`, the outputs that are not intermediates.
- A stage may name any output as an output and any intermediate as an input.
- The format of an intermediate is needed because it may be stored, referenced, and viewed like any other output.

The exposed intermediates are the author's domain types that an app or an aggregation needs.
They are listed once per spec, not once per stage.
The spec says nothing about the graph: which parameters an intermediate depends on is known only to the binding.

### The binding builds stages

The workflow protocol is one method to build stages, which covers plain runs as the stage with no inputs, and one to make accumulators:

```python
class Workflow(Protocol):
    def stage(self, params: BaseModel, inputs: Collection[str], outputs: Collection[str],
              data: Inputs) -> StageCall: ...
    def accumulator(self, name: str) -> Accumulator: ...

class StageCall(Protocol):
    def __call__(self, params: BaseModel, intermediates: Mapping[str, Any],
                 data: Inputs) -> Mapping[str, Any]: ...
```

`stage` gets the workflow record's values (with the spec's defaults for fields nobody set) and the stage record's names.
Each call gets the parameters among the stage inputs, and the intermediates among them already as objects, with `Accumulate` resolved through `accumulator`.
A plain function `(params, inputs) -> outputs` remains a valid workflow; its stages take parameters only and hold nothing (`FunctionWorkflow`).

The sciline adapter implements it directly: set `params` on a copy of the pipeline, build `sciline.Stage(pipeline, inputs=keys_of(inputs), outputs=keys_of(outputs))`, and call `compute`.
It needs the accumulators for the values that can be accumulated:

```python
# ess/sans/aggregation.py: one definition for notebooks and for the binding
ACCUMULATORS = {Numerator: sciline.Buffered(add), Denominator: sciline.Buffered(add)}

def sans_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    return sciline.Aggregation(pipeline, members=[SampleRun], accumulators=ACCUMULATORS, outputs=[IofQ])

# the binding
PipelineAdapter(
    sans_pipeline(),
    keys={'sample_run': SampleRun, 'masks': Masks, 'q_bins': QBins, ...},
    targets={'iofq': IofQ, 'numerator': Numerator, 'denominator': Denominator,
             'detector_image': DetectorImage, 'beam_centre': BeamCentre},
    accumulators=ACCUMULATORS,
)
```

A notebook uses `sans_aggregation` directly with sciline.
The binding reuses the same accumulators, and there is no second adapter for aggregations.

## What a session holds

| Held object | Addressed by |
|---|---|
| output of a stage record | the stage record's ID |
| `sciline.Stage` | workflow record, input names, output names, checksums of the datasets it read |
| accumulator | workflow record, value name, the stage records pushed so far |

All three are caches over records: dropping one costs time and never changes a result.
The session no longer infers stage inputs from the request a new one supersedes.
The handle a client builds with `wf.stage(...)` names the stage, and a stage record without a handle, for example one submitted from a rule, runs the stage it names.

## Batches and rules

`apply` cuts every member from a template: the template's blanks become the member's stage inputs, and everything else, template values, lookup fills, and pinned values, is the workflow record:

```python
template = Template(name='sans-defaults', spec=SANS.id,
                    params={'masks': ..., 'direct_beam': ..., 'q_bins': 100},
                    blanks=('sample_run',))
# each member: workflow record {masks, direct_beam, q_bins}, stage inputs {sample_run: <dataset>}
```

Members that agree on everything but their blanks share one workflow record, and in a session they share one held stage.
A lookup entry that fills a value per dataset, such as a Q range per angle, or a value a person pinned for one member, gives that member a workflow record of its own.

A rule with a series submits, on each arrival, one member stage record and one finalize stage record, both cut from the arriving member's workflow record:

```python
rule = Rule(
    name='sans-series',
    template=template,
    selector=Selector(match={'role': Like(pattern='sample')}),
    series=Series(key='sample_name', accumulate=('numerator', 'denominator'), outputs=('iofq',)),
)
```

The member stage goes from the blanks to the accumulated intermediates; the finalize stage from their accumulation to the outputs.
The rule holds one template instead of two, and `Series` names no combine spec, contribution output, or collection parameter.
A member whose workflow record differs from the others', because a lookup or a pinned value set something only for it, cannot be accumulated with them: the finalize is refused and the trigger loop logs the refusal.

## Chaining without a session

A rule runs each stage record in a throwaway process, and no session holds an accumulator.
A finalize over all current members then reads k contributions on the k-th arrival.
For event-mode or 4D contributions of several gigabytes that is too much.

Chaining needs no declaration on the spec.
A finalize stage can select an accumulated value as an output as well as an input.
`sciline.Stage` passes such a value through, so the finalize record stores the accumulated value:

```python
finalize = wf.stage(inputs=['numerator', 'denominator'],
                    outputs=['numerator', 'denominator', 'iofq'])     # accumulated values stored
```

The next arrival accumulates the previous finalize's value with the new member:

```python
finalize.compute({
    'numerator':   Accumulate(accumulate=[previous.ref('numerator'), new.ref('numerator')]),
    'denominator': Accumulate(accumulate=[previous.ref('denominator'), new.ref('denominator')]),
})
```

This is correct if accumulating a pre-accumulated value gives the same result as accumulating its parts.
That is associativity, and it is a property of the accumulator, not of the spec.
sciline's `Accumulator` contract already requires it, because `Aggregation.combine` pushes combined values too; `ess.apps.testing.assert_accumulator_is_associative` checks it for an accumulator.
Commutativity is not required, because a chain keeps the order in which members arrived.
A combination that is not associative, such as reflectometry's stitch over angles with its global fit of scale factors, is not an accumulator: it stays a spec whose parameter is a list of references to per-angle outputs, computed over all members whenever it is requested. A rule has no clause for it yet (see the open questions).

Which members a previous finalize covers is found by following its `Accumulate` elements back until they reach member stage records.
`_covers` in `batch.py` does this walk.
The conditions for chaining are unchanged: the previous finalize completed, and every record it covers is still a current member.

## Validation and identity

- **A stage input that is a parameter must be unset in the workflow record.**
  The stage record's effective parameters are then the workflow record's values plus the stage inputs, with no overlap and no ambiguity about which value was used.
- **A stage input that is an intermediate must be exposed by the spec, and its value is a reference or an `Accumulate`.**
  Parameters upstream of it may stay set in the workflow record; the record does not claim they affected the result.
- **An intermediate of spec S supplied from a stage record of S must come from the same workflow record.**
  This is an ID comparison and runs at validation, before anything is computed.
  It makes one value of every shared parameter a property of the records, not of the binding.
- **Whether a stage's inputs suffice for its outputs is known only when the binding builds the stage.**
  A stage that leaves a needed parameter unset fails at the start of its run, not at validation.

## What goes away, what stays

Removed from the current design:

- the contribute and combine specs per pipeline, and the rule "cut a workflow into specs where an aggregation adds";
- `carry` on `WorkflowSpec`, and the associativity helper for it (replaced by one for accumulators);
- `ess.apps.aggregation.Aggregation`, the `SHARED` entry, and the combine callable's agreement check; the sciline adapter takes `accumulators=` instead;
- `default_stage_inputs`, and the session's inference of stage inputs from the predecessor request;
- the combine template and the `output` and `parameter` fields of `Series`.

Unchanged:

- references, datasets, groups, and pending outputs as the only scheduling primitive;
- records are complete without the session, and everything a session holds is a cache;
- labels, member keys, superseding, templates, lookups, rules, and the trigger loop, apart from the series changes above;
- non-associative combinations as specs over lists of references;
- the framework never adds arrays and never imports sciline.

The reuse rule in [records.md](../records.md#reuse-means-a-workflow-boundary) changes from "a value other requests reference must be an output of a run of its own" to "must be an output of a stage record".
Exposed intermediates can be such outputs, so reuse no longer forces a spec boundary; separate pipelines remain separate specs.

## Open questions

1. **Exposed intermediates in ess.reduce.spec.** The skeleton adds `intermediates` to the spec of scipp/ess#690. Whether that belongs upstream, and in which form, is open.
2. **Per-dataset lookup fills in a series.** The skeleton puts them into the workflow record, so members that a lookup fills differently are not accumulated together. The alternative is to make such fields stage inputs, which keeps one workflow record per series but lets members differ in a value the finalize may also read.
3. **Validation before running.** Can a GUI learn that a stage leaves a needed parameter unset before submitting? In local mode the backend imports the registry and could ask the binding; in shared mode it does not.
4. **Chaining on disk.** The skeleton writes the chain into the finalize request, as sketched above. The alternative is for a stateless runner to find the previous finalize itself, a cache lookup invisible in the request, so that every request lists all members.
5. **A rule over a combination that is not an accumulation.** `Series` only accumulates. A rule that stitches reflectometry angles, a spec over a list of references such as `amor.COMBINE`, has no clause for it, although the design allows the stitch itself as an ordinary request.
6. **An aggregation helper on the client.** `wf.aggregation(members=..., accumulate=..., outputs=...)` could write the member and finalize stages for a notebook. It adds no concept, and nothing needs it yet.

## Alternatives considered

**One record kind, with the session inferring stages.**
The current design.
It cannot record which piece of a pipeline ran, so aggregation needs specs cut at the accumulation keys, and supplied intermediates are not expressible at all.
A GUI cannot declare the stage it needs, and the first call of every slider computes everything and holds nothing.

**A summary of the graph in the spec.**
The spec lists, per output and intermediate, the parameters it depends on, so the backend can decide which parameters a supplied intermediate makes irrelevant.
It works, but puts a derived property of the graph into a document meant to be written by hand and read without workflow code, and every change to the pipeline must regenerate it.

**An aggregation spec.**
A spec whose signature is `Aggregation.compute(table)`, expanded by the backend into member and finalize records.
It removes `carry` and `SHARED` too, but adds a second kind of spec and records created on behalf of a request, and it does nothing for stages in general.

**Contribute and combine specs with `carry`.**
The current design for aggregation.
Every pipeline that aggregates is published as two or three specs, the adapter re-derives the member parameters to check that members agree, and the associativity declaration lives on the spec although it is a property of the code.

## Costs

- Two record kinds. The record store, recompute, the UI, and publication handle the pair; a stage record always joins to exactly one workflow record.
- A stage that leaves a needed parameter unset fails when it runs, not when it is submitted.
- Authors must expose the intermediates that apps and aggregations use, with formats.
- `Accumulate` is a new reference form, resolved by the binding.
- Every accumulator must be associative, which the framework cannot check and a test helper must.
- Clients that want a stage write two calls, `client.workflow(...)` and `wf.stage(...)`, instead of relying on the session to infer it. `client.run` stays for the plain case.
- A rule cannot run a combination that is not an accumulation, such as a stitch over angles.

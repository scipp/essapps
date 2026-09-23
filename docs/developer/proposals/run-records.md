# Records of workflow runs

**Status: implemented in the skeleton.** The code under `packages/essapps/src/ess/apps` follows this document, and the questions at the end are open.
It changes [records.md](../records.md), [workflow-contract.md](../workflow-contract.md), [stages.md](../stages.md), [aggregation.md](../aggregation.md), and the series part of [rules.md](../rules.md).

## Summary

What runs is often a piece of a pipeline: a `sciline.Stage` over the parameters that move, or the contribute or finalize half of a `sciline.Aggregation`.
A record that holds only a spec and its parameters cannot hold an intermediate supplied from outside, and then needs extra machinery around it: specs cut into contribute and combine, a `carry` declaration, a `SHARED` entry in contributions, and a session that infers stage inputs from successive requests.

This proposal records what runs, in the terms sciline uses.
A **run record** is one run of a workflow, like one `sciline.Stage.compute`.
Its request holds the spec and every parameter value, which together are a configured pipeline, like a `sciline.Pipeline` with parameters set, and the outputs to compute.
A stage is visible in the record only where it cuts the pipeline: an intermediate supplied in place of what computes it.

A stage names its inputs and its outputs.
An input is either a parameter the caller varies or an intermediate supplied from outside.
Of the two, only a supplied intermediate changes what is computed, so only it has a field of its own.
A varied parameter is in `params` like any other, and the names of the varied parameters are a hint for the session.
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

Everything below is this, with each `compute` call written down as a run record that carries every value the pipeline was run with.

## The record

```python
class RunRequest(BaseModel, frozen=True):
    spec: SpecId
    params: dict[str, Plain]         # every parameter value, varied ones included, defaults filled at submit
    supplied: dict[str, Plain]       # intermediates supplied in place of what computes them, by name
    outputs: tuple[str, ...]         # which exposed values to compute; recorded explicitly
    vary: tuple[str, ...]            # parameters the caller varies: a hint for the session
    instrument: str
    proposal: str
    submitter: str
    label: str | None
    member_key: str | None
    origin: Origin

    @property
    def workflow_id(self) -> str: ...  # hash of spec, params outside vary, instrument, proposal

class RunRecord(BaseModel):
    id: str                          # UUID
    request: RunRequest
    # what happened:
    status: Status
    outputs: dict[str, Any]; stored_outputs: list[OutputRef]
    package_versions: ...; environment: ...; binding: ...
    checksums: ...; derives_from: ...; supersedes: ...; failure: ...; published: ...
    reused: bool
```

In `ess.apps.records`.

**Defaults are filled at submit.**
When the backend accepts a request, it adds to `params` the spec's default for every parameter that has one and is not given.
A plain run therefore records the whole params model.
Every value is recorded in the form the params model gives it, so `0` and `0.0` given for one float field are one value and one workflow ID, and the record keeps no second copy of the parameters.
Only a required parameter without a default may stay unset, and only a request that supplies an intermediate in place of what needs it can do without it.
The record says which value every parameter had, so a recompute runs with the values of the first run even after the spec's defaults change.

**`vary` is a hint, like `label`.**
It names the parameters a caller varies from call to call, whose values are in `params` like any other.
It does not change the result, it is not part of the workflow ID, and provenance does not rely on it.
A session reads it to hold the stage cut at those parameters.

**The workflow ID names the configured pipeline a session cuts stages from.**
It is a hash of spec, the parameters outside `vary`, instrument, and proposal, derived from the request and never stored as a record of its own.
Two requests that set the same values outside `vary` have the same workflow ID, whether a value was given or filled as the default, and whatever they vary or supply.
A session names the stages it holds by it, and the record store indexes run records by it.

**Provenance stays the graph of references.**
`params` and `supplied` may hold references to outputs of other run records.
To recompute a run record, the runner sets `params` on the pipeline, builds the stage from the supplied intermediates to the outputs, resolves the references, and computes.

## Using it from a client

### A plain run

```python
result = client.run(SANS, {'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
result.ref('iofq')
```

This is shorthand for a template with every value set and no blanks, one stage with no inputs:

```python
sans = Template(spec=SANS, params={'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
result = client.run(sans)                       # no blanks, no outputs named: the spec's results
```

`client.run(template, values)` takes the values of the template's blanks, and a template with no blanks takes none.
A template is plain data held on the client side; nothing is stored until a run request is submitted.

Scripts that run a workflow once never need more than this.

### Tuning a parameter

A GUI slider, or a notebook loop, declares the stage it needs before the first call:

```python
sans = Template(spec=SANS, params={'sample_run': run, 'masks': masks, 'direct_beam': db})   # q_bins unset
tune = sans.cut(blanks=('q_bins',), outputs=('iofq',), name='iofq-plot')

for q in (50, 100, 200):
    r = client.run(tune, {'q_bins': q})   # one run record per call
    plot(client.view(r.ref('iofq')))
```

The session builds one `sciline.Stage` on the first call and holds it.
The load, the conversion to wavelength, and the masking run once.
Each call writes a run record `{spec: SANS, params: {..., 'q_bins': q}, vary: ['q_bins'], outputs: ['iofq']}`, which is complete: recomputing it needs nothing from the session.
Apart from `vary` and `label`, it is the record a plain run with the same values writes.

The template is the declaration the session needs.
There is no inference from successive requests, and the first call already builds the stage.

### Showing an intermediate

```python
sans = Template(spec=SANS, params={'sample_run': run, 'masks': masks, 'direct_beam': db, 'q_bins': 100})
det = client.run(sans.cut(outputs=('detector_image',)))          # an exposed intermediate as output
spectrum = client.run(sans.cut(outputs=('wavelength_spectrum',)))
client.view(det.ref('detector_image'))
```

The author exposes these values in the spec (see [The spec and the binding](#the-spec-and-the-binding)).
The author does not predict which stages an app will build from them.

### Supplying an intermediate

A beam centre found in one view and used in the next.
In this pipeline the beam centre is an intermediate that a finder computes; here a person supplies it instead:

```python
sans = Template(spec=SANS, params={'sample_run': run, 'masks': masks, 'direct_beam': db})   # q_bins unset
det = client.run(sans.cut(outputs=('detector_image',)))
centre = client.run(PICK_CENTRE, {'image': det.ref('detector_image')})   # a person clicks, or another spec runs

reduce = sans.cut(blanks=('beam_centre', 'q_bins'), outputs=('iofq',))
client.run(reduce, {'beam_centre': centre.ref('centre'), 'q_bins': 100})
```

The run record holds `beam_centre` in `supplied`, with the run record it came from, and `q_bins` in `params`.
The parameters of the finder that `sans` also sets did not affect this result, and the record does not claim they did: it says "this pipeline, cut at `beam_centre`".
Which values a cut makes irrelevant follows from the graph when the binding builds the stage, as in sciline.

### Aggregation over runs: a batch at once

```python
sans = Template(spec=SANS, params={'masks': masks, 'direct_beam': db, 'q_bins': 100})   # sample_run unset

contribute = sans.cut(blanks=('sample_run',), outputs=('numerator', 'denominator'), name='members')
members = [client.run(contribute, {'sample_run': r}) for r in runs]                  # dispatched in parallel

finalize = sans.cut(blanks=('numerator', 'denominator'), outputs=('iofq',), name='total')
total = client.run(finalize, {
    'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
    'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
})
client.output(total, 'iofq')
```

This is `sciline.Aggregation(pipeline, members=[SampleRun])`: the first stage is its `contribute_stage`, the second its `finalize_stage`.
Outside a session each `client.run` is dispatched and returns at once, and the finalize record waits for its members as pending outputs, the one scheduling primitive.

`Accumulate` is a reference form of its own.
It says: the value of this input is the accumulation of these outputs.
The framework never adds anything; the binding resolves `Accumulate` with the accumulator the author gave for that value.

**Members cannot disagree on shared parameters.**
The backend refuses an intermediate supplied from a run record of the same spec that disagrees with the consuming request on a parameter both set in `params`, and names that parameter.
A parameter either request varies is not compared.
Members and finalize cut from one template agree by construction, so masks and direct beam have one value.
The members vary `sample_run`, and the finalize leaves it unset, so it is not compared.
The check compares recorded values and needs no workflow code.
This replaces the `SHARED` entry and the check inside the combine callable.

### A series that grows, in a session

```python
members = []

def add_run(run):
    members.append(client.run(contribute, {'sample_run': run}))
    return client.run(finalize, {
        'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
        'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
    })

add_run(r1)
add_run(r2)
total = add_run(r3)
```

Every finalize record lists all current members, so every record is complete as written.
The session holds, beside the finalize stage, one accumulator per accumulated value, addressed by the workflow ID, the value's name, and the run records already pushed.
When a request's elements include everything pushed so far, the session pushes only the new ones.
Adding r3 pushes one contribution, not three.

### Correcting or removing a member

```python
members[1] = client.run(contribute, {'sample_run': r2_fixed})
total = client.run(finalize, {
    'numerator':   Accumulate(accumulate=[m.ref('numerator') for m in members]),
    'denominator': Accumulate(accumulate=[m.ref('denominator') for m in members]),
})
```

The held accumulator contains the old record for r2, which the request does not list.
The session therefore accumulates all three from scratch.
Removing a member works the same way, and nothing is ever subtracted.

A correction that changes a shared parameter, such as better masks, gives another workflow ID, and all members run again with it.
That is what sciline does too: a changed pipeline parameter means a new `Aggregation`.

### Two member tables

Sample runs and background runs in ess.sans:

```python
sans = Template(spec=SANS_WITH_BACKGROUND, params={'masks': masks, 'direct_beam': db, 'q_bins': 100})
# sample_run and background_run both unset

sample = sans.cut(blanks=('sample_run',), outputs=('sample_numerator', 'sample_denominator'))
background = sans.cut(blanks=('background_run',), outputs=('background_numerator', 'background_denominator'))
s = [client.run(sample, {'sample_run': r}) for r in sample_runs]
b = [client.run(background, {'background_run': r}) for r in background_runs]

finalize = sans.cut(
    blanks=('sample_numerator', 'sample_denominator', 'background_numerator', 'background_denominator'),
    outputs=('iofq',),
)
client.run(finalize, {
    'sample_numerator':       Accumulate(accumulate=[m.ref('sample_numerator') for m in s]),
    'sample_denominator':     Accumulate(accumulate=[m.ref('sample_denominator') for m in s]),
    'background_numerator':   Accumulate(accumulate=[m.ref('background_numerator') for m in b]),
    'background_denominator': Accumulate(accumulate=[m.ref('background_denominator') for m in b]),
})
```

Nothing special is needed: two member stages cut from one template, one finalize stage.

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

`stage` gets the request's parameters outside `vary`, defaults filled, as `params`; the varied parameter names and the supplied intermediate names as `inputs`; and the outputs.
Each call gets the varied parameters as a model, and the supplied intermediates already as objects, with `Accumulate` resolved through `accumulator`.
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
| output of a run record | the run record's ID |
| `sciline.Stage` | workflow ID, varied parameter names, supplied intermediate names, output names, checksums of the datasets the parameters outside `vary` name |
| accumulator | workflow ID, value name, the run records pushed so far |

All three are caches over records: dropping one costs time and never changes a result.
The session does not infer stage inputs from the request a later one supersedes.
A template names the stage through its blanks, which a request made from it carries as `vary` and `supplied`, whether a client cut the template for a slider or a rule holds it.

## Batches and rules

`apply` makes every member from a template: the member's `params` hold the template values, the lookup fills, the pinned values, and the values of the blanks, and its `vary` names the blanks:

```python
template = Template(name='sans-defaults', spec=SANS.id,
                    params={'masks': ..., 'direct_beam': ..., 'q_bins': 100},
                    blanks=('sample_run',))
# each member: params {masks, direct_beam, q_bins, sample_run: <dataset>}, vary ('sample_run',)
```

Members that agree on everything but their blanks share one workflow ID, and in a session they share one held stage.
A lookup entry that fills a value per dataset, such as a Q range per angle, or a value a person pinned for one member, gives that member a workflow ID of its own.

A rule with a series submits, on each arrival, one member run request and one finalize run request, the finalize with the template's values:

```python
rule = Rule(
    name='sans-series',
    template=template,
    selector=Selector(match={'role': Like(pattern='sample')}),
    series=Series(key='sample_name', accumulate=('numerator', 'denominator'), outputs=('iofq',)),
)
```

The member stage, `template.cut(outputs=accumulate)`, goes from the blanks to the accumulated intermediates; the finalize stage, `template.cut(blanks=accumulate, outputs=outputs)`, from their accumulation, which it supplies, to the outputs.
The rule holds one template instead of two, and `Series` names no combine spec, contribution output, or collection parameter.
A member made with a value other than the template's, because a lookup or a pinned value set a field beyond the blanks, cannot be accumulated with the others: the finalize is refused and the trigger loop logs the refusal.

## A growing series without a session

A rule runs each request in a throwaway process, and no session holds an accumulator.
Every finalize request lists every current member of the series in its `Accumulate`, and its outputs are the series' outputs only.
The sum a finalize record stands for is therefore read off its request alone, with no walk back through earlier finalizes.
The cost is reading: the finalize of the k-th arrival reads k contributions per accumulated value, which for event-mode or 4D contributions of several gigabytes is a lot.

A runner may lower that cost with a cache.
It keeps the accumulated value of an earlier finalize, and when a later finalize's list begins with the list that value was accumulated from, it reads only the rest.
That is the rule a session's held accumulator follows (`Stages.accumulate` in `stages.py`).
The request stays the same, so the cache is invisible in the records.
It is not built.

No finalize accumulates a value that another finalize accumulated, so nothing here requires an accumulator to be associative.
sciline's `Accumulator` contract asks for associativity, and nothing here relies on it.
A combination that is not an accumulation, such as reflectometry's stitch over angles with its global fit of scale factors, stays a spec whose parameter is a list of references to per-angle outputs, computed over all members whenever it is requested. A rule has no clause for it yet (see the open questions).

## Validation and identity

- **Validation sees the request as it will be recorded, defaults filled.**
  The workflow ID of the recorded request is the one a session names its stage by.
- **Every name in `vary` is a parameter of the spec with a value in `params`.**
  A value given to `client.run` for a parameter replaces the template's value; nothing conflicts.
- **A supplied intermediate must be exposed by the spec, and a data intermediate is a reference or an `Accumulate`.**
  Parameters upstream of it may stay set in `params`; the record does not claim they affected the result.
- **An intermediate of spec S supplied from a run record of S must agree with it on every parameter both requests set in `params`, apart from those either request varies.**
  This compares recorded values and runs at validation, before anything is computed; a member of the same group is completed with its defaults first.
  It makes one value of every shared parameter a property of the records, not of the binding.
- **A request that supplies no intermediate is checked against the whole params model.**
  A plain run or a batch member that leaves a required parameter unset is refused at submit.
- **Whether a request that supplies an intermediate has what its outputs need is known only when the binding builds the stage.**
  Such a request is checked field by field, and a needed parameter it leaves unset fails at the start of its run.

## What goes away, what stays

Needed where a record cannot hold a supplied intermediate, and not here:

- the contribute and combine specs per pipeline, and the rule "cut a workflow into specs where an aggregation adds";
- `carry` on `WorkflowSpec`, and the associativity helper for it;
- `ess.apps.aggregation.Aggregation`, the `SHARED` entry, and the combine callable's agreement check; the sciline adapter takes `accumulators=` instead;
- `default_stage_inputs`, and the session's inference of stage inputs from the predecessor request;
- the combine template and the `output` and `parameter` fields of `Series`.

Unchanged:

- references, datasets, groups, and pending outputs as the only scheduling primitive;
- records are complete without the session, and everything a session holds is a cache;
- labels, member keys, superseding, templates, lookups, rules, and the trigger loop, apart from the series changes above;
- combinations that are not accumulations as specs over lists of references;
- the framework never adds arrays and never imports sciline.

The reuse rule in [records.md](../records.md#reuse-means-a-run-record) reads "a value other requests reference must be an output of a run record".
Exposed intermediates can be such outputs, so reuse does not force a spec boundary; separate pipelines remain separate specs.

## Open questions

1. **Exposed intermediates in ess.reduce.spec.** The skeleton adds `intermediates` to the spec of scipp/ess#690. Whether that belongs upstream, and in which form, is open.
2. **Per-dataset lookup fills in a series.** The skeleton does not vary them, and a finalize has the template's values, so a member that a lookup fills beyond the blanks is not accumulated. The alternative is to vary such fields as well, which the agreement rule does not compare, but which lets members differ in a value the finalize may also read.
3. **Validation of a request that supplies an intermediate.** Can a GUI learn that such a request leaves a needed parameter unset before submitting? In local mode the backend imports the registry and could ask the binding; in shared mode it does not.
4. **A rule over a combination that is not an accumulation.** `Series` only accumulates. A rule that stitches reflectometry angles, a spec over a list of references such as `amor.COMBINE`, has no clause for it, although the design allows the stitch itself as an ordinary request.

## Alternatives considered

**A record of one call of a spec, with the session inferring stages.**
It cannot record a supplied intermediate, so aggregation needs specs cut at the accumulation keys.
A GUI cannot declare the stage it needs, and the first call of every slider computes everything and holds nothing.

**A summary of the graph in the spec.**
The spec lists, per output and intermediate, the parameters it depends on, so the backend can decide which parameters a supplied intermediate makes irrelevant.
It works, but puts a derived property of the graph into a document meant to be written by hand and read without workflow code, and every change to the pipeline must regenerate it.

**An aggregation spec.**
A spec whose signature is `Aggregation.compute(table)`, expanded by the backend into member and finalize records.
It removes `carry` and `SHARED` too, but adds a second kind of spec and records created on behalf of a request, and it does nothing for stages in general.

**Contribute and combine specs with `carry`.**
Every pipeline that aggregates is published as two or three specs, the adapter re-derives the member parameters to check that members agree, and the associativity declaration lives on the spec although it is a property of the code.

**Finalizes that accumulate onto earlier finalizes.**
A finalize also outputs the accumulated values, and the next finalize lists them with the members the previous one does not cover.
The k-th arrival then reads two values per accumulated value instead of k.
What a finalize record sums is then found only by walking back through earlier finalizes, a correction is detected only by comparing the records a previous finalize covers with the current members, and every accumulator must be associative.
A cache in the runner gives the same saving and leaves the request as it is.

**Stage-level records whose inputs hold varied parameters apart from `params`.**
A request `(spec, params, inputs, outputs)` where `inputs` holds both the parameters a stage varies and the supplied intermediates, and `params` holds the rest.
The layout of the record then depends on which parameters a caller chose to vary, which is a caching choice: two runs with the same values have different records depending on the stage the caller held, and a tool that reads a parameter has to look in two places.
Only supplied intermediates change the computation, so only they get a field of their own, and the varied names are a hint beside the values.

**A stored workflow record holding only given values.**
A record kind of its own, `WorkflowRecord(spec, params, instrument, proposal)`, embedded in each request with the values given and no defaults, where an intermediate from a run record of the same spec must come from the same workflow record.
It gives two identities per configuration: omitting a parameter and giving it at its default are two workflow records, with no shared held stage and no accumulation across them.
A recompute is unsafe against changed defaults, because the defaults apply at run time, so a spec whose default changed recomputes another result from the same record.
Filling defaults at submit makes the run request hold every value that ran, and the identity of the configured pipeline follows from it.

## Costs

- A request that supplies an intermediate and leaves a needed parameter unset fails when it runs, not when it is submitted.
- A default is recorded on every request, so after a spec's default changes, a request with the same given values has another workflow ID: a held stage and an accumulation do not span the change.
- The agreement rule compares only parameters both requests set in `params` and neither varies; a value one of them varies is not checked against the other.
- A varied parameter is left out of the workflow ID, so two requests that differ only in which parameters they vary, and in their values, share a workflow ID; they differ in the held stage they name.
- Authors must expose the intermediates that apps and aggregations use, with formats.
- `Accumulate` is a reference form of its own, resolved by the binding.
- Without a session, the finalize of the k-th arrival reads k contributions per accumulated value.
- Clients that want a stage declare it as a template with its blanks, instead of relying on the session to infer it. `client.run(spec, params)` stays for the plain case.
- A rule cannot run a combination that is not an accumulation, such as a stitch over angles.

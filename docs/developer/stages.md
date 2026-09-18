# Specs, stages, and aggregations

Companion to [architecture.md](architecture.md).
It answers one question: how do the workflow spec of scipp/ess#690 and the run record of this framework relate to sciline's `Stage` and `Aggregation` (scipp/sciline#245, ADR 0003)?
The answer changed D8, D13, D14, and D15 of the sketch in its fourteenth review pass, and this note gives the reasoning in one place.

## The sciline objects

scipp/sciline#245 replaces `Pipeline.map` and `reduce` with objects that a caller composes outside the graph.

- A **`Stage`** is the part of a pipeline from named input keys to named output keys.
  It computes once everything the inputs cannot affect and holds those values at its **frontier**.
  Each call supplies the inputs and computes only what lies downstream of them.
  An input may be a parameter or an intermediate result.
  A stage is a snapshot: the other parameters were set on the pipeline before it was built, and the stage does not expose them.
- An **`Accumulator`** is any object with `push` and `value`.
  `Buffered(func)` and `Reduced(func)` make one from a combine function.
- An **`Aggregation`** is two stages of one pipeline with accumulators between them.
  The contribute stage computes the values at the **accumulation keys** from the **member keys**, the parameters that differ from member to member.
  One accumulator per accumulation key combines the contributions of the members.
  The finalize stage computes the outputs from the combined values.
  The aggregation holds nothing between calls; whoever loops over the members holds the contributions.

## The question

The sketch has specs, requests, and records.
A spec declares a parameter model and an output model.
A request names a spec and gives parameter values.
A record is a request plus what happened to it.
Everything else in the framework reads these three: validation, forms, templates, lookups, rules, the picker, provenance, publication.

Until this pass, D15 let one spec stand for three callables.
A spec marked one output as its `contribution` and listed the parameters that finalize reads.
A request carried a `stage`: `run`, `contribute`, or `combine`.

| Request `stage` | Parameters | Outputs |
|---|---|---|
| `run` | the spec's parameter model | all outputs |
| `contribute` | the spec's parameter model | the contribution only |
| `combine` | a model derived from the spec: the listed finalize parameters | combined contribution and the other outputs |

That is where the following came from, each checked against the skeleton:

- The spec carried `contribution` and `finalize_params`, whose only purpose was to derive the second and third signature from the first.
- A combine request carried its `contributions` beside the parameters, against D13, which says every input is a parameter of data-reference type.
- Every output except the contribution had to be optional, because a member run does not produce it.
- The binding had a second protocol with three entry points beside the single callable of D8.
- Backend, runner, batch, and rules each branched on `stage`.
- The check that the members of a combine agree on their parameters skipped contributions that came from a previous combine, so it checked nothing in a chained series, which is the common case.
- A workflow with two member tables could not be declared.
  esssans has one: sample runs and background runs are two aggregations that share a finalize stage.

The wording showed the same mismatch.
"Map and combine" in D6 and "contribute, combine, finalize" in D15 named one thing twice.
A `runs_to_sum` list parameter sat on the same example spec that declared a contribution.
The relation between the warm workflow and `Stage` was described three times in different words.

## The answer

**A spec is the signature of one callable, and a record is one call of it.**
There is no request that runs part of a spec, and no spec that stands for several callables.

The relation to sciline, in one sentence each:

- A `Pipeline` is what such callables are cut from.
- A `Stage` is one implementation of a spec's callable, with the rest of the pipeline held.
  `stage.keys` bounds the parameters the spec may declare, and `stage.outputs` is its output set.
- An `Aggregation` is not a callable.
  It is a way of composing two callables with accumulators between them, so it has no spec.

sciline moved map and reduce out of the graph so that the caller composes stages and accumulators.
The framework is such a caller: one that composes across processes and keeps the values between stages as outputs of records.
A stage boundary that the framework must see is a boundary between two specs.
A stage boundary that it need not see stays inside a callable.
D4 already says this for reuse ("a value that other requests reference must be an output of a run of its own").
An aggregation over runs is D4's cut with an accumulator at the boundary.

| sciline, in one process, values in memory | Framework, across processes, values are outputs of records |
|---|---|
| `Pipeline` with parameters set, `compute(targets)` | one spec, one request, one record |
| `Stage` whose inputs are parameters, kept between calls | the same spec; a session runner keeps the callable (a warm callable) |
| `Stage` whose input is an intermediate result | a second spec, with a data parameter that takes an output of the first (D4) |
| `Aggregation.contribute_stage` | the **contribute spec**: contribute parameters in, contribution out |
| accumulators and `finalize_stage` | the **combine spec**: a collection of contributions and the finalize parameters in, combined contribution and results out |
| member table | the batch table that `apply` takes (D14) |
| `Aggregation.compute(table)` | an **aggregation**: a group of one member request per row and one combine request that references their contributions (D6) |
| accumulators held by a loop | the combine callable kept between calls (the fold) |

An aggregation appears in one of two places, never half in each.

- **Inside one callable.**
  The callable runs `Aggregation.compute` itself, and the framework sees an ordinary spec.
  This is for members the framework has no records for: angle groups inside a Bifrost run, chunks of an NMX file.
- **Across records.**
  Each stage is a spec, and the framework's group of requests is the aggregation.
  This is for runs: each member has a record, the launcher runs members in parallel, a series can be chained, and a member can be removed.

`WorkflowSpec` in scipp/ess#690 needs no field for any of this.
Its ADR 0001 already says that a workflow whose parameter set depends on what is computed is several workflows with several specs.

## Warm callables

A runner makes the callable from its factory.
A throwaway runner calls it once.
A session runner keeps it, one per spec version, and calls it for every run of that spec.
A kept callable may hold values from earlier calls, and is then **warm**.
What it holds must be a cache: a warm call returns what a fresh callable returns for the same parameters.
The warm-equals-cold test helper checks this.
The framework keeps a callable or it does not; it knows nothing about what the callable holds.

For a sciline pipeline the held values are a stage's frontier.
The adapter (see "The adapter") names some parameters as **stage inputs**, sets all others on the pipeline, and builds a stage from the stage inputs to the targets.
A call that changes only stage inputs is one call of the stage.
A call that changes another parameter builds a new stage.
A warm sciline callable is therefore the same function as the pipeline, with the part that the stage inputs cannot affect computed once.
Which parameters are stage inputs is the adapter's choice.
It is not on the spec, because nothing in the framework reads it and correctness does not depend on it.

"Workflow lifetime" is the lifetime of the callable, and the runner sets it.
`Stage` is how a sciline callable makes use of being kept.

## An aggregation across records

A workflow package that wants its pipeline summed over runs publishes two specs cut from one pipeline.
It may also publish the pipeline's own spec, for one run.
The example assumes a contribution that holds events, so that the Q bins are read after the sum.
esssans today histograms in Q before its accumulation keys, which makes `q_bins` a contribute parameter there.

```python
class Contribute(BaseModel):          # parameters the contribute stage reads
    sample_run: NexusRef
    direct_beam: ArrayRef
    wavelength_bins: WavelengthEdges

class Finalize(BaseModel):            # parameters only the finalize stage reads
    q_bins: QEdges

class Reduce(Contribute, Finalize):   # the whole pipeline, for one run
    pass

class Combine(Finalize):
    contributions: Annotated[
        list[OutputRef], DataField(format=SCIPP), Accumulates(into='contribution')
    ]

CONTRIBUTE = WorkflowSpec('loki-iofq-contribute', params=Contribute, outputs=Contribution)
COMBINE    = WorkflowSpec('loki-iofq-combine',    params=Combine,    outputs=CombinedIofQ)
REDUCE     = WorkflowSpec('loki-iofq',            params=Reduce,     outputs=IofQ)
```

`Contribution` has one field, `contribution`, a scipp data group with one entry per accumulation key.
It is one output so that one reference names a member's whole contribution.
`CombinedIofQ` has the same field, holding the combined contribution, and the fields of `IofQ`.
The combine spec runs sciline's combine and finalize in one call.

All three are plain specs.
A contribute request and a combine request are validated, shown in a form, saved as templates, recorded, and recomputed like any other request.
The combine request references the members' contributions through an ordinary collection parameter, so holding it until the members complete is D6 and nothing else.

**The accumulating mark.**
`Accumulates(into='contribution')` is the one declaration that remains.
The mark says that the output `contribution` of this spec may be passed as an element of this parameter.
The author may set the mark only if the combination does not depend on how the elements are grouped or ordered.
The framework cannot check that property; a generic test helper does.
The spec validates on its own that the mark names one of its outputs and that the types match.
It has two readers, and neither can import workflow code:

- `apply` and the trigger loop, which chain: a combine request references the previous combine's contribution and the new members, instead of all members.
- A runner that keeps the combine callable for a series (the fold).

The mark is additive.
A component that ignores it still validates, runs, records, and recomputes the spec correctly, and only loses an optimisation.
The declarations it replaces were not additive: a component that ignored `stage` or `finalize_params` validated and ran the wrong thing.

A combine that is not additive has the same shape without the mark.
Reflectometry's stitch is a spec with a collection parameter of per-angle curves, and a rule combines over all members on every arrival.
A rule's combine clause therefore has one form for both: a combine template, and which member output feeds which collection parameter.
A workflow with two member tables, sample runs and background runs, has two contribute specs and one combine spec with two accumulating parameters.

**When chaining is valid.**
The current members of a series are a query: the latest record per member key under the rule's label, leaving out failed, cancelled, and excluded ones.
A chained combine references the previous combine's contribution and the contribution of every current member that the previous combine does not cover.
A combine covers the member records it references, directly or through the combines it chains onto.
`apply` finds them by following the accumulating parameter back.
Chaining is valid only if every record the previous combine covers is still a current member.
The comparison is between records, not member keys: a corrected member has a new record, so the previous combine covers a record that is no longer current.
If a member was corrected, excluded, or reprocessed under a new rule version, the check fails, and `apply` submits a combine over all current members instead.
The first combine of a series has no previous combine and references its one member.
Two arrivals close together cannot lose a member, because the later combine references every current member that the previous combine does not cover.
The skeleton always chains onto the latest combine, so a corrected member is counted twice; that is a bug in both the old design and the new one.

**Parameters that members must share.**
Members of one sum must be reduced with the same masks, the same direct beam, the same wavelength bins.
The old design checked this in the backend: all parameters except data references had to agree.
That check was wrong twice.
It did not follow chains, and it would refuse a parameter that a lookup legitimately fills per member.
Which parameters may differ between members is exactly sciline's member keys, and only the adapter knows them.
(architecture.md calls them member parameters, because a member key there is the label of a batch member.)
The adapter therefore writes the values of all other contribute parameters into the contribution, and combine refuses contributions that disagree.
The combined contribution carries the same values, so the check holds along a chain without walking it.
The refusal happens when the combine runs, not at validation: the run fails after loading the contributions and before computing anything.

The same mechanism serves a parameter that both stages read.
esssans has one: `UncertaintyBroadcastMode` is read before and after the accumulation keys.
Such a parameter is a field of the contribute spec only, and the finalize stage takes its value from the contribution.
The combine spec does not expose it, so it cannot be set differently from the members.

## The adapter

The missing component is not a second kind of spec.
It is the code that lays stages over a pipeline to serve plain specs.
Per spec it holds: the mapping from parameter fields to sciline keys, the form each data reference is asked for (a path or an object), the mapping from output fields to target keys, and the stage inputs.
For an aggregation it also holds the accumulation keys, an accumulator per key, and the member keys.
It returns one callable per spec.

The skeleton has this as `WarmPipeline` and `AggregatePipeline` inside essapps, the second with three entry points.
It imports sciline and the workflow's key types, so it is code and not spec.
It belongs in ess.reduce, next to the spec module, because every workflow package needs it and essapps must not import sciline.

When it is built, the adapter checks its specs against the graph:

- The literal parameters of the combine spec are the parameters in `finalize_stage.keys` that are not in `contribute_stage.keys`.
- The parameters of the contribute spec are the parameters in `contribute_stage.keys`.
- The parameters of the one-run spec are the union.
- `agg.accumulation_keys` equals the declared accumulation keys; a key that does not depend on the members is an error.

A wrong spec is then an error when the adapter is built, not a wrong result.

In a session, the callables cut from one pipeline share one adapter object.
The one-run callable is implemented as contribute followed by finalize, and the adapter object keeps each contribution in a dictionary keyed by the contribute parameter values that produced it.
A run that was reduced on its own is then not loaded again when it becomes a member of a sum.
The factories of the three specs return callables of one shared object; the framework is not involved.

The warm contribute callable is sciline's contribute stage.
Its stage inputs are the member keys, so what the members share, a reduced reference or a direct beam, is computed once per session.
The warm combine callable holds the finalize stage and the accumulators, keyed by the references it last combined.
A request whose contributions extend the previous list pushes only the new ones.
A request that changes only a finalize parameter calls only the finalize stage.
Its stage inputs are the accumulation keys and the finalize parameters, which `Aggregation` does not offer today (see "Points for the sciline proposal").

## Alternatives considered

**One spec, three entry points.**
The design before this pass, described under "The question".

**A spec for the pipeline, and a second declaration for the operation on top.**
`WorkflowSpec` describes the whole pipeline.
A separate stored object mirrors the constructors of `Stage` and `Aggregation`: the workflow, the member parameters, the accumulation outputs, the final outputs.
A record is then a call of one entry point of an operation on a workflow.
This gets the aggregation fields off `WorkflowSpec`, and it lets one template cover both stages.
It does not remove the declaration of which parameters finalize reads, because that is a property of the graph that field names cannot give; the declaration only moves.
The backend must derive each entry point's signature from the pipeline's model, which duplicates the rule that `Stage.keys` implements.
Every component that reads a record's identity gets a three-part address: workflow, operation, entry point.
A form for a combine still needs the contributions, which are a field of no model.
It also contradicts ADR 0001 of scipp/ess#690, which decided that a slice of a pipeline with its own parameter set is a workflow with its own spec.

**Three specs: contribute, combine, finalize.**
This mirrors sciline exactly: stage, accumulators, stage.
The middle spec has no parameters and no workflow code, and it writes a record per arrival that nobody reads.
Its benefit is that changing a finalize parameter does not write the combined contribution again.
An author who needs that can publish a finalize-only spec as a further cut under D4.
How many specs a pipeline is cut into is the author's choice, not the framework's.

**No mark: always combine over all members.**
Correct, and simpler.
A series of k members then reads k contributions per arrival instead of two.
For event-mode contributions of gigabytes that is the cost D15 exists to avoid.

**The mark on the rule instead of the spec.**
The person who writes a rule cannot know whether a combine is additive, and a wrong answer gives a wrong number without an error.

## Costs

- Up to three specs per pipeline.
  A UI that lists workflows shows the two building blocks beside the whole reduction.
- A rule with a series holds two templates, for the member and for the combine.
  They are versioned together as part of the rule.
- Changing a finalize parameter on a combined result, outside a session, is a combine request over the one previous contribution, and writes that contribution again.
- The shared-parameter check runs in the combine, not at validation.
- The contribute and the combine spec are versioned together by convention of the adapter.
- A value at an accumulation key must be storable in a scipp data group.
  essreflectometry accumulates a list of ORSO file entries beside its events; the author converts it.

## Open points this pass did not close

- **Two-level aggregation under a rule.**
  Reflectometry sums same-angle runs and then stitches angles.
  Both levels are expressible as specs.
  A rule has one combine clause; the second level is a second rule whose candidates are the completed combine records of the first.
  This is untested.
- **Roles.**
  A rule that feeds sample runs into one accumulating parameter and background runs into another needs a role per dataset and a mapping from role to parameter.
  D14 names "a series of fixed roles" without saying how.
- **The feedback cycle** of the Amor probe, members to combine to members, is unchanged.
- **Chaining between specs is checked by format only** (D13 finding of the Amor probe), so a contribution from the wrong spec passes validation and fails in combine.

## Phase 3

[stateless.md](stateless.md) lays out three models for interactive work.
With `Stage` the three hold the same objects and differ in placement: where the object between two stages lives, and whether its value is a record.

| Model | The stage and what sits after it |
|---|---|
| Session | The session's runner keeps the callable; each rerun is a call with the stage inputs, recorded in a slot |
| Checkpoint | The application keeps the stage; calls create no records; a kept result is a cold request |
| Stateless with splits, first rung | Two specs with the value between them stored as a record; each rerun is a throwaway process calling the second |
| Stateless with splits, second rung | The second callable kept by a warm runner, addressed by the reference it holds |

The fold is the second rung for a combine spec: a runner that keeps the combine callable of one series.
Phase 3 still does not need deciding now.
Whichever model is chosen, the callables it keeps are built and tested in phases 1 and 2.

## What this does not solve

- **Memory policy.**
  A stage holds its frontier, an aggregation holds nothing, and the adapter holds contributions in a session.
  All of it is a private cache under D3.
- **Parallelism over members.**
  Across records the launcher runs one throwaway process per member.
  Inside one callable the adapter maps `contribute` over rows with threads, after warming all stages together.
- **Live streams.**
  `StreamProcessor` has members that cannot be recomputed.
  That is esslivedata's problem and stays out of scope.
- **Static work across processes.**
  A contribute request in a throwaway process computes the part shared by all members again.
  This is the phase 3 cost that stateless.md already measures.

## Points for the sciline proposal

- **Section 6.7 of the design document** describes essapps as one spec with a declared contribution and three entry points, and says the spec declares which parameters finalize reads.
  After this pass it should say: essapps serves an aggregation as two plain specs, the adapter checks them against `contribute_stage.keys` and `finalize_stage.keys`, and nothing is declared on the spec except that the combined contribution may be fed back.
- **Finalize inputs.**
  `Aggregation` builds its finalize stage with the accumulation keys as its only inputs, so a finalize parameter cannot be given per call.
  A `Stage` with the accumulation keys and that parameter as inputs takes both, and the adapter builds that stage itself.
  An additive `finalize_inputs=` argument would remove the need.
  Not yet applied on the sciline branch.
- **A parameter read by both stages** needs to reach the finalize stage with the value the contributions were made with.
  Within one process the snapshot guarantees it.
  Across processes the adapter carries it in the contribution.
  The design document could name this as the consumer's responsibility.
- Applied on the branch on 2026-09-14: the condition for combining combined values on `Accumulator`, the lock on a stage's held part, and the stale evidence paragraph.

## What changes in the skeleton

Not yet done; the skeleton still implements the design before this pass.

- Remove `WorkflowSpec.contribution`, `WorkflowSpec.finalize_params`, `finalize_model`, `RunRequest.stage`, `RunRequest.contributions`, the `CombiningWorkflow` protocol, the rule that other outputs are optional, `_check_stage`, `_check_contributions`, and every branch on `stage`.
- Add the `Accumulates` field annotation and its spec-level validation.
- `AggregatePipeline` returns two or three callables that share one object, writes the shared contribute parameters into the contribution, and refuses contributions that disagree.
- A rule's `Series` names a combine template, the member output, and the collection parameter.
  `apply` chains only when the previous combine's members are a subset of the current ones.
- `examples.NORMALIZE` becomes two specs, and the grouping helper takes the two callables.

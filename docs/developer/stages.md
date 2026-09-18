# Specs, stages, and aggregations

Companion to [architecture.md](architecture.md).
It answers one question: how do the workflow spec of scipp/ess#690 and the run record of this framework relate to sciline's `Stage` and `Aggregation` (scipp/sciline#245, ADR 0003)?
The answer changed D8, D13, D14, and D15 of the sketch in its fourteenth review pass, and this note gives the reasoning in one place.
A fifteenth pass asked who holds a stage in a session, and changed D8 and D15 again; see "Stages in a session".

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
| `Stage` whose inputs are parameters, kept between calls | the same spec; the session holds the stage and routes matching requests to it |
| `Stage` whose input is an intermediate result | a second spec, with a data parameter that takes an output of the first (D4) |
| `Aggregation.contribute_stage` | the **contribute spec**: contribute parameters in, contribution out |
| accumulators and `finalize_stage` | the **combine spec**: a collection of contributions and the finalize parameters in, combined contribution and results out |
| member table | the batch table that `apply` takes (D14) |
| `Aggregation.compute(table)` | an **aggregation**: a group of one member request per row and one combine request that references their contributions (D6) |
| accumulators held by a loop | the previous combined contribution, an output the session holds or the store keeps, passed back into the next combine (chaining) |

An aggregation appears in one of two places, never half in each.

- **Inside one callable.**
  The callable runs `Aggregation.compute` itself, and the framework sees an ordinary spec.
  This is for members the framework has no records for: angle groups inside a Bifrost run, chunks of an NMX file.
- **Across records.**
  Each stage is a spec, and the framework's group of requests is the aggregation.
  This is for runs: each member has a record, the launcher runs members in parallel, a series can be chained, and a member can be removed.

`WorkflowSpec` in scipp/ess#690 needs no field for any of this.
Its ADR 0001 already says that a workflow whose parameter set depends on what is computed is several workflows with several specs.

## Stages in a session

A workflow is a stateless callable: parameters and inputs in, outputs out, and no call affects a later one.
The spec is its signature.
State between runs exists only in a session, and the session holds all of it: the outputs of its records, and stages.

Until the fifteenth pass the sketch said otherwise.
A session runner kept the callable, and a kept callable "may hold values from earlier calls, and is then warm".
What it held was called a cache, and a test helper compared warm with cold.
That definition let three kinds of session state sit inside workflow code, where the session could not see them:

- a stage, with the rule for when to rebuild it;
- accumulators for a series, keyed by the references last combined, with their own rule for when they are stale;
- a dictionary of contributions, keyed by parameter values and shared between the callables of three specs.

It contradicted the sentence above that the framework is the caller that composes stages and accumulators: in a session, a second composer sat inside the callable.
It also put the choice of stage inputs with the adapter's author, who cannot make it.
A `Stage` recomputes everything downstream of all its inputs on every call.
The Amor binding named the sample run, the number of Q bins, and a scale factor as stage inputs, so a change of the Q bins loaded the run again.
No fixed choice serves both "tune one parameter" and "the same settings over many runs"; which one it is depends on what the person does, and only the session sees that.

**A workflow may offer a stage.**
Beside the callable, a workflow may offer `stage(params, stage_inputs, inputs)`.
`stage_inputs` is a set of parameter field names.
The result is a callable with the signature of the workflow.
It accepts every request that equals `params` in all fields outside `stage_inputs`, and for those it returns what the workflow returns.
It may hold whatever the stage inputs cannot affect.
The interface is in field names, so the framework does not import sciline.
A plain function offers no stage, and the session calls it directly.
The sciline adapter implements `stage` with `sciline.Stage`: it sets the other parameters on a copy of the pipeline and builds a stage from the keys of the stage inputs to the targets.

**The session holds the stages.**
A held stage is addressed by what it holds: the spec, its stage inputs, the values of all other fields, and the checksums of the datasets among them.
The values are compared as plain data, so a reference compares as a reference and nothing is loaded for the comparison.
The checksums keep a file that changed on disk from finding the stage built from its earlier bytes.

- A request is routed to a held stage of its spec whose other fields equal the request's. If several match, the one with the fewest stage inputs is used, because it recomputes least.
- If none matches, the session builds one. Its stage inputs are the fields in which the request differs from its **predecessor**: the request it supersedes (D14), or, for a new member of a batch, the latest request under its label.
  A slider therefore names its own stage input, a batch over runs names the columns of its table, and a correction to one member names the corrected field.
- A request without a predecessor has nothing to differ from. The adapter may name default stage inputs for it. If it names none, the session calls the workflow and holds nothing.
  The default is a first guess that saves one full computation per series, and nothing depends on it.
- The session holds a bounded number of stages and drops the least recently used. Dropping a stage is always safe.

A stage belongs to no label.
Two labels that move the same parameter on the same data are routed to one stage, so their loaded data is held once.
The label is used only to find the predecessor.

The cost of this rule is one full computation when a person starts to move a parameter that was not a stage input: the stage for it must be built.
The design before this pass had the same cost on every change to a parameter the author had not named, and no way to avoid it.
A person who moves two parameters in turn pays it on every switch, unless both changed in one request, which builds a stage with both as inputs.
Holding the values between two consecutive stages, so that a switch is cheap as well, needs the network of stages that scipp/sciline#245 deferred.

Every call through a stage is a complete request and writes a complete record, as before.
The record says whether a held stage served it (`reused`), which publication reads (D11); the session now knows this itself and does not ask the callable.
The test helper states the contract: for a sequence of requests, the results through held stages equal the results of the workflow.

"Workflow lifetime" is no longer a concept.
A throwaway runner calls the workflow once; a session holds stages and says how long.

## An aggregation across records

A workflow package that wants its pipeline summed over runs publishes two specs cut from one pipeline.
It may also publish the pipeline's own spec, for one run.
That spec is served by the plain adapter over the same pipeline and is no part of the aggregation; the skeleton uses it as the oracle that a series of one member must equal.
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
    contributions: Annotated[list[OutputRef], DataField(format=SCIPP)]

CONTRIBUTE = WorkflowSpec('loki-iofq-contribute', params=Contribute, outputs=Contribution)
COMBINE    = WorkflowSpec('loki-iofq-combine',    params=Combine,    outputs=CombinedIofQ,
                          chain={'contributions': 'contribution'})
REDUCE     = WorkflowSpec('loki-iofq',            params=Reduce,     outputs=IofQ)
```

`Contribution` has one field, `contribution`, a scipp data group with one entry per accumulation key.
It is one output so that one reference names a member's whole contribution.
`CombinedIofQ` has the same field, holding the combined contribution, and the fields of `IofQ`.
The combine spec runs sciline's combine and finalize in one call.

All three are plain specs.
A contribute request and a combine request are validated, shown in a form, saved as templates, recorded, and recomputed like any other request.
The combine request references the members' contributions through an ordinary collection parameter, so holding it until the members complete is D6 and nothing else.

**The chain declaration.**
`chain={'contributions': 'contribution'}` is the one declaration that remains.
It says that the output `contribution` of a run of this spec may be passed as an element of the parameter `contributions` of a later run, and that it then stands for all the elements it was combined from.
In symbols, `combine([combine([a, b]), c])` gives the same result as `combine([a, b, c])`.
The author may declare this only if the combination does not depend on how the elements are grouped or ordered.
The framework cannot check that property; a generic test helper does.
The spec validates on its own that the parameter is a collection of references, that the output exists, and that their formats match.

It is a field of the spec and not an annotation of the parameter, because it states a property of the callable that relates a parameter to an output.

It has one kind of reader, and that reader cannot import workflow code: whatever builds the combine request for a series, which is `apply` and the trigger loop.
With the declaration, the request references the previous combine's contribution and the new members; without it, all members.

The declaration is additive.
A component that ignores it still validates, runs, records, and recomputes the spec correctly, and only loses an optimisation.
The declarations it replaces were not additive: a component that ignored `stage` or `finalize_params` validated and ran the wrong thing.

A combine that is not additive has the same shape without the declaration.
Reflectometry's stitch is a spec with a collection parameter of per-angle curves, and a rule combines over all members on every arrival.
A rule's combine clause therefore has one form for both: a combine template, and which member output feeds which collection parameter.
A workflow with two member tables, sample runs and background runs, has two contribute specs and one combine spec that chains two parameters, each with its own combined output.

**When chaining is valid.**
The current members of a series are a query: the latest record per member key under the rule's label, leaving out failed, cancelled, and excluded ones.
A chained combine references the previous combine's contribution and the contribution of every current member that the previous combine does not cover.
A combine covers the member records it references, directly or through the combines it chains onto.
`apply` finds them by following the chained parameter back: an element is a previous combine if a run of the combine spec itself produced it, through the output that `chain` names, and a member otherwise.
The declaration and the records are enough for this walk; no rule is needed.
It reads one record per combine of the chain, so the k-th arrival of a series chained one at a time costs k reads of metadata, while the data read stays at two contributions.
Chaining is valid only if the previous combine completed and every record it covers is still a current member.
A failed or cancelled combine is never chained onto; otherwise one transient failure would end the series.
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
In the skeleton they are one reserved entry of the data group, `shared`, holding the canonical JSON of a model of exactly those fields as a scipp string, which scipp HDF5 can write; the comparison is string equality, and the finalize stage gets typed values back from it.
The combined contribution carries the same values, so the check holds along a chain without walking it.
The refusal happens when the combine runs, not at validation: the run fails after loading the contributions and before computing anything.

The same mechanism serves a parameter that both stages read.
esssans has one: `UncertaintyBroadcastMode` is read before and after the accumulation keys.
Such a parameter is a field of the contribute spec only, and the finalize stage takes its value from the contribution.
The combine spec does not expose it, so it cannot be set differently from the members.

## The adapter

The missing component is not a second kind of spec.
It is the code that lays stages over a pipeline to serve plain specs.
Per spec it holds: the mapping from parameter fields to sciline keys, the form each data reference is asked for (a path or an object), the mapping from output fields to target keys, and optional default stage inputs.
For an aggregation it also holds the accumulation keys, an accumulator per key, and the member keys.
It returns one workflow per spec: a stateless callable that offers `stage`.

The skeleton has this as `PipelineAdapter` and `AggregatePipeline` inside essapps.
It imports sciline and the workflow's key types, so it is code and not spec.
It belongs in ess.reduce, next to the spec module, because every workflow package needs it and essapps must not import sciline.

When it is built, the adapter checks its specs against the graph:

- The literal parameters of the combine spec are the parameters in `finalize_stage.keys` that are not in `contribute_stage.keys`.
- The parameters of the contribute spec are the parameters in `contribute_stage.keys`.
- The parameters of the one-run spec are the union.
- `agg.accumulation_keys` equals the declared accumulation keys; a key that does not depend on the members is an error.

A wrong spec is then an error when the adapter is built, not a wrong result.

**In a session** an aggregation needs nothing of its own.

- The contributions are outputs, and the session holds outputs in memory.
  A chained combine therefore gets the previous combined contribution and the new member as objects, and combines two values.
  With sciline's `Reduced` accumulator, a push into a held accumulator is that same function call on those same two values, so a session that held accumulators would save nothing.
  The superseded combined contributions stay in the session's store until they are evicted, and D3 evicts outputs of superseded records first.
- A contribute request whose parameters differ from the previous one only in the run is routed to a stage whose stage input is the run, by the rule of "Stages in a session".
  What the members share, a reduced reference or a direct beam, is then computed once.
- A combine request that changes only a finalize parameter is routed to a stage of the combine spec with the contributions fixed.
  The adapter builds it by combining the contributions once, setting the combined values at the accumulation keys, and building a `sciline.Stage` with the finalize parameter as input.
- Successive combines of a growing series differ only in their contributions, so the session asks for a stage with the contributions as stage input.
  The adapter then makes the accumulation keys the inputs of the `sciline.Stage`, which holds what the contributions cannot affect.
  The combine workflow is a second adapter class beside the one for a plain pipeline, because it combines before it computes and returns an output, the combined contribution, that is not a target of the pipeline.

The design before the fifteenth pass had a combine callable that held accumulators "keyed by the references it last combined", and an adapter object that kept every contribution in a dictionary so that a run reduced on its own was not loaded again as a member of a sum.
Both are removed.
A person who expects to add runs to a result reduces the first run as a series of one, a contribute request and a combine request, and the contribution is then a record that the next combine references.

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

**A callable that may hold state, kept by the session runner.**
The design before the fifteenth pass, described under "Stages in a session".

**No chain declaration: always combine over all members.**
Correct, and simpler.
A series of k members then reads k contributions per arrival instead of two.
For event-mode contributions of gigabytes that is the cost D15 exists to avoid.

**An annotation on the parameter, `Accumulates(into='contribution')`.**
The form the fourteenth pass gave the declaration.
It put a property of the callable on one of its fields, with the other end named by a string.
The name described the direction of the data, which is the same for every combine, and not the property declared.

**The chain declaration on the rule instead of the spec.**
The person who writes a rule cannot know whether a combine is additive, and a wrong answer gives a wrong number without an error.

## Costs

- Up to three specs per pipeline.
  A UI that lists workflows shows the two building blocks beside the whole reduction.
- A rule with a series holds two templates, for the member and for the combine.
  They are versioned together as part of the rule.
- Changing a finalize parameter on a combined result, outside a session, is a combine request over the one previous contribution, and writes that contribution again.
- The shared-parameter check runs in the combine, not at validation.
- The contribute and the combine spec are versioned together by convention of the adapter.
- The contribution output mixes dimensions and types and carries the shared parameters, so its spec can declare its format but no `ArraySpec`.
- A value at an accumulation key must be storable in a scipp data group.
  essreflectometry accumulates a list of ORSO file entries beside its events; the author converts it.

## Open points this pass did not close

- **Two-level aggregation under a rule.**
  Reflectometry sums same-angle runs and then stitches angles.
  Both levels are expressible as specs.
  A rule has one combine clause; the second level is a second rule whose candidates are the completed combine records of the first.
  This is untested.
- **Roles.**
  A rule that feeds sample runs into one chained parameter and background runs into another needs a role per dataset and a mapping from role to parameter.
  D14 names "a series of fixed roles" without saying how.
- **The feedback cycle** of the Amor probe, members to combine to members, is unchanged.
- **Chaining between specs is checked by format only** (D13 finding of the Amor probe), so a contribution from the wrong spec passes validation and fails in combine.

## Phase 3

[stateless.md](stateless.md) lays out three models for interactive work.
With `Stage` the three hold the same objects and differ in placement: where the object between two stages lives, and whether its value is a record.

| Model | The stage and what sits after it |
|---|---|
| Session | The session holds the stage; each rerun is a request routed to it, recorded in a slot |
| Checkpoint | The application holds the stage; calls create no records; a kept result is a request to a throwaway runner |
| Stateless with splits, first rung | Two specs with the value between them stored as a record; each rerun is a throwaway process calling the second |
| Stateless with splits, second rung | A kept runner that holds the output the second spec reads, addressed by that reference |

Session and checkpoint now differ only in who holds the stage and whether a call writes a record.
The fold is the second rung for a combine spec: a kept runner that holds the latest combined contribution of one series, chains in memory, and writes a record every n arrivals.
Phase 3 still does not need deciding now.
Whichever model is chosen, the callables it keeps are built and tested in phases 1 and 2.

## What this does not solve

- **Memory policy.**
  A stage holds its frontier, an aggregation holds nothing, and a session holds stages and outputs.
  The session can count its stages and drops the least recently used; how that bound relates to the memory budget of D3 is open.
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
- **Finalize inputs** need no change to sciline.
  `Aggregation` builds its finalize stage with the accumulation keys as its only inputs, so a finalize parameter cannot be given per call.
  The adapter does not use that stage for a rerun: it sets the combined values at the accumulation keys on a copy of the pipeline and builds a plain `Stage` with the finalize parameter as input.
  The `finalize_inputs=` argument this note asked for earlier is not needed.
- **A parameter read by both stages** needs to reach the finalize stage with the value the contributions were made with.
  Within one process the snapshot guarantees it.
  Across processes the adapter carries it in the contribution.
  The design document could name this as the consumer's responsibility.
- Applied on the branch on 2026-09-14: the condition for combining combined values on `Accumulator`, the lock on a stage's held part, and the stale evidence paragraph.

## State of the skeleton

The skeleton implements this note: `PipelineAdapter` and `Aggregation` in `ess.apps` are the adapter, `Stages` holds the stages of a session, the spec has the `chain` field, and no component of the framework branches on whether a spec is a contribute, a combine, or a plain one.
The adapter stays in essapps until `ess.reduce.spec` of scipp/ess#690 has merged.

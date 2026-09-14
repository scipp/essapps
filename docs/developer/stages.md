# Stages and aggregations in the architecture

Companion to [architecture.md](architecture.md), [stateless.md](stateless.md), and [staging.md](staging.md).
It reads the sketch against scipp/sciline#245, ADR 0003 with its design document `map-reduce-outside-the-graph.md`, which replaces `Pipeline.map`/`reduce` with composition outside the graph: a **Stage**, the part of a graph from named input keys to named output keys with everything else computed once and held at its frontier; an **Accumulator**, any object with `push` and a `value`, sitting between stages, with `Buffered(func)` making one from an n-ary combine function; and an **Aggregation**, two stages of one pipeline, contribute and finalize, with an accumulator factory per **accumulation key** between them, holding nothing itself.
`Stage`, `warm`, `Accumulator`, `Buffered`, `Aggregation`, and `compute_members` are sciline's; a `Forwarder` that holds a value until it is replaced, and the existing accumulators with their `clear`, are ess.reduce's; the loop that calls stages and pushes into accumulators is the caller's, a package object such as esssans's or this framework's wrapper.
The question here is whether those objects are what the sketch's execution side needs, what they change in its text, and what they do not solve.
Analysis, not decision; judgments are marked.
The points that should go back to the sciline proposal are collected in their own section.

## The correspondence

The sketch has four places where a workflow is run in parts, and it describes each in its own words.
They are one construction seen from four sides.

| In the sketch | Where | As stages and accumulators |
|---|---|---|
| Warm workflow: the wrapper caches the nodes just upstream of the cheap parameters and reruns what lies downstream | D8, `warm.py` in the skeleton | `Stage(pipeline, outputs=targets, inputs=cheap_keys)`; the cache is the stage's frontier |
| Contribute, combine, finalize: the graph up to the accumulation keys, the addition, the graph from the keys to the targets | D15 | `Aggregation(pipeline, members=member_keys, accumulators={key: factory}, outputs=targets)`, whose three entry points are those three |
| A stage output: a value one spec produces and another takes, at a cut the author chose | D4, phase 3 split model | The frontier of a `Stage`, held in a forwarder that is a record |
| The fold: a process holding the running contribution and writing a combine record every n arrivals | D15, stateless.md | Accumulators from `agg.accumulators()` held by a process; their `value` is the record |

The skeleton's `WarmPipeline` is already the first row: its `frontier()` computes the nodes not downstream of a cheap key that feed one that is, holds their values, and reruns from them.
That is the `Stage` frontier, with the same rule and the same "extra parameters are harmless" remark.
What the skeleton adds on top, the field-name-to-key mapping, the `reused` flag, and the rebuild when an expensive parameter changes, stays; what it does by hand, the graph walk and the cache, becomes the library's.

The vocabulary lines up.
The sketch's "stage output" is the value at a `Stage` boundary; its accumulation key is the aggregation's, the word taken from the ADR in place of the earlier "accumulation point"; its "contribution" is the aggregation's contribution, a mapping from accumulation keys to values; its "combine" is a push of each contribution into fresh accumulators and a read of their values; its "fold" is a process holding accumulators.
Only the last has no class behind it, and none is needed: the ADR dropped `Fold` as a class name because scipp uses the word for reshaping, and the sketch's fold is a deployment shape, not an object.
Judgment: "fold" stays as the name of the process shape, since it names a deployment and not an object, and renaming it would touch D15, stateless.md, staging.md, and the deck for no gain in precision.

## What changes in the text

Small, and all in one direction: declarations the sketch asks of the author become derivations from the graph, or declarations the binding checks against the graph.

**D8.**
The spec still declares the cheap parameters, because that declaration answers a UI question, slider or run button, that the graph cannot.
The sentence "the wrapper caches the nodes just upstream of them" becomes a property of `Stage` rather than a rule the wrapper implements, and "reuse is correct by construction from the sciline graph" is now literally true: the stage is a snapshot of the pipeline, and a value it holds cannot be affected by an input it takes.
`Stage` refuses an input the outputs do not need, so a cheap parameter that never reaches the targets is a bind error rather than a dead slider.
The warm-equals-cold helper stays, since providers may be impure and the snapshot does not know; so does the `reused` flag.

**D13 and D15, which parameters finalize reads.**
The binding can derive the split: `contribute_stage.keys` and `finalize_stage.keys` say which parameters each stage reads, and a parameter in both, read upstream and downstream of the accumulation keys, is contribute's, because changing it invalidates the contributions.
An earlier reading of this note, and the sciline design document's paragraph on this framework, concluded that the declaration in D13 can therefore go.
Judgment: keep the declaration and check it, for the reason D8 keeps the cheap parameters declared.
The backend validates a combine request at submission and never imports workflow code, and a combine form or a rule's combine template has to know which fields it has; only the spec can tell them.
The binding derives the split at bind time and refuses a spec whose declaration disagrees with the graph, so a wrong declaration is a bind error rather than a wrong result, and the sketch's remark that the two sets "usually coincide with the expensive and cheap parameters of D8" becomes something the binding can report.
A combine request that reaches the runner with a contribute parameter is refused there as well, which is the runnability check the sketch did not have.

**D15, the chained series.**
A combine request in a throwaway process is `agg.combine([previous, new])` followed by `agg.finalize(...)`: fresh accumulators, two pushes, one read, and no computation before finalize.
Building the aggregation in that process walks the graph and computes nothing, because a stage computes its static part on first use and `combine` touches no stage; the process pays the imports, the read of two contributions, and the write of one, which is what D15 says a chained arrival costs.
The member run of a series is `agg.contribute(row)` with the member's full parameter set as the row, so its contribute stage has no static part, which is right for a process that runs once.

**D15, the associativity helper.**
"A test helper runs contribute, combine, and finalize over a list of members in two groupings and compares with the one-shot callable" is `Aggregation.compute(table)` against `finalize(combine([combine(first), combine(rest)]))` under two splits and a permutation, with no workflow-specific code, so the helper is written once.
It checks one thing more that the sciline protocol does not state: that a combined value can be pushed back in.
`Aggregation.combine` accepts "contributions of members, or combinations of such", and the chained series and the fold both rely on that, but `Accumulator` promises only `push` and `value`.
`Buffered` has the property when its function is associative; a running-total accumulator from ess.reduce has it only if its `push` accepts a value of the type its `value` returns, which a histogramming accumulator does not unless it histograms conditionally.
Associativity and commutativity remain what the author declares and the helper checks; the closure is the same declaration seen from the accumulator's side.

**D15, adding and removing a member.**
`Aggregation` holds nothing, so the contributions live in one of three places: the record store for a chained series, the wrapper's mapping by member label in a session, accumulators in a process for the fold.
One more member costs one contribution, and one fewer costs a combine over the rest, from the held contributions or from their disk copies.
That is the sketch's rule for removal, "a combine referencing the remaining contributions, never a subtraction", as the natural shape of the holder rather than a rule the framework enforces.
Whether a held contribution survives a parameter change is one test, `key in agg.contribute_stage.keys`, and the wrapper makes it.

**D15, hierarchy inside one record.**
"Inside one record, angle groups in a Bifrost run or chunks of an NMX file, the callable applies the same three stages itself" is an inner aggregation whose finalize output is a member of the outer one, or whose accumulation key is the outer one's member key, since a stage passes an output that is an input through.
The sentence stands; it now names a construction rather than an aspiration.

**The apply operation and the member table (D14).**
Apply "in a notebook accepts a DataFrame, member key as index and typed values as columns".
The aggregation takes that table as a `Mapping[label, Mapping[Key, value]]`, which `df.to_dict('index')` produces, with the member label as the key; pandas stays at the client, and the wrapper maps field names to sciline keys as the skeleton's `WarmPipeline` already does.
A batch summed as one request is one `Aggregation.compute` over that table; a batch of independent members is `compute_members` over it, one run per row; a rule's series is the table growing by one row per arrival.
The member keys are whatever varies across the members of the request at hand, not a spec declaration: the table's columns for a summed batch, the data references for a session's growing list, everything contribute reads for a member run of a chained series.
Non-member parameters are shared by construction within one request; across the records of a chained series they are not, and a combine over members contributed with different masks concatenates their events without complaint.
Judgment: since the binding knows which parameters contribute reads, the runner can require the referenced member records to agree on them before combining, which turns a silent mix into a refusal; add it to the runnability check of a combine request.

**Glossary.**
Add stage, accumulator, accumulation key, and aggregation as the mechanisms, and point the definitions of accumulation key, contribution, combine request, and stage output at them.

## Phase 3

stateless.md lays out three models for interactive work and says they differ in where the state lives.
With `Stage` the three hold the same object and the difference is placement alone: where the object between the two stages lives, an accumulator or a forwarder, and whether its value is a record.

| Model | The Stage and what sits after it |
|---|---|
| Session | Held by the session's runner; each rerun is a call with the cheap parameters, recorded in a slot |
| Checkpoint | Held by the application; calls create no records; a kept result is a cold request |
| Stateless with splits, first rung | Two stages with a spec boundary between them; the forwarder's value is a stored stage output, and each rerun is a throwaway process calling the second stage |
| Stateless with splits, second rung | The second stage held by a warm runner keyed by the frontier reference it holds |

The stateless note recommends measuring the throwaway feedback loop "cold, with a warm process pool, and with a runner that already holds the intermediate in memory".
Those are one `Stage` built per call, one built once per process, and one built once and called; the spike can measure all three with one class.

The note also says the fold "is the third model's second rung" and that solving one solves the other.
That is now visible in code: a keyed warm runner holds a `Stage` for a cheap-parameter loop and a set of accumulators for a series, and both are addressed by what they hold.

The sciline proposal considered and deferred a generic object that holds a network of stages and accumulators and routes pushes through it.
The candidates for it are the per-package objects, such as esssans's, holding a pipeline, two aggregations, a finalize stage, and contributions, and this framework's wrapper, which holds the same things for one aggregation.
If they turn out to be the same code, or if phase 3 places accumulators on process boundaries, that object is their generalisation, and the loops written in phases 1 and 2 say what it must do.
Judgment: phase 3 still does not need deciding now, and the reason is stronger than before.
Whichever model is chosen, the object it runs is built and tested in phases 1 and 2, because D15's throwaway contribute and combine are the same `Aggregation` calls.

## Where it lives

"The framework never imports sciline" holds.
`Stage` belongs in sciline, since it needs the graph; so does `Aggregation`, which the ADR places there as the documented replacement for `map(...).reduce(...)` that users outside ESS need, holding no contributions, no member table, and no invalidation rule.
`Forwarder` and the accumulators that keep a running total belong in ess.reduce next to `StreamProcessor`, since that is where the combine functions and the drivers live.
The framework sees the wrapper's callable, or for a declared contribution its three entry points, and nothing behind them.

The wrapper sets a request's parameters on the pipeline, builds one `Aggregation` from the spec's accumulation keys, the accumulator factory per key, and the member keys of the request at hand, holds the contributions where the execution shape says, and maps between the aggregation's world and the spec's: the contribution, keyed by sciline types, to the typed output the spec declares, a data group keyed by field name, and back.
That mapping is the wrapper's, is fixed per spec version, and is the same as the mapping the skeleton's `WarmPipeline` already keeps between field names and keys.
The accumulator per key is part of the declaration: `Buffered` over the package's combine function is the default, and a running-total accumulator is the author's choice where a sum over large dense arrays should hold one array instead of one per member.

## What it does not solve

- **Memory policy.**
  A `Stage` holds its frontier; an `Aggregation` holds nothing; whoever loops holds the contributions or the accumulators.
  A first draft of the sciline proposal also held what the graph computes from a member alone, so that a parameter change upstream of the accumulation keys would not reload; on LoKI that bought nothing, because the wavelength conversion reads a parameter and everything below it reruns anyway.
  Holding more needs a second tier, parameters declared as rarely changing, which is `StreamProcessor`'s context.
  The fold holds accumulators, so its memory is the accumulator's choice: a running total holds one array, `Buffered` holds every contribution, which for concatenated events is the same size either way and for dense sums is not.
  The sketch's rule that a memory cache is private to a process and never registered (D3) applies to all of it.
  Judgment: frontier always, contributions in a session, since they are what D15 stores anyway; running-total accumulators in the fold; nothing else.
- **Parallelism over members.**
  In-graph mapping ran the members of a batch in one scheduler, and the LoKI validation measures what that gave: under the dask scheduler the map/reduce reference is faster by the two sample runs it ran in threads.
  An `Aggregation` runs them as the driver says: in the framework that is the launcher, one throwaway process per member, which is the sketch's shape already; inside one process it is a thread pool over `contribute`, which the wrapper must provide for the summed-batch-in-one-request case.
  `Stage` computes its static part once even when called from several threads, but the wrapper should still warm all stages together before it maps over rows, so that static work the stages share is done once.
- **Live streams.**
  `StreamProcessor` on `Stage` has members that cannot be recomputed and a context that changes without invalidating what was accumulated.
  The stateless note's boundary stands: that is where authoritative state lives in memory, it is esslivedata's, and nothing here moves it into scope.
- **Static work across processes.**
  Stages of one aggregation in one process share their static work through `warm`; a contribute in a throwaway process recomputes it.
  That is the phase 3 cost the stateless note already measures, not a new one.

## Points for the sciline proposal

Read against the ADR, the design document, and the modules on the PR branch at 010ca83; all five were applied on that branch on 2026-09-14, the first two in the design document's sections 6.7 and 4 and the `Accumulator` docstring, the fourth as a lock on the stage's held part.

- **The paragraph on this framework** in the design document (now section 6.7) said a chained series is "an accumulator per accumulation key, from `agg.accumulators()`, into which essapps pushes each contribution as it arrives".
  That describes the fold, phase 3, where a process holds accumulators.
  The chained series of phases 1 and 2 is `combine([previous, new])` in a throwaway process per arrival, holding nothing; and the wrapper holding contributions by member label is the session case, where members are removed.
  The design document's migration section points at the edit list at the end of this note, whose earlier form dropped the D13 declaration; the list now keeps it, checked against the graph.
- **Combining combined values** is used by `combine`'s docstring, the chained test, and every consumer here, but nothing states the condition on the accumulator: its `value` must be pushable and the result independent of grouping and order.
  `Buffered` has it when its function is associative; an ess.reduce accumulator has it only if `push` accepts its own `value` type, which the histogramming ones do through `maybe_hist` today and will need to keep once that moves.
  One sentence on `Accumulator` or on `combine` would say what an author who writes a running-total accumulator for an aggregation has to provide.
- **The evidence section** says the prototype "reads `TaskGraph._graph` for the concrete graph and hardcodes the naive scheduler inside stages; both go away inside v2".
  The modules on the branch build from `to_task_graph` with `HandleAsComputeTimeException` and take a scheduler argument through `scheduler_or_default`, so the sentence is stale.
- **`Stage.static` under threads.**
  The property computes the static part on first use with no guard, and the design document recommends mapping `contribute` over rows with threads.
  Two cold calls in parallel compute the frontier twice, which for a file read is the cost `warm` exists to avoid and for an impure provider gives two frontiers.
  Either the user-guide example warms first and the docstring says so, or `Stage` takes a lock.
- **Static accumulation keys** pass silently to finalize, which the ADR lists as a consequence for package tests to check.
  For this framework a declared accumulation key that does not depend on the members is a bind error, since a contribution without it is not what the spec promised; the wrapper compares `agg.accumulation_keys` with the declaration.
  No change to sciline is needed, but the ADR could name the check as the consumer's rather than leave it to tests.

## Settled in the proposal since the draft this note first read

- `Fold` is `Aggregation`, "cut key" is accumulation key, `at=` is `accumulators=`, `fold.cut` is `agg.accumulation_keys`.
- The combine protocol is an accumulator per accumulation key, given as a factory, with `Buffered(func)` for n-ary functions; the draft took n-ary functions directly.
  `combine` and `compute` make fresh accumulators per call, and `accumulators()` hands instances to a caller that pushes.
- `Aggregation`, `Accumulator`, `Buffered`, and `compute_members` are in sciline, not ess.reduce; the draft placed the fold beside `StreamProcessor`.
- The member table is `Mapping[label, Mapping[Key, value]]`; pandas stays out of sciline.
- Parallelism over members is the caller's; `Aggregation` takes no executor.
- `Stage` and `Aggregation` take a scheduler, defaulting to dask when installed.
- `compute(table)` pushes each contribution as it is made rather than holding the list, and refuses an empty table.
- No experimental label; the staged rollout is the trial period, and `provide`, a reporter on `Stage`, and visualization over stages are follow-ups.

## Suggested edits to architecture.md

Not applied; for the next pass.

- D8: replace "the wrapper caches the nodes just upstream of them" with a reference to the stage's frontier, note that the warm sciline wrapper is a `Stage` with the cheap parameters as inputs, and that a cheap parameter the targets do not need is refused at bind time.
- D13, contribution output: keep "and declare which parameters finalize reads"; add that the binding derives the split from the graph, refuses a spec whose declaration disagrees with it, and refuses a combine request carrying a contribute parameter.
- D15: name `Aggregation` as what the wrapper builds from the accumulation keys, the accumulator per key, and the member keys of the request; say the associativity helper is generic and also checks that a combined value can be pushed; say removal is a combine over the contributions the holder keeps; add the memory policy sentence; soften "an accumulator that concatenates, which `ess.reduce.streaming` does not have" to `Buffered` over the package's concatenation; add the runnability check that referenced members agree on the contribute parameters.
- D14, apply: say that the member table is the aggregation's table with the member key as its label, and that the member keys are whatever varies in the request at hand.
- Glossary: stage, accumulator, accumulation key, aggregation.
- Next step: the D3/D6 spike's fake workflow with two accumulation keys is an `Aggregation`, and the feedback-loop measurement is one `Stage` under three lifetimes.

# Stages and folds in the architecture

Companion to [architecture.md](architecture.md), [stateless.md](stateless.md), and [staging.md](staging.md).
It reads the sketch against a proposal for sciline, `map-reduce-outside-the-graph.md` on the sciline branch of that name, which replaces `Pipeline.map`/`reduce` with two objects derived from a flat pipeline: a **Stage**, the part of a graph from named input keys to named output keys with everything else computed once and held, and a **Fold**, a member table and cut keys with a combine function per key, built from two stages of the same pipeline.
The question here is whether those two objects are what the sketch's execution side needs, what they change in its text, and what they do not solve.
Analysis, not decision; judgments are marked.

## The correspondence

The sketch has four places where a workflow is run in parts, and it describes each in its own words.
They are one object seen from four sides.

| In the sketch | Where | As a Stage or Fold |
|---|---|---|
| Warm workflow: the wrapper caches the nodes just upstream of the cheap parameters and reruns what lies downstream | D8, `warm.py` in the skeleton | `Stage(pipeline, outputs=targets, inputs=cheap_keys)`; the cache is the stage's frontier |
| Contribute, combine, finalize: the graph up to the accumulation keys, the addition, the graph from the keys to the targets | D15 | `Fold(pipeline, members, at=accumulation_keys, outputs=targets)`, whose three methods are those three stages |
| A stage output: a value one spec produces and another takes, at a cut the author chose | D4, phase 3 split model | The frontier of a `Stage`, crossing a spec boundary as a record |
| The fold: a process holding the running contribution and writing a combine record every n arrivals | D15, stateless.md | A `Fold` whose combined contribution is held between `combine` calls |

The skeleton's `WarmPipeline` is already the first row: its `frontier()` computes the nodes not downstream of a cheap key that feed one that is, holds their values, and reruns from them.
That is the `Stage` frontier, with the same rule and the same "extra parameters are harmless" remark.
What the skeleton adds on top, the field-name-to-key mapping, the `reused` flag, and the rebuild when an expensive parameter changes, stays; what it does by hand, the graph walk and the cache, becomes the library's.

The vocabulary lines up without renaming.
The sketch's "stage output" is the value at a `Stage` boundary; its "accumulation point" is a `Fold` cut key; its "contribution" is the fold's contribution, a mapping from cut keys to values; its "fold" is a `Fold` whose partial lives in memory.
`Fold` is a class and the sketch's fold is a deployment of it, and that is the right relation.

## What changes in the text

Small, and all in one direction: declarations the sketch asks of the author become derivations from the graph.

**D8.**
The spec still declares the cheap parameters, because that declaration answers a UI question, slider or run button, that the graph cannot.
The sentence "the wrapper caches the nodes just upstream of them" becomes a property of `Stage` rather than a rule the wrapper implements, and "reuse is correct by construction from the sciline graph" is now literally true: the stage is a snapshot of the pipeline, and a value it holds cannot be affected by an input it takes.
The warm-equals-cold helper stays, since providers may be impure and the snapshot does not know; so does the `reused` flag.

**D13 and D15.**
"A spec may mark one output as its contribution and declare which parameters finalize reads" loses its second half.
Which parameters finalize reads is the set of parameters that are not ancestors of the cut keys, and the wrapper derives it at bind time; a combine request that carries any other parameter is refused there, which is a runnability check the sketch did not have.
The remark that "the two sets usually coincide with the expensive and cheap parameters of D8, but are separate declarations" becomes: they are two derivations from the same graph, one from the cheap parameters and one from the cut keys, and the sketch's observation that they usually coincide is now something the wrapper can report.

**D15, the associativity helper.**
"A test helper runs contribute, combine, and finalize over a list of members in two groupings and compares with the one-shot callable" is `Fold.compute` against `Fold.finalize(Fold.combine(...))` under two groupings, with no workflow-specific code, so the helper is written once.
The combine function is n-ary, as `reduce(func=)` is today; the framework's chained combine is the n-ary function with two arguments, and associativity remains a property the author declares and the helper checks.

**D15, hierarchy inside one record.**
"Inside one record, angle groups in a Bifrost run or chunks of an NMX file, the callable applies the same three stages itself" is an inner `Fold` over groups whose finalize output is the member of the outer one.
The sentence stands; it now names a construction rather than an aspiration.

**The apply operation and the member table (D14).**
Apply "in a notebook accepts a DataFrame, member key as index and typed values as columns".
A `Fold`'s member table has the same shape.
A batch summed as one request is one `Fold` over that table; a batch of independent members is the same table with no cut, one run per row; a rule's series is the table growing by one row per arrival.
One table shape from the form, the trigger loop, and the wrapper is a small win that the sketch's separate descriptions of batch and combine did not make visible.

**Glossary.**
Add stage and fold as the two mechanisms, and point the definitions of accumulation point, contribution, and stage output at them.

## Phase 3

stateless.md lays out three models for interactive work and says they differ in where the state lives.
With `Stage` the three hold the same object and the difference is placement alone.

| Model | The Stage |
|---|---|
| Session | Held by the session's runner; each rerun is a call with the cheap parameters, recorded in a slot |
| Checkpoint | Held by the application; calls create no records; a kept result is a cold request |
| Stateless with splits, first rung | Two stages with a spec boundary between them; the frontier is a stored stage output, and each rerun is a throwaway process calling the second stage |
| Stateless with splits, second rung | The second stage held by a warm runner keyed by the frontier reference it holds |

The stateless note recommends measuring the throwaway feedback loop "cold, with a warm process pool, and with a runner that already holds the intermediate in memory".
Those are one `Stage` built per call, one built once per process, and one built once and called; the spike can measure all three with one class.

The note also says the fold "is the third model's second rung" and that solving one solves the other.
That is now visible in code: a keyed warm runner holds a `Stage` for a cheap-parameter loop and a `Fold` for a series, and both are addressed by what they hold.

Judgment: phase 3 still does not need deciding now, and the reason is stronger than before.
Whichever model is chosen, the object it runs is built and tested in phases 1 and 2, because D15's throwaway contribute and combine are the same `Fold` calls.

## Where it lives

"The framework never imports sciline" holds.
`Stage` belongs in sciline, since it needs the graph; `Fold` belongs in ess.reduce next to `StreamProcessor`, since that is where the combine functions and the wrapper live.
The framework sees the wrapper's callable, or for a declared contribution its three entry points, and nothing behind them.

The runner's contract needs one addition the sketch implies but does not state.
A contribution crosses processes in shared mode, so the wrapper must map the fold's contribution, keyed by sciline types, to the typed output the spec declares, a data group keyed by field name, and back.
That mapping is the wrapper's, is fixed per spec version, and is the same as the mapping the skeleton's `WarmPipeline` already keeps between field names and keys.

## What it does not solve

- **Memory policy.**
  A `Stage` holds its frontier; a `Fold` may hold a per-member frontier, so that a rerun does not reload members, and per-member contributions, so that removing a member is a combine over the rest.
  For event data all three are large.
  The sketch's rule that a memory cache is private to a process and never registered (D3) applies to all of them, and the cheap-parameter declaration decides what a session holds; but which of the three a session holds is a policy the wrapper exposes and the sketch should name.
  Judgment: frontier always, per-member frontier in a session, contributions never by default, since D15 already says removal may recompute from the raw files.
- **Parallelism over members.**
  In-graph mapping ran the members of a batch in one scheduler.
  A `Fold` runs them as the driver says: in the framework that is the launcher, one throwaway process per member, which is the sketch's shape already; inside one process it is a thread pool over `contribute`, which the wrapper must provide for the summed-batch-in-one-request case.
- **Live streams.**
  `StreamProcessor` on `Stage` has members that cannot be recomputed and a context that changes without invalidating what was accumulated.
  The stateless note's boundary stands: that is where authoritative state lives in memory, it is esslivedata's, and nothing here moves it into scope.
- **The synthesized node.**
  `Fold.as_pipeline()` grafts the fold into a flat pipeline so that existing `with_*` helpers and the UI's `parameter_mappers` keep working.
  The framework never sees it; the wrapper uses `Fold` directly.
  It is a bridge for ess.reduce, not a concept here.

## Suggested edits to architecture.md

Not applied; for the next pass.

- D8: replace "the wrapper caches the nodes just upstream of them" with a reference to the stage's frontier, and note that the warm sciline wrapper is a `Stage` with the cheap parameters as inputs.
- D13, contribution output: drop "and declare which parameters finalize reads"; add that the binding derives it and refuses a combine request carrying any other parameter.
- D15: name `Fold` as what the wrapper builds from the accumulation keys, say the associativity helper is generic, and add the memory policy sentence.
- D14, apply: say that the member table is the fold's member table.
- Glossary: stage and fold.
- Next step: the D3/D6 spike's fake workflow with two accumulation points is a `Fold`, and the feedback-loop measurement is one `Stage` under three lifetimes.

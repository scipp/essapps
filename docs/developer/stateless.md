# What all-in stateless would remove, and what it would cost

Companion to [architecture.md](architecture.md) and [staging.md](staging.md).
The sketch keeps state between runs in one place, the session, so that interactive work can rerun a warm workflow in memory.
This note asks what the design looks like if it never does that: no caching, no reuse, no accumulation, and a changed parameter means a fresh process.
It then asks whether that makes phases 1 and 2 a separate, simpler system from phase 3, and what the three ways of doing phase 3 look like.

## The variant

Every run is a throwaway process.
Inputs are read from disk, outputs are written to disk before the run reports completion, and the process exits.
A record is exactly what the sketch already says it is; nothing about records changes.
The client interface, the spec vocabulary, the record store, the scheduler, the trigger loop, and publication are unchanged.
The only difference from the sketch is that the second execution shape, and everything that exists to make it safe, is gone.

The sketch itself notes that an earlier version said exactly this, and rejected it because "it makes interactive loops disk-bound and rebuilds the workflow on every rerun, so the feedback loop never gets below seconds".
That objection is about phase 3 alone.
For phases 1 and 2 the variant is not a variant; it is what the sketch already does in shared mode.

## What disappears

By part of the sketch.
The list is long because the session touches many sections, not because any one of them is large.

**Decisions.**

- D2 loses its second half: sessions, the two invariants, the definition of a session by its owner, local mode as a session in the client's process, and the remote-session forms.
- D3 loses the private memory caches, the sentence that the registry knows disk copies only, the two execution shapes, the rule that a group runs in one shape, the memory lifetime, and the cache-coherence argument.
  What remains is a disk tier with a registry, a read cache in the view server if measurements ask for one, and retention.
- D4 loses its second reason and the remark that it disappears inside a session.
  The rule becomes: split a workflow wherever a stage should be reusable or tunable on its own, always.
- D8 loses the warm workflow, the sciline wrapper and its frontier, the declaration of cheap parameters as a framework concept, the accumulation special case, the warm-equals-cold helper, and the `reused` flag.
  In-process binding goes with it, because a throwaway process imports code by name.
- D10 loses slots and everything on them.
  Views stay, served by the service from disk copies, with one routing rule instead of three.
- D11 loses the cold-recompute-before-publish rule and the refusal of in-process records, because every record is cold and every binding is an installed package.

**Components.**
The session.
The launcher has one shape and no `needs_disk_inputs`.
The runner has no keep flag.
The data store has no cache, no `write_out`, no `evict`.
The record store has no slot column and no latest-by-label query.

**Rules and open questions.**
Session loss as a failure event, and the sentence that session runs have no liveness timeout.
Remote sessions, the session launcher, idle timeout, and session cap, as deferred items and as an open question.
The two-notebooks question changes shape: a notebook is a client of the service over the transport, or it runs a private throwaway backend of its own, and a private record store is uploaded to the shared one by the deferred upload operation in either case.
The interpreter-sharing cost of a one-process application.

**Code.**
In the skeleton, roughly one seventh of the source, isolated in `warm.py`, the session launcher, the runner's keep flag, the data store's cache paths, the slot column, in-process binding, and the local-file helpers; see the table in [staging.md](staging.md).
The tests for those parts go with them.

**What stays is still most of the system.**
Records and references, the spec vocabulary, stand-in resolution, the scheduler with pending outputs, the trigger loop, templates and batch, three-layer validation, completion markers and reconciliation, publication with its snapshot, proposal scoping, retention.
Facilities that built automatic and batch reduction report that these, and the monitoring on top of them, were where the value was.
Statelessness does not make the framework small; it makes it uniform.

## What it costs

The costs land in three places: the feedback loop, the disk, and the workflow authors.

**The feedback loop.**
A rerun pays a process start, the imports, the reads of every input, and the full computation of the requested stage.
Orders of magnitude, to be measured in the spike rather than trusted from here:

| Step | Warm session | Throwaway process |
|---|---|---|
| Process start and imports of scipp, sciline, and the instrument package | none | 2 to 5 s |
| Reading a large binned intermediate from scipp HDF5 | none, in memory | 1 to 10 s |
| The expensive stage, such as loading and converting a SANS run | none, cached | 10 s to minutes |
| The cheap stage, such as histogramming in Q | under 1 s | under 1 s |

With eager splitting, so that the expensive stage is its own record and the cheap stage reads its stored output, a rerun of the cheap stage costs the first two rows: a handful of seconds.
Without splitting it costs all of them.
Either way it is "change and press run", not a slider.

A pool of warm *processes*, idle runners with imports done and no workflow state, removes the first row and keeps every stateless property, because state still crosses only through disk.
It is a launcher implementation detail, not a design change, and it is the cheapest mitigation available.

**The disk.**
Every stage output that a user wants to iterate on is written, and for event data those are the large ones.
The sketch avoided this on purpose: "a stage output can be huge, and writing one to disk is wasted work when its only consumer is in the same process".
Stateless pays that write once per expensive-stage run and the read once per rerun, and retention has more to manage.

**The workflow authors.**
Splitting becomes mandatory wherever a user will iterate, and the cut must be at a stored intermediate the framework can serialize.
The sketch already notes that the cut is not always clean, with processed vanadium in diffraction as the example.
Every technique's notebook today has such a cut, the point after loading and coordinate conversion where the parameters people move come in, so the authors know where it is; the cost is that the cut becomes a spec boundary and its output a stored, typed, retained artefact rather than a variable.

**Accumulation.**
Adding one run to a sum of fifty need not recompute fifty loads.
With per-run stage outputs on disk the sum reads fifty-one small intermediates, and a sum can also be expressed as a chain, a new request that references the previous sum's output and the new run.
That is stateless accumulation with exact provenance and no accumulator.
It stops working only when the accumulated quantity is too large to store, which is the case the in-process `StreamProcessor` exists for.

## The user stories under the variant

Only the stories that the session touched change outcome.

| Story | Under the sketch | All-in stateless |
|---|---|---|
| B1 tune a SANS reduction, feedback within a second or two | Fits in a session | Fails the timing check; a handful of seconds per change with splitting, more without |
| B2 add a run to a sum, then remove one | Accumulator in the warm workflow | Fits, by chaining or by reading stored per-run outputs |
| B3 compare two variants | Two slot labels | Fits, two records, the UI keeps two IDs |
| B4 explore a 4D volume | Served from session memory | Needs the deferred chunked on-disk layout, or the application loads the volume itself |
| B5 kernel dies mid-session | Records survive, warm state is recomputed | Fits better: there was no state to lose |
| C5 vanadium and sample tuned together | Two warm workflows chained in memory | Correct but slow: each vanadium change reruns both stages cold |
| F3 publish what was tuned | Recompute cold first | Fits trivially; the rule is gone |
| G2 developer iterates on a workflow | In-process binding | Editable install plus a throwaway run; a slower loop |
| G3 local application, remote compute | Session on the laptop | The stage output crosses once; the cheap stage reruns in local throwaway processes |
| All others | | Unchanged |

Two of the stories that fail, B1 and C5, are the ones the sketch was built around.
Whether they must be met by the framework or can be met by the application is the phase 3 question.

## Three ways to do phase 3

The stateless core does not decide phase 3; it only refuses to pay for it early.
When phase 3 arrives there are three models, and they differ in where the interactive state lives.

**The session model**, which is the sketch.
State lives in a session the framework knows about.
Every slider move is a complete record in a slot; the framework guarantees, through the wrapper and the test helper, that the warm result equals the cold one; publication recomputes cold to be sure.
Interactive work in the shared web UI needs remote sessions, which the framework must launch, route requests to, time out, and cap.

**The checkpoint model.**
State lives in the application's process and the framework does not know about it.
The application holds its workflow object, reruns it in memory as a notebook does, wrapped by the same sciline wrapper if it wants cheap reruns, and plots with plopp or with the view function called in-process.
None of that creates records.
When the user keeps a result, the application submits the complete parameter set as an ordinary request, which runs cold in a throwaway process and yields a record indistinguishable from a batch member.
The application may compare the cold output with what was on screen and warn if they differ; that is the warm-equals-cold check, moved to the one moment it matters.
Templates are saved from checkpoints; publication reads checkpoints; chains are checkpointed as a group.
Shared interactive use is then a hosting question, a process per user as JupyterHub or a per-user web server already provides, and the framework never sees a session.

**The stateless model with eager splits.**
Every change is a fresh run of the cheap stage over a stored intermediate.
No new concept at all, one code path, and a feedback loop of seconds rather than sub-second.

| | Session model | Checkpoint model | Stateless with splits |
|---|---|---|---|
| Feedback on a cheap parameter | Sub-second | Sub-second | Seconds |
| Records created while exploring | One per change, hidden by slots | None | One per change |
| Provenance of a kept result | Complete | Complete | Complete |
| What the user saw equals the record | By the wrapper's rules plus a test helper | Checked once at checkpoint, cold | By construction |
| New framework concepts | Session, warm workflow, cheap parameters, slots, private caches, two shapes | None; the wrapper becomes a library for applications | None |
| Interactive use in the shared web UI | Remote sessions owned by the framework | A hosted process per user, owned by infrastructure | Works, slowly |
| Disk volume | Low | Low | High |
| Burden on workflow authors | Declare cheap parameters and cache nodes correctly | None beyond the callable | Split at every tunable boundary |
| Losing the process | Lose time; every step was recorded | Lose the exploration since the last checkpoint | Lose nothing |
| Exploring a large volume in the browser | Views from session memory | Views from the application's memory, or a hosted process | Chunked layout on disk |

The checkpoint model keeps what the session model was for, sub-second reruns and chained tuning, and drops what it cost, because the framework's only promise about interactive work becomes "a record is a cold run", which the core already promises.
Its weak point is the same as the session model's: shared interactive use needs a process per user somewhere, and it answers that with infrastructure rather than with framework code.
Its other cost is that a checkpoint takes as long as a cold run, once per kept result.

## Should phases 1 and 2 be split from phase 3

At the record layer, no.
The stories that give the project its value cross the boundary: what was tuned interactively becomes the template for batch and automatic reduction, a stage output tuned in a notebook feeds a batch, and a published result has the same provenance whichever way it was made.
That requires one spec vocabulary, one request, one record, and one client interface across all three phases.
The sketch has these, and they are the parts that are already stateless.

At the execution layer, yes, and the sketch almost says so: "initially a session exists only in local mode", "the shared web UI initially has no interactive loop".
The staging note's recommendation follows from this.
Build phases 1 and 2 on the core alone, with one execution shape, and keep the session concepts out of the core documents until phase 3 is designed.
Under the checkpoint model the session concepts never enter the framework at all; under the session model they enter as an additive extension, which the skeleton shows costs a launcher class, a flag, a cache, and a column.

Two things the core must keep so that phase 3 stays open in every model:

- The callable contract takes materialized objects, not files (D8).
  A checkpoint application and a session both call workflow code in-process with scipp objects; a file-based contract would foreclose both.
- Any client may turn a reference into an object in its own process.
  The sketch grants this to local mode; the core should grant it to any client that can reach the disk tier, which is what lets an application hold its own state without the framework knowing.

And one thing it must not do: nothing in the core may need a request to reach a particular process.
That is already true, and it is what makes the HTTP transport, load balancing, and a restart trivial.

## Recommendation

As a judgment.

- Adopt the all-in stateless variant as the design of phases 1 and 2, which changes nothing in what those phases would have built and removes about a fifth of the architecture text from what their reviewers must hold in mind.
- Decide the phase 3 model when phase 3 is designed, with the checkpoint model as the default to beat, because it meets B1 and C5 without adding a concept to the framework.
- Measure the throwaway feedback loop in the spike, with and without a warm process pool, so that the phase 3 decision rests on numbers; if the cheap-stage rerun is under a few seconds for the techniques that matter, the third model is enough for some of them.
- Keep `warm.py` and the session launcher as an experiment, outside the core's tests and documents, so that nothing is lost if the session model wins.

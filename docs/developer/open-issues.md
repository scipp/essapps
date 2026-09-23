# Open issues

This document lists what the design does not decide, what binding real workflows found that it does not answer, what is deliberately left for later, and what the walking skeleton does not contain.
It is one of the topic documents behind [architecture.md](architecture.md).

## Open questions

Decisions the team needs to make.
The recommendation in brackets is mine.

- **Template sharing.**
  The templates of one instrument share most of their parameters, such as masks and a direct beam, across its specs.
  Facilities that tried template inheritance moved to version-controlled read-only templates with per-dataset substitution.
  [No inheritance. A template may be derived from another by copy, and the record keeps the origin.]
- **The model for remote interactive work.**
  [stages.md](stages.md#sessions-are-one-of-three-models) compares three models: sessions owned by the framework, a checkpoint model in which the application holds the state and only kept results become records, and throwaway processes over stored intermediates, with kept runners as an addition.
  The skeleton implements sessions in local mode.
  For the shared web UI the choice is open, and with sessions it includes where a session runs: on the backend host, in the user's own application, or as an interactive cluster job with queue latency at session start.
  [Decide when remote interactive work is designed, from the measurements below. The checkpoint model is the one to beat, because it meets the interactive user stories without a framework-owned process per user. Batch and automatic reduction do not depend on the answer.]
- **Measurements the decision needs.**
  The feedback loop of a throwaway process in three configurations: cold, with a pool of idle runners that have their imports done, and with a runner that already holds the intermediate in memory.
  The three numbers separate the cost of process start, of the disk read, and of the computation.
  Also the read of a 4D intermediate of several gigabytes, which a finalize makes once per member on every arrival, and which decides when a series needs a cache of earlier finalizes or the [fold](aggregation.md#the-fold).
  [Measure before remote interactive work is designed.]
- **Retention policy for disk copies in shared mode.**
  How long each kind of run's outputs is kept, and the analysis window after which a proposal's records are dropped together.
  [One order: outputs of superseded records first, then outputs the spec marks as cheap to recompute from their inputs, then the kind of run. Authors know which outputs are throwaway, and Snakemake's `temp` and `protected` flags show that they get it right.]
- **Local paths after a drop.**
  A local file's path stays on the record's origin after the bytes are dropped, until the proposal is dropped.
  [Keep it. A path is not data, and provenance needs it.]
- **Two notebooks on one machine.**
  Each notebook is its own backend with its own record store, so referencing a result of one notebook from another needs a local transport.
  [Separate stores now. `essapps serve` on one of them and `remote()` from the other is the alternative, at the cost of every run of the served store executing in a throwaway process.]
- **SciCat push mechanism for new datasets**, if the deployment offers one, and how far ingestion lags the file.
  ISIS's interfaces discover runs from the archive because the catalogue lagged or failed, and their outputs are consequently unknown to it.
  [Measure the lag before [phase 1](roadmap.md#the-three-phases). A filesystem-watching dataset source is the fallback behind the same interface, but a catalogue dataset's identity is its PID, so such a source can only get ahead of the catalogue and wait, never replace it.]
- **Exposed intermediates in ess.reduce.spec.**
  The skeleton adds `intermediates` to the spec of scipp/ess#690.
  Whether that belongs upstream, and in which form, is open.
- **Per-dataset lookup fills in a series.**
  `apply` has each member vary only the template's blanks, and a finalize has the template's values, so a member that a lookup fills beyond the blanks is not accumulated.
  The alternative is to have members vary such fields as well, which the agreement check does not compare, but which lets members differ in a value the finalize may also read.
- **Validation of a request that supplies an intermediate.**
  Such a request that leaves a needed parameter unset fails when it runs; a request that supplies no intermediate is checked against the whole parameter model at submit.
  Whether a GUI can learn this before submitting is open: in local mode the backend imports the registry and could ask the workflow, in shared mode it does not.
- **A rule over a combination that is not an accumulation.**
  `Series` names the intermediates to accumulate and the outputs of a finalize with the template's values.
  A stitch over angles is a separate spec over a list of references to the members' outputs, and a rule has no field that names it.
  A second template on the series, or a second rule whose candidates are the completed member records of the first, would each express it.
- **Where the workflow contract lives once it is stable.**
  `Inputs`, `Workflow`, `StageCall`, `FunctionWorkflow`, and `resolve` are in `ess.apps.binding`, and the sciline adapter is in `ess.apps.adapter`.
  Workflow packages must not depend on the framework in order to publish a workflow.
  [Move both to ess.reduce, beside `ess.reduce.spec`, once scipp/ess#690 has merged.]
- **The name of the backend component.**
  It clashes with esslivedata's "backend services", which are Kafka worker processes.
  [Keep it unless the two projects are documented together.]

## What binding real workflows found

LoKI SANS and Amor reflectometry are bound to the skeleton.
Each found something the design does not answer.
The findings stand until the design changes or the finding is dismissed.

### The workflow contract

See [workflow-contract.md](workflow-contract.md).

- **Pixel masks are a graph rewrite, so the request is not complete.**
  In the LoKI workflow a list of mask filenames rebuilds the sciline graph, so the masks are fixed in the factory that makes the workflow and never reach the record.
  The same rewrite is what stops the additive half of reflectometry, the sum over runs at one angle, from being bound at all.
  The way out is a pipeline whose masks are parameters, with the values that add exposed as intermediates, but neither is bound that way yet.
- **The beam-centre finder takes a pipeline, not a key.**
  It is therefore a plain function, and the expensive part of it is not shared with the reduction that consumes its result.
- **Every scalar parameter costs a `NewType` and a provider** in the adapter's setup, whose only job is to turn plain data into a scipp object.
  Naming a conversion beside the key, the way the form of a data reference is named, would remove them.
- **An output the framework cannot serialize has no place to declare its serializer.**
  An ORSO file is such an output.
  The contract says that an output of a type the framework cannot write must come with a serializer, and the spec has no field for one.

### The type vocabulary

See [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).

- **Collection keys are the submitter's invention.**
  Nothing ties a key of a collection parameter to the record it came from, and a reference into a pending collection output is not checked for its key, so a transposed dictionary is accepted and fails at run time.
- **Chaining between specs is checked by format only.**
  A declared `ArraySpec` is read by nobody, so an output of another spec with a matching format passes validation as a supplied intermediate and fails inside the stage.
  The runner checks an output's dimensions and coordinate names, but not its unit and not the `binned` flag.
- **The vocabulary lacks the parameter types real reductions need.**
  A pixel-index range, an angle range, a Q range, and an edges model whose range is derived from the data are all missing.
  The last is what a workflow whose binning follows the geometry needs.

### Aggregation

See [aggregation.md](aggregation.md).

- **A combination that is not an accumulation has no consistency check over its members.**
  The members of an accumulation agree with the finalize on every parameter both set, which the backend checks.
  A stitch takes references to run records of another spec, which the check does not cover, so a stitch over curves reduced with different detector limits passes silently.

### Rules

See [rules.md](rules.md).

- **Two-level aggregation under a rule is untested.**
  Reflectometry sums same-angle runs and then stitches the angles.
  The first level is a series, and the second is a spec over a list of references, which a rule cannot yet submit (see [open questions](#open-questions)).
- **Roles are named but not defined.**
  A rule that feeds sample runs into one member stage and background runs into another needs a role per dataset and a mapping from role to the parameter a member varies.
  "A series of fixed roles" names this and says nothing about how it works.
- **A feedback cycle has no rule shape.**
  Amor fits scale factors over all members and then re-reduces each member with its factor, which is a cycle from members to the stitch and back to members.
  Three requests express it, but a rule only ever runs members and then one request over them.

## What the design does not solve

- **Memory policy.**
  A stage holds its frontier, and a session holds stages, accumulators, and outputs.
  The session counts its stages and its accumulators and drops the least recently used, but how that bound relates to the memory the session's outputs occupy is open.
- **Migration of the record store to a new schema.**
  The store carries a schema version and a stored parameter set that no longer matches its spec version fails loudly, but nothing says how an existing store is brought to a new schema during a backend upgrade.
- **Parallelism over members.**
  Across records, the launcher runs one throwaway process per member.
  Inside one run, the workflow code may map the contribute stage over the rows with threads.
  Nothing coordinates the two.
- **Static work across processes.**
  A member stage in a throwaway process computes the part shared by all members again, once per member.
  A [kept runner](stages.md#kept-runners) would remove this cost.
- **Live streams.**
  A streaming workflow has members that cannot be recomputed from records, which breaks the invariant the session rests on.
  That is esslivedata's problem and stays out of scope.

## Not in the skeleton

What the skeleton under `packages/essapps` does cover is listed in architecture.md.
It does not contain:

- A Tiled-backed disk tier.
  This is the next implementation worth building, as a second implementation of the same data-store interface, with the test that a scipp data array with units, variances, bin edges, and a mask survives the round trip.
- A SciCat dataset source.
  The folder source and the in-memory fake are the only implementations, which keeps the tests off SciCat.
- A rule over a combination that is not an accumulation, and a series over two member tables.
- A cache in a runner that keeps the accumulated value of an earlier finalize and reads only the members a later finalize adds, under the prefix rule of a session's held accumulator.
- The fold: a long-lived runner that holds the accumulators of one series, accumulates in memory, and writes a finalize run record every few arrivals.
  It is not needed until a series arrives faster than its accumulated values can be read and written.
- The `paused` status and the runner liveness timeout described in [operations.md](operations.md#failure-handling).
- A rule that fires on a completed record.
  The trigger loop iterates datasets only, so a second rule over the completed finalize records of a first rule is untested.
- The last clause of the trigger loop's firing condition, that the dataset's SciCat entry must not carry our provenance snapshot.
  Local mode has no catalogue to ask.
- A durable reservation of a rule's label.
  `Backend.reserve` holds reservations in memory, because no store for templates and rules exists.
- Cancelling a batch by its label.
  `Client.cancel` takes one record.
- A field on `Rule` that says whether its finalize is published.
- Failure reasons declared on the spec, described in [workflow-contract.md](workflow-contract.md#changes-to-the-spec-of-scippess690).
- The test helper that recomputes a completed record and compares the outputs, described in [workflow-contract.md](workflow-contract.md#test-helpers).
- The version counter that [operations.md](operations.md#the-client-interface) promises for observing change.
  `RemoteBackend.wait` polls the records it was given by id, and nothing tells a client what else changed.
- A cluster launcher, remote sessions, a UI, and a store for templates and rules.

## Explicitly deferred

Real SciCat integration, a cluster launcher with its download tokens, view workers and the chunked on-disk layout for dense data, the UI framework, UI state in the record store, metrics, and an agent-facing API.

Remote sessions, whether on the backend host or in a client process on the user's machine, need a session launcher, an idle timeout, and a cap on the number of sessions.

Upload of records from a private local record store to a shared backend, carrying everything a chosen record reaches through its references and nothing downstream of it.

Provisional outputs of a running run, for progress display during chunk-wise processing.

A versioned collection record, appended to by the client and referenced by version, if lists of runs to aggregate grow well beyond a few thousand entries and resending them whole becomes a cost.

Resource hints on the spec for the cluster launcher, such as memory as a function of input size and of the attempt number, which a retry record knows from its link to the record it retries.

## Follow-ups outside this repository

These are changes to scipp/sciline#245, the proposal this design's stages and aggregations build on.

- **The design document of the proposal describes essapps as one spec with a declared contribution and three entry points**, and says that the spec declares which parameters the finalize stage reads.
  It should instead say that essapps records each `Stage.compute` as a run record holding the pipeline's parameters, that a spec exposes the intermediates a stage may take or return, and that the binding reuses the accumulators an `Aggregation` takes.
- **Finalize inputs need no change to sciline.**
  `Aggregation` builds its finalize stage with the accumulation keys as its only inputs, so a finalize parameter cannot be given per call.
  The adapter does not use that stage: a finalize run is a plain `Stage` whose inputs are the supplied intermediates and any finalize parameter the request varies.
  An argument for naming finalize inputs is therefore not needed.
- **A parameter read by both stages must reach the finalize stage with the value the members were made with.**
  Within one process the snapshot guarantees this.
  Across processes the backend checks that the members and the finalize agree on every parameter both set and neither varies.
  The proposal could name this as the consumer's responsibility.

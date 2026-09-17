# Architecture sketch for ESS data-reduction applications

Living document.
Records the choices made so far, the alternatives that were on the table, why each choice was made, and what it costs.
Companion to [scoping.md](scoping.md), which states goals and scope.
A walking skeleton of it exists under `packages/essapps`; see "Next step".
Technology choices at the end are proposals.
A reading edition with diagrams is [architecture.html](architecture.html); its wording follows this file.
Three review notes read this document against the delivery order and the execution model: [staging.md](staging.md), on what each phase needs, [stateless.md](stateless.md), on what a design without sessions would remove and cost, and [stages.md](stages.md), on how the sciline proposal to replace map/reduce with stages and folds fits the execution side.
Three more, [snakemake.md](snakemake.md), [aiida.md](aiida.md), and [mantid.md](mantid.md), read it against the histories of Snakemake, AiiDA, and Mantid's ISIS batch interfaces: what each got right, what it learned the hard way, and what was taken from it here.

## How to read this

The design rests on one modelling idea and four choices.
The idea comes first, under "Records and references".
The four choices follow, each with the options that were rejected, so that the team can disagree with a choice rather than only with its consequences.
Everything after that is consequence: the rules that fall out, how requests are made from data, the changes the workflow spec needs, the components, and how failures are handled.
Decisions carry stable identifiers D1 to D15 so discussion can point at them; they are indexed at the end, next to the glossary.
Terms are introduced where they are first needed.
Two are used before they are defined: the **vocabulary** is the set of types the workflow spec allows for parameters and outputs, and the **registry** is the part of the data store that knows which stored copies of an output exist.

## The picture in one paragraph

A user, a script, or an automatic trigger submits a *run request*: which workflow to run, with which parameters, on which input data.
The *backend* checks the request, writes it down as a *run record*, and asks a *launcher* to start a *runner* somewhere: in the same process, in a subprocess, or on the cluster.
The runner fetches the inputs, calls the scientific workflow code, and stores the results.
Any output of a record, whether a single number or a large array, can be an input of the next request, named as *output X of record Y*.
Raw files from the catalogue and files on a user's disk are records too, so every input is named the same way.
Interactive work happens in a *session*: a process that keeps the workflow and its data in memory between runs, while the records look the same as for any other run.
Everything the backend knows is in the records, so any result can be traced back to raw data and parameters, and any result can be recomputed if its data was thrown away.
Batch reduction is many requests made from one *template*, applied to a set of datasets at once.
Automatic reduction is a *rule*, a template with a lookup and a selector, which the *trigger loop* applies to each new dataset as it arrives and which combines a series of runs again whenever a run joins it; a rule is to a batch what a template is to a request.
Publishing a result to the data catalogue is a separate, deliberate step.

## Records and references

The framework has one way to name a piece of data: *output X of record Y*.
This section is what follows from taking that seriously (D1).

A **run request** is everything needed to execute a workflow once: which spec, the parameter values, and the instrument, proposal, and submitter.
A **spec** is a workflow's declared interface: name, version, parameters, outputs.
It is independent of how the workflow is implemented or where it runs, and is defined in scipp/ess#690.
A request is complete: sufficient to reproduce the outputs from scratch.
It is plain, JSON-serializable data, even when it never leaves a process.

A **run record** is the request plus what happened to it: run ID, status, timestamps, output values, the resolved parameter values including defaults, the package versions and the environment of the runner, how the spec was bound to code, whether the result was computed from state held from an earlier run, and the runner's console output, kept beside it.
A request may carry a **label** and a **member key**, under which records supersede each other (D14).
The record also carries a **submission**, which says how the request was made: the template version, the rule version and lookup entry when a rule filled it, and the values the submitter typed beyond template and lookup, so that a later reprocess can carry them forward.
The submission is explanation, not provenance, because the resolved request alone reproduces the run.
Optionally it carries one link to the record it derives from, with the reason: retry, recompute, or copy.
The run ID is a UUID, so that records can move between stores without renumbering.
A failed run carries a structured failure reason, so that a user sees why without reading logs.
Resolved values and package versions are what make "recompute from the record" true; without them a changed default or a package upgrade silently changes what a record means.
The environment is recorded as an opaque name and revision, at ESS a conda environment, so that a recompute can be checked against it; the framework does no more with it than record and compare.
A record is immutable once the run completes, except for status, and is never deleted on its own.
Beside it live **annotations**: labels and notes a user attaches after inspection, such as "use this vanadium" or "superseded"; they are outside provenance, may change at any time, and nothing in the framework reads them.
Small output values are stored in the record; large ones are held by the data store, and the record says only that they exist.
Which of the two applies is decided by type and is invisible to clients.

A **reference** is a value that a parameter field of matching type may hold instead of a literal, and it has two forms: "output X of record Y", optionally one element of a collection output, "output X of record Y, key k"; or a dataset, below.
It is the only way a request names data.
A reference names data by identity, never by where its bytes are; the data store's internal keys never appear in a request.
A reference may name a **pending output**, one whose record has not completed yet; the backend holds the request until it does.
The record keeps references in reference form, so **provenance**, the chain from any result back to raw data, parameters, and software, is the graph you get by following them.

**Datasets are the leaves.**
A **dataset** is data the framework did not compute: a SciCat dataset, or a file on a user's disk.
A reference names it by its **dataset identity**: the PID for a catalogue dataset; for a local file, the instrument and run number it carries in its name or header, which is what a PID is minted from, or its path when it carries neither, the one case where a path is an identity.
Identity is not location: where the bytes are is asked of SciCat at dispatch and cached at most, because SciCat moves files to archive and back and edits metadata while our records are immutable, or is the user's path for a local file, or a copy in the data store.
Nothing is stored per dataset: its proposal is checked against the request's at submission, when the PID's SciCat entry is read anyway, and the data store registers a copy of its bytes only when it makes one.
In the local application a folder is a dataset source (D7), read when asked; the checksum of a local file is recorded on the record of the run that read it, so that a recompute can tell whether it read the same bytes, and the path the submitter typed stays on the submission.
A dataset is not a record: it has no request, no status, and nothing to recompute.
A parameter of data-reference kind accepts either form, a dataset reference is never pending, and the trigger loop's two kinds of candidate are a new dataset and a completed record (D14).
Making every dataset a record of a built-in `file` spec, so that a reference has one form, was the sketch's first answer and was dropped in the tenth pass: it bought a UUID over an identity SciCat already keeps, a spec with no workflow, and a rule to stop the store from becoming a catalogue, and gave nothing the two forms do not, since "which records used this dataset" is the same index over references either way, and a raw file is not viewable in either, a quick look being a preview run (D10).

**Stand-ins resolve at submission.**
Users submit a local path, a PID, or a run number, which is unique within an instrument and proposal.
The backend turns each into a reference before persisting anything, because provenance must not depend on a search that could give a different answer later: a run number is looked up in SciCat; a PID whose entry carries our provenance snapshot resolves to the run record named there while the store still has it, and any other PID to a dataset reference, once its proposal is checked against the request's.
A path under the facility filesystem resolves to the PID of the dataset that owns it; any other path becomes a local dataset, identified as above.
Nothing is downloaded or copied at submission, and SciCat is not needed again once a reference exists.

**Whether an output is usable is two questions**: the record's status, and whether the data store holds a copy.
A missing copy is reported as such, never silently recomputed.
Getting it back is an explicit operation, **recompute**, which submits the record's request again and yields a new record linked to the old one; published outputs and catalogue files are instead downloaded again.
Recompute is exact only in the environment the record names, and refuses to run elsewhere unless the client overrides.

| Data | Where the truth lives | On a missing copy | Copies evictable |
|---|---|---|---|
| An output of a run record | The record: parameters, references, versions | Recompute, explicitly | Yes |
| A catalogue dataset | The PID; SciCat says where the bytes are | Download again | Yes |
| A local file | Its identity and the user's path | Nothing to recover from, unless a store copy was made | A store copy, only by an explicit drop |
| A published run record | The PID, whose entry carries the provenance snapshot | Download rather than recompute | Yes |

Two kinds of stored data make requests without a person filling a form, and are defined under "Rules: how requests are made from data" (D14): a **template** is a partial run request, and a **rule** is a template with a lookup and a selector, applied to datasets.
A **batch** is the records made under one label, whether a person or a rule made them; none of the three is a copy of the records.

**Why this is the foundation.**
Two forms of reference, a record's output and a dataset, serve inputs, views, publication, and provenance; only the first can be pending, so the scheduler has one kind of dependency.
There is no "which record produced this" query, because the reference names the record.
A handle with its own origin and lifecycle would be three special cases of this: the origin is the producing record, and the lifecycle is record status plus copy availability.
Records are never deleted one at a time, because every field that can hold a record ID would otherwise be an edge in a garbage collector, and a row per run costs nothing; a proposal's records are dropped together, under "Lifetimes" in choice 1.

**Cost.**
The record store could become a second catalogue by accretion: a row per dataset discovered, metadata copied for display, paths that go stale.
The rules that stop it are under "The record store is not a catalogue".

## The four choices

Each choice states the question, the options that were on the table, the option taken, why, and what it costs or forecloses.
Rejected options are described from this project's point of view and are short; they may be unfair to the tools in general.

### Choice 1: Where state lives (D2, D3, D4)

**The question.**
Batch and automatic reduction need runs that can be made without a human present, and provenance needs runs that fully describe their result.
Interactive work needs the opposite: sub-second reruns over multi-gigabyte intermediates, as in SANS, kept in memory between reruns.
Where does that state live, and what does a record know about it?

**Options.**

- *Stateful jobs.*
  A running workflow object is the unit; clients address it by identity, and a record is whatever it reports.
  This is esslivedata's model, and interactive work is natural in it.
  Provenance and batch then have to be reconstructed from a job's history, and everything else must compensate for identities that vanish with the process (scipp/esslivedata#1042, ADR 0008).
- *Stateless everywhere.*
  Every run is a fresh process, and every input and output passes through disk.
  Simple and remote-friendly, and an earlier version of this document said exactly this.
  It makes interactive loops disk-bound and rebuilds the workflow on every rerun, so the feedback loop never gets below seconds.
- *Shared memory across processes.*
  A distributed object store, such as Dask's or Ray's, or a layer of our own, so that any process can reach any array.
  scipp objects are not chunk-aware, so such a layer would work badly and cost a lot to build and operate.
- *Stateless records, stateful sessions.*
  The record is stateless and complete.
  Execution may happen in a **session**: a process belonging to one client, which keeps the workflow object and the outputs of earlier runs in memory.
  Memory never crosses a process; anything that must reach another process goes through disk.

**Choice.**
The last option, with two invariants that keep it safe (D2).
Session identity never appears in a record.
Everything a session holds can be recomputed from records, so a session is a cache, and losing one costs only time.
The invariant is not about sessions: every value this system holds in memory, including a partial sum over a growing series, is recomputable from records, because every input to it is a dataset or an output that has one.
Held state is therefore always an optimisation with a recompute fallback, never the only copy of a fact, which is what lets a growing series be combined on disk or in memory interchangeably (D15).
It holds because reduction here consumes datasets; it would not hold for reduction of a live stream, where the inputs are pulses with no records, which is why that is esslivedata's problem and not in this project's scope.
This is esslivedata's lesson applied the other way round: ephemeral identities are dangerous when other things depend on them, so nothing depends on this one.

A session is defined by its owner, not by where it runs.
Initially it exists only in **local mode**, where client, backend, launcher, session, and data store are one Python process: a notebook, or a local application.
Later it may live on the backend host, or in a client process on the user's machine with the backend elsewhere; the second form is how a local application inspects data interactively while the expensive stages run on a cluster.
In **shared mode** the backend runs as a service used by many people.
Batch and automatic reduction run there without sessions, and the shared web UI submits and inspects.

A session holds one **warm workflow** per spec version: the workflow object kept alive between runs, so a rerun recomputes only what a changed parameter affects.
Tuning a stage and its consumer together, such as vanadium processing and a sample reduction, uses two, chained through the session's memory.
From the framework's side there is one kind of rerun: a new, complete request.
Changing a threshold and adding one more run to a list of runs to sum look the same, a full parameter set that differs from the previous one in one field, and the record of each rerun stands on its own.
A changed threshold reruns the warm workflow (choice 3); one more run is a combine request over the session's previous combine (D15); neither is a kind of request the framework distinguishes.
The list is resent whole on every rerun; it holds references, not data, so at the few thousand entries expected it stays under a megabyte.

**The data store follows from the choice (D3).**
It is one component owned by the backend: a registry and a disk tier.
Every process that holds data, a session or the shared service, also keeps a private in-memory cache of outputs.
The registry knows disk copies only, keyed by reference in either form: outputs written to the disk tier, and copies of datasets the store made, a catalogue file downloaded or a local file uploaded.
A memory cache is invisible to every other process, is never registered, and nothing is ever pulled out of a session by anyone but the session's own client.
A runner asks the store for an input and hands it an output; the store serves from the cache of that process when it can and otherwise reads or writes disk.
The runner never knows about caches, and the launcher decides only where a run executes.
A run executes where every input has a reachable location; when an input has none there, the client first makes one, copying a local file into the data store or fetching a catalogue file onto a machine without the mount, which are one action in opposite directions.

There are two execution shapes, and a group of requests submitted together runs in one of them, never mixed:

- **In the session holding its inputs.** Inputs come from that cache and outputs stay there; nothing is written.
  A chain of dependent requests runs in one session.
  An output leaves the session only when the client asks for it to be written out, which publication and chaining to a request placed elsewhere do on the client's behalf.
- **In a throwaway process.** A subprocess or a cluster job, sharing one code path.
  Outputs go to disk before the run reports completion.
  This is every run in shared mode.
  The shared service loads an output into its cache on first access: written once, loaded once, every further view served from memory.

**Lifetimes.**
There are three, and none is set by the framework.
Memory lives as long as a session: a process with an operating-system limit, owned by its client; its cache has no budget of its own, because the store's copy of an output and the warm workflow's intermediate are the same object and evicting one frees nothing.
The shared service's cache has a byte budget and evicts least recently used first, with outputs of superseded records (D14) before anything else.
Records live as long as their proposal plus an analysis window set by the facility: long enough to find last week's result and to chain to yesterday's vanadium, and no longer, because what is worth keeping longer was published, and a published entry carries its own provenance (D11).
A proposal's records and disk copies are dropped together, exported as one JSON bundle first; nothing is deleted one record at a time.
This is safe because references never cross proposals except into instrument-shared artefacts, whose commissioning proposals are long-lived, so no reference can point into a dropped proposal.
Within a proposal, disk copies have a retention policy per kind of run, an open question; when it expires the bytes are dropped and the record stays.
A dropped copy is a missing copy, under the rule in "Records and references".
Store copies of local files are exempt from that policy: the framework cannot bring them back, so such a copy is dropped only by an explicit operation on the copy or with its proposal, after which every record that reaches it through references is no longer recomputable.
They count against a per-proposal quota.
A login, a batch, or an application start is not a lifetime: what a user comes back for outlives all three, and automatic reduction has none of them.

**A rule that follows: reuse across requests is a workflow boundary (D4).**
A value that other requests reference must be an output of a run of its own, with its own record.
Workflow authors therefore cut a workflow into separate specs exactly where such a value arises, and nowhere else.
Two reasons produce a cut.
Reuse: one artefact feeds many runs, such as processed vanadium, a beam centre, or a direct beam, which sample reductions, batch, and automatic reduction all take as an input.
Iteration without a session: an expensive stage whose result is tuned from a throwaway runner or from the shared web UI, such as loading and preprocessing a large run before adjusting its post-processing.
Inside a session the second reason disappears: one unsplit spec is enough, because the warm workflow recomputes only what changed, and the loaded data stays a value inside the workflow, which the framework never sees: no record, so no reference, and it dies with the session.
The first reason holds in a session too, because the artefact needs a record of its own before batch can reuse it.
Splitting is the only strategy that works on a fire-and-forget remote runner, and it lets the UI tell which stage is cheap, because it is a separate workflow.
An output that another spec takes as input is called a stage output in this document; the word names a role, not a kind.

**Why.**
Batch, automatic reduction, and provenance are properties of the record, not of the process that executes it, so pinning "stateless" at the record level costs them nothing.
A stage output can be huge, and writing one to disk is wasted work when its only consumer is in the same process; recompute is often cheaper than storage.
Keeping memory caches private is what keeps the design free of a cache-coherence protocol: a registry that tracked copies in processes it does not own would need every eviction, close, and crash reported, and would block backend requests on user processes that may be busy or gone.
The argument is about processes the backend does not own, and does not rule out an index over a pool of warm runners the backend does own, addressed by the reference they hold; that stays open as an additive option, and is discussed in [stateless.md](stateless.md).

**Cost.**
Two lifetimes for the workflow object in the runner: once per run, or once per session.
Remote sessions need a session launcher, an idle timeout, and a cap on sessions; that is why they are deferred, and why the shared web UI initially has no interactive loop.
Shared mode pays one disk write and one read per output and a process start per run.
Placement is a launcher decision and must be explicit in its interface; whether a chained consumer exists is only known for requests submitted together as a group (choice 2).
The author chooses where to cut a workflow, and the cut is not always clean: processed vanadium in diffraction is rebinned onto the sample's edges without interpolation, so a stored dense vanadium is usable only for compatible binning, and the alternative is to keep it as events.
Both stages of a split workflow typically share parameters; see the open question on templates.
Every rerun in a session is a complete record, so a series of N slider moves is N records; slots (choice 4) keep that from being what a person sees.

### Choice 2: Own the records and the scheduling, or adopt an engine (D5, D6, D7)

**The question.**
Something must keep the records, hold a request until the outputs it references exist, propagate failure and cancellation along a chain, and notice new datasets.
Do we build that, or take it from a workflow engine or a message broker?

**Options.**

- *A workflow engine* such as AiiDA, Snakemake, Nextflow, or Prefect.
  Each bakes the dependency graph into code rather than records and keeps provenance in its own model.
  None gives a stateless request that a notebook, a trigger loop, and a batch UI can all emit.
- *A message broker, with Kafka for dataset events.*
  Facilities that built their own scheduler for the remote case eventually added a broker (ISIS, SNS, Diamond, ESRF), so a cluster deployment may end here.
  Kafka for dataset discovery would couple the backend to the streaming infrastructure and reduce data that is not yet catalogued.
  Reliable command delivery over Kafka was a long struggle in esslivedata (scipp/esslivedata#856); it concluded that a request row in a database is the durable desired state that was missing.
- *Our own record store and a small scheduler.*

**Choice.**
Our own record store, with the backend as its single writer (D5).
The **record store** is SQLite on local disk, Postgres if a backend ever needs it.
Records contain nothing sciline-specific.
The store carries a schema version, and a stored parameter set that no longer matches its spec version must fail loudly, never be silently defaulted; schema versioning was left unresolved in esslivedata and needed hand-run migrations (scipp/esslivedata#915).

**The backend is the single writer.**
Exactly one backend process per record store, enforced by a lock that a live backend renews and a dead one loses.
In local mode every notebook is its own backend with its own store, at a location the client chooses with a per-user default; a second notebook must use another location, and referencing records across notebooks waits for a local transport (open questions).
The runner reaches storage for data and the backend's API for everything else, and never touches the record store; there are no database credentials on compute nodes.
The backend resolves references to locations at dispatch: a dataset's path, asked of SciCat for a catalogue dataset or the user's own for a local file, is handed to the runner as is, anything else the runner fetches from the data store.

**One scheduling primitive: pending outputs as inputs (D6).**
A group of requests is submitted atomically and gets its IDs back; inside the group, requests refer to each other's outputs before they exist.
The backend holds a request until every record it references has completed, fails it if any of them fails, and cancels it if any is cancelled.
A request whose reference names a collection element that the completed producer does not have fails with a missing-key status; the producer is unaffected.
Groups must be acyclic; a waiting request has no timeout, because every record it waits on reaches a terminal state through its own failure handling.
Map and combine are one use of this: a batch of members plus one request whose inputs reference their outputs.
They are optional: a spec may instead take a collection of runs and iterate inside, as reflectometry does when it scales overlapping angle curves against each other and writes the scaled curves back per angle.
A combine's output may itself be a collection keyed by member.
An additive combine is declared as such and chained (D15); any other merge strategy belongs to the combine workflow.
Batch members are independent: no ordering between them, and rerunning a member is a new record under the same label and member key; anything else is chaining.
A group is validated whole before any record is created, and a batch is cancelled whole by its label, queued and running members alike.

**The dataset source is abstracted (D7).**
Its interface: for a proposal, yield new datasets as PID plus the metadata fields it declares for the instrument; those fields, an angle, a sample name, a run's role, are the only ones a lookup or a rule may match on, and they are declared here because the source is what knows what the acquisition writes into the catalogue.
It persists nothing: a dataset enters the store only as a reference in the requests a rule submits, like any other stand-in; which datasets a rule has already decided on is a query over the records, not memory in the source or the loop, and which series a member belongs to is asked of the source when a combine is submitted.
A SciCat implementation, polling or push as the deployment allows, a folder for the local application, and an in-memory fake.
Arrival may be out of order and repeated; the interface does not promise a monotonic cursor.

**Why.**
No engine gives the stateless request the whole design rests on.
Single writer avoids the multi-client ownership problems that produced most of esslivedata's hard bugs (scipp/esslivedata#1285, #714, ADR 0007), and one runner gives one code path for execution.
Pending outputs as inputs is the smallest addition that covers a vanadium stage feeding a sample reduction submitted together, temperature scans, angle series, and automatic reduction over a series of runs.
Two dataset-source implementations from day one keep tests off SciCat.

**Cost.**
We own dependency handling, failure propagation, and cancellation: a small scheduler.
"No broker" is a local-mode decision to be re-examined when the cluster launcher is built.

### Choice 3: The contract with workflow code (D8)

**The question.**
How does the framework call scientific code, how do inputs get in and outputs out, and what does a rerun in a session reuse?

**Options.**

- *The framework knows sciline.*
  It builds the pipeline, computes the requested nodes, and caches intermediates itself.
  That ties the framework to one engine, and caching every intermediate is not affordable with event data while the graph does not know compute cost.
- *A file-based contract.*
  The workflow reads input files and writes output files.
  Simple and remote-friendly, and what an earlier version of this document had; it forces every chain through disk, which contradicts choice 1.
- *Two protocols*, one for one-shot execution and one for incremental reruns.
  Two execution paths to test and keep consistent.
- *One callable from parameters to outputs*, kept between runs in a session.

**Choice.**
One callable (D8).
Spec identity is bound to an implementation via Python entry points; in local mode a notebook may also bind a spec in-process, provided it does not claim the name and version of a spec an installed package provides.
The record says which binding was used, so publication can tell a reproducible record from a development one, and a spec may carry a code revision, a git commit or package version, so that a record made from a development branch is honest about what ran.
The entry point returns a callable that takes the validated parameter model and returns the output model.
A throwaway runner constructs it and calls it once.
A session runner constructs it once per spec version and calls it for every run, so the callable may keep state between calls; that is the warm workflow.
A session holds the code it imported, so a change to workflow code takes effect in a new session, never in a running one.
The framework never imports sciline.

Data-reference fields are materialized by kind:

| Reference kind | Arrives in the callable as | Why |
|---|---|---|
| Raw NeXus file | local path | Loading NeXus is workflow-specific: which detector banks, which monitors. |
| Opaque file (CIF, ORSO, ...) | local path | The framework cannot know the format. |
| scipp array | scipp object | The one format the framework writes itself, so it can read it; served from memory when the process holds a copy, from scipp HDF5 otherwise. |

Outputs are returned as objects matching the output model; an output field may be absent when the workflow's mode does not produce it.
The callable never writes files.
The runner validates outputs against the model, stores vocabulary-typed small values through the backend, and hands the rest to the data store, which serializes scipp objects to scipp HDF5 when they must reach disk and requires other types, such as CIF, ORSO, or NeXus products, to come with their own serializer, declared with the output.
Chunk-wise processing of one large file, as the NMX workflow does, happens inside the callable and is invisible to the framework; that the NMX product is then held in memory before it is written is a cost accepted here.

**What a warm workflow reuses.**
A sciline workflow meets the contract through a thin wrapper that keeps the pipeline and caches the values of the nodes the spec declares expensive.
A rerun sets the changed parameters and recomputes only what lies downstream of them, which is how every notebook already works: the parameters people move interactively, Q bins, d-spacing bins, cut axes, a beam centre, enter after the expensive load and coordinate conversion.
The spec therefore declares which parameters are cheap to change, and the wrapper builds a **stage** from the workflow's targets and those parameters as its inputs: sciline's `Stage` (scipp/sciline#245), the part of a graph from named inputs to named outputs, which computes once everything the inputs cannot affect and holds it at its frontier.
That frontier is the cache, and a rerun is a call of the stage with the cheap parameters.
The declaration is also what lets a UI offer a slider rather than a run button, and a stage refuses an input its outputs do not need, so a cheap parameter that never reaches the targets is a bind error rather than a dead slider.
The wrapper imports sciline; the framework does not.
A list of runs that only grows is not a case for the warm workflow but a combine (D15); the wrapper caches nodes and never accumulates.
The framework provides a test helper that drives a callable through a sequence of parameter sets warm and cold and asserts equal outputs; that is the one check on the wrapper's reuse rules, and every workflow with a warm form runs it.
A second helper recomputes a completed record and compares the outputs, so that a workflow package can keep records from production as regression tests.

**Validation** has three layers.
Shape: JSON Schema, in the backend, always.
Parameters: the pydantic model, which catches cross-field rules the schema cannot express.
The backend runs it too, by importing the spec module alone, which esslivedata keeps separate from the workflow factory precisely so that specs can be validated without importing workflow code; scipp/ess#690 must keep that separation, and the backend never imports a factory.
Runnability: every reference resolves to a record the user may read and, for a collection element, to a key the producer declares; files exist where the launcher would look; the launcher's environment has the spec.
Anything past that, such as a file that opens but lacks a monitor, is a run that fails fast, not validation.
The runner remains authoritative for parameters (ADR 0001 in scipp/ess#690); a disagreement with the backend's check is a deployment bug, and the backend refuses a spec it cannot import unless the request opts out.

**Why.**
Arrays arriving as objects is what lets a chain in a session stay in memory while workflow code looks the same in every mode.
One callable rather than a separate incremental protocol keeps one execution path: the only difference between runners is whether the callable is kept, and a combine request (D15) calls two stages of the same binding rather than a second protocol.
Reuse inside the wrapper is correct by construction from the sciline graph, given the declared cheap parameters; the declaration cannot be avoided, because caching every intermediate is not affordable and the graph does not know compute cost.
Serialization of outputs must be pluggable because the outputs that get published are often not scipp objects.

**Cost.**
Two validation points, with the runner's being the authoritative one.
The runner loads scipp arrays whole.
Reuse is exact only if the wrapper's rules are right; the test helper is the check, and the record's reused flag, which is what the wrapper reports about the state it held, is what lets publication insist on a cold result (D11).
Accumulation over a growing list is D15's problem, where normalisation sits after the accumulation key.
DREAM and imaging masks are Python callables today; each such workflow needs a range vocabulary and a conversion before its requests are plain data.

### Choice 4: How clients reach the system (D9, D10)

**The question.**
A notebook, a local application, a shared web UI, and eventually agents all need to submit, inspect, and plot.
What is the API, and what may a UI touch?

**Options.**

- *The UI reaches into backend internals.*
  Fastest to start, and the source of esslivedata's cross-session races (scipp/esslivedata#1046, #1098, ADR 0007).
- *A Qt desktop application.*
  A third UI with its own testing story, and no path to shared use.
- *An HTTP API and a TypeScript frontend from day one.*
  API-first, but it front-loads a transport and a second language before there is a backend to serve, and needs a plotting stack other than plopp.
- *The backend's Python interface is the API.*

**Choice.**
The Python client interface is the API (D9).
Every UI, notebook, or service reaches the backend only through it; HTTP is a later transport for the same interface, not a second API.
Requests, records, templates, and references are plain data even in local mode.
The interface has a validate operation, separate from submit, that returns structured errors per field and says which of the three layers ran; submit refuses on any error, and a UI calls validate on every change so that feedback arrives before the user leaves the form.
Clients observe change by pulling with a version counter, never by callbacks carrying data.
No standalone local application initially: a notebook on the library is the local application.
When one is wanted, it is the same web UI hosted in the local process against the in-process interface; the shared deployment is the same UI over the HTTP transport.
UIs for batch and interactive work will grow more complicated than anticipated, so the workflow contract and the view interface are the two contracts that must not foreclose UI options; everything else in a UI is replaceable.
The UI framework is chosen after the backend skeleton exists; a Python-driven web framework satisfies API-first if it only uses the client interface, and Tiled, if adopted for the data path, nudges toward a JavaScript frontend with plopp as the notebook path.
Qt is out.

**Views are not runs (D10).**
A view is a request for a small piece of an output's data for display: label-based slicing, reduction over dimensions such as sum or mean, and downsampling to a display resolution.
Views are part of the client interface, so there is one endpoint in every mode, and a view result is small by construction: plain arrays with coordinates, units, and masks, not a scipp object, so that any plotting stack can consume it, plopp in a notebook being one.
A view is a pure function of a reference and a view specification, served by the process that holds a copy: a session serves views on its own outputs, the shared service serves views on disk copies it has loaded, and dedicated view workers can take that role later if one instrument's volumes outgrow one process.
Views are not recorded and are never inputs to a run.
When a user wants to compute further from a slice they found interactively, the slice specification becomes a parameter of the next workflow.
Overlaying several runs in one plot is several views; anything beyond slicing, reduction, and downsampling, including the difference of two runs, is a workflow.
Event data is never viewed, and neither is a raw file: a quick look at a run just measured goes through a per-instrument preview spec that loads the file and produces the dense outputs a quick look needs.
Four of six workflow families end in binned events, so the array spec marks an output as binned, a workflow that ends in events also declares the histogrammed output people look at, and the UI offers to plot only dense outputs.
In local mode a client may also turn a reference into a scipp object directly, so plopp and the full scipp API work in a notebook.

**Interactive reruns live in slots (also D10).**
Interactive tools bind to parameters of shared vocabulary types: range, rectangle, polygon.
Each change submits a new complete request into the plot's slot.
A **slot** is a label an interactive tool owns.
A **label** is a field on a request, and the latest record under a label, and its member key when it has one, supersedes the earlier ones; a slider moving a threshold, a selection being dragged, or a series of runs growing by one are one label with no member key.
The client interface supplies the label whenever a request reruns a warm workflow, so a notebook user gets a slot without asking for one; comparing two variants side by side is two labels, the second assigned when the user forks, and discarding a variant drops its label from the UI and nothing else.
The backend offers one query, the latest record per label and member key, and treats outputs of superseded records as the first to evict; the same field and the same query serve a batch and a rule's records (D14).
Cancelling a slot's queued predecessors is one client call.
Tools list, replay, and inspect by slot; inspection shows the latest record with its diff against the previous one, which is "one more contribution" for a series (D15) and "one value changed" for a slider.
Slot runs execute in the submitter's session, which until remote sessions exist means local mode.

**Why.**
One API keeps the UI out of backend internals.
Exploring a 4D volume by dragging through 2D slices must not create records, and the frontend must never receive the volume; the slice-becomes-parameter rule keeps provenance exact without making views part of it.
The slot is the one interactive concept in the framework, and it adds nothing to the record: the label is the stable identity that a plot, a record browser, and a replay tool all need across superseded runs, the same split between a stable data key and a per-result key that esslivedata needed (scipp/esslivedata#1062).
Without it a session's reruns are hundreds of complete, near-identical records, which is the right model for provenance and the wrong thing to show a person.

**Cost.**
A local application in one process shares the interpreter between UI and runs, so a long run blocks the UI unless the session moves to a subprocess, which needs the remote-session machinery.
A view vocabulary in the client interface, and a chunking decision at write time for dense data so that views on data larger than a cache can read partially from disk.
Through the client interface a user explores declared outputs only; a notebook can compute any node of a sciline workflow.
Interactive feedback is restricted to sessions, and initially to local mode.
Every workflow that ends in events carries a dense twin, one histogram call in the callable.

## Combining: how contributions become a result (D15)

**The question.**
Reduction is combination: counts from many pulses, angles, banks, and runs are added up into a curve or a volume, and that sum is then normalised, stitched, or fitted.
Techniques differ in what is added, a one-dimensional curve for SANS and diffraction or a four-dimensional volume for spectroscopy; in how it is added, dense histograms by summation and binned events by concatenation; in what is added over, runs in a series, angle groups inside one run, or chunks of one file; and in when the non-additive step comes.
Earlier versions of this document answered this in three places that did not meet: D6 made every combine an opaque workflow, D8 made a growing list of runs a special case of the warm workflow, and D14 had a rule recombine the members' outputs.
The last is wrong for the first technique it would meet: a SANS member's displayed output is I(Q), and I(Q) curves do not add.
What the workflows do, read from the source:

- ess.sans merges runs at two nodes, the numerator and the denominator of I(Q), events by concatenation and the dense denominator by summation, and normalises once after the merge.
  One function dispatches on the value: concatenate if binned, sum if dense.
  The one-dimensional and two-dimensional outputs share the path.
- ess.reflectometry concatenates events for runs at the same angle, which is additive, and combines angles by a global fit of scale factors over all curves, written back per angle and followed by a variance-weighted mean.
  That is not additive; it needs the whole set, and it is affordable because the curves are one-dimensional.
- ess.powder has no multi-run sum yet, and normalises by proton charge and by vanadium per run, upstream of where a sum would go.
- ess.bifrost combines angles inside one run: events are grouped by rotation and concatenated into the volume.
  A series across runs is unwritten.
- `ess.reduce.streaming.StreamProcessor` is the additive structure made explicit: a graph up to declared accumulation keys is run per chunk, values are added at those keys, and the graph from those keys to the targets is run once at the end.
  Its accumulators histogram events before adding, so it covers histogram mode only.

Every additive case has one shape: a per-member stage produces an intermediate at one or more **accumulation keys**, the intermediates are added, and a final stage turns the sum into the outputs.
Normalisation sits in the final stage, which is why the intermediate usually has two parts, a numerator and a denominator, as LoKI's does in esslivedata.
Dimensionality and event mode do not change the shape: a four-dimensional volume adds like a curve, and concatenation is addition for binned data.
They change the size of the intermediate and the operation, and the framework needs to know neither.

**Options.**

- *Opaque.*
  Every combine is a workflow; the framework knows nothing beyond map-combine (D6).
  Exact for reflectometry, wrong or silently expensive for every additive case, because the framework cannot chain, fold, or parallelise what it cannot see.
- *Framework-owned.*
  The framework sums scipp arrays itself.
  It then imports scipp semantics it has otherwise avoided, decides between summation and concatenation, and still cannot place normalisation.
- *Declared structure.*
  The workflow declares its accumulation keys and supplies the three stages; the framework knows the algebra, that the middle stage is associative and commutative, and nothing about the values.

**Choice.**
Declared structure.
A workflow may declare a **contribution**: an output at its accumulation keys, opaque to the framework, and typically a data group holding a numerator, a denominator, and whatever else must be summed, such as monitor spectra or proton charge.
A workflow with a contribution exposes three entry points instead of one: **contribute**, from parameters to a contribution; **combine**, from two contributions to one, associative and commutative; and **finalize**, from a contribution and the parameters to the outputs.
The single callable of D8 is the composition contribute, then finalize, and is what a run without a series executes.
A sciline workflow gets the three from the wrapper given the accumulation keys, which is the declaration `StreamProcessor` already takes: the graph up to the keys is contribute, the graph from the keys to the targets is finalize, and combine is the addition ess.reduce already dispatches on the value.
The wrapper builds the three from sciline's `Aggregation`, constructed from the accumulation keys, an accumulator per key, and the **member table**, one row per member holding the parameters that vary across the members of the request at hand.
The accumulator is sciline's `Buffered` over the package's combine function, or a running total where a sum over large dense arrays should hold one array rather than one per member.
Whether the intermediate is events or a histogram is the author's choice at the accumulation key: events keep rebinning a cheap parameter of finalize and cost memory and disk; a histogram fixes the bins at contribute and is small.
The framework does not see the difference.

A spec that declares a contribution also declares which of its parameters finalize reads; the rest, every data reference among them, are contribute's.
The two sets usually coincide with the expensive and cheap parameters of D8, since the parameters people move interactively are the ones that enter after the sum, but they are separate declarations because they answer different questions.
A member run of a declared series executes contribute only, and its other output fields are absent, which D13 allows.
A **combine request** names the spec, carries the finalize parameters, and references contributions: the contribution outputs of member records, and usually the contribution output of the previous combine record.
It produces the combined contribution, a stage output kept for the next combine, and the finalized outputs.
It is complete in D1's sense because its references resolve to records that name the files; a recompute of a chained combine walks the chain back to the members.
It is one use of pending outputs as inputs (D6) and needs no scheduling of its own; the runner calls combine over the references and finalize on the result, from the same binding that supplied contribute.
The backend's runnability check requires every referenced contribution to come from the same spec name and version and, since the spec says which parameters finalize reads and the rest are contribute's, that the referenced member records agree on the contribute parameters; anything finer, such as histogram bins that do not match, is combine failing fast.
Without that second check a combine over members contributed under different masks concatenates their events without complaint.
Combine may modify its first argument in place, since D2 makes the partial recomputable.

Which contributions a combine references is the submitter's choice, and it decides the cost.
A rule chains: each arrival is a combine record referencing the previous combine's contribution and the new member's, one read and one write of the partial per arrival, in a throwaway process, exact because combine is associative.
That is what a series costs in shared mode, and it is enough for phases 1 and 2: even a four-dimensional partial of a few gigabytes is read and written once per arrival, and arrivals are minutes apart.
`Aggregation` holds nothing itself, so the contributions sit where the execution shape puts them: in the record store for a chained series, in the wrapper's mapping by member key in a session, in accumulators in a process for the fold.
Removing a member is therefore a combine over the contributions the holder kept, never a subtraction; if their disk copies were evicted, contribute runs again from the raw files.
A superseded combine's partial is needed only by the combine that superseded it, which has already run, so evicting it first (D10) costs nothing until a recompute walks the chain.
In a session the same request shape serves the growing list of B2: the session holds the previous combine's contribution in memory as it holds any output, and each addition is a combine record in the slot.
The warm workflow (D8) has no accumulation special case.

The **fold** is an addition to chaining for a series that arrives faster than its partial can be read and written: a process holds the running contribution in memory and writes a combine record every n arrivals or when the series goes quiet.
Between records the memory copy is a cache, recomputable from the last record and the members since, so D2's invariant holds; this is the warm runner keyed by what it holds, discussed in [stateless.md](stateless.md).
A fold's records carry the reused flag, so publication recomputes them cold along the chain (D11); a rule that publishes its combine therefore chains.

The unit that is combined is whatever the workflow maps over.
Across records it is the framework's map-combine: one contribute per member, one combine.
Inside one record, angle groups in a Bifrost run or chunks of an NMX file, the callable applies the same three stages itself, in parallel if it likes, and the framework never sees it; that the structure serves both is what makes declaring it worth the author's while.

A combine that is not additive, reflectometry's stitching or a tomographic reconstruction, stays an ordinary workflow taking a collection of member outputs (D6), executed by recomputing on every arrival.
The rule (D14) says which of the two it runs; the trigger loop submits contribute for the new member and a combine request, chained for a declared combine and over all members for an opaque one.

**Why.**
One structure serves adding a run in a notebook, a rule's growing series, a batch summed as one request, a parallel reduction of five hundred runs, the esslivedata accumulator, and whatever phase 3 holds in memory; they differ in where the contributions come from and how often the partial is written as a record.
The author declares what every additive workflow already contains, and what `StreamProcessor` already asks for.
The framework stays ignorant of scipp: it never sums, never chooses between summation and concatenation, and never places normalisation.
Held state stays a cache: the invariant of D2, that every value in memory is recomputable from records, is what lets the fold be an addition rather than a design.

**Cost.**
Authors must place normalisation after the accumulation key; ess.sans does, ess.powder does not yet.
Contributions are stage outputs on disk in shared mode, often large, and a chained series keeps k partials until the superseded ones are evicted, the first to go under D10's rule.
What is kept in memory is a short list: a stage's frontier, a session's contributions, which are stored as records in any case, and the fold's accumulators, and nothing else; all of it is a private cache under D3.
The framework cannot check associativity; a test helper runs contribute, combine, and finalize over a list of members in two groupings and compares with the one-shot callable, and every workflow that declares a contribution runs it.
The helper needs nothing workflow-specific and is written once for every spec, and it checks one thing more: that a combined value can be pushed in again, which chaining and the fold both rely on.
The fold needs a long-lived process addressed by its series, which phases 1 and 2 do not have; the accumulator an event-mode contribution needs is `Buffered` over the package's own concatenation and is nothing new.
The process is not needed before a series arrives faster than a partial can be read and written.

## Rules: how requests are made from data (D14)

Batch and automatic reduction make requests without a person filling a form.
They are one mechanism seen twice: a rule is to a batch what a template is to a request.
This section says what is stored, what is a query, and what is deliberately not stored.

A **template** is a stored, immutable, versioned partial run request, from a version-controlled file such as instrument defaults, or from a user saving a request.
Saving a request makes a template with its data-reference fields blank and every other field literal; the user may blank more.
A template moves to a new spec version by copy, and a batch rerun under the copy is a new batch whose records link to the old ones.

A **lookup** is stored, versioned data beside a template: an ordered list of entries, each matching dataset metadata by a value within a tolerance, a pattern, or a run-number range that may be open-ended, and supplying template fills, with at most one wildcard entry for what nothing else matches.
It matches on the fields the dataset source declares for the instrument (D7), and a dataset matching more than one entry is a validation error, not a choice.
Every ISIS batch interface converged on this table under a different name, and [mantid.md](mantid.md) says why it must be data rather than code: the instrument scientist edits it, the UI shows it, and a batch file carries it.

A **rule** is stored, versioned data that makes requests from datasets.
It holds a selector, metadata criteria that pick the datasets it applies to, with a lower bound on the run number or the dataset's creation time, set at creation to the newest dataset the source knows; the template and lookup version it fills; a retry policy, the declared failure reasons on which a failed record is resubmitted as a retry record, up to a limit; and exclusions, datasets it must not fire on, each with a reason.
Optionally it holds a series key, a metadata field whose value keys selected datasets into a **series**, and a combine: for a workflow with a contribution its own combine stage with the finalize parameters, otherwise the spec and template of a combine workflow (D15).
Exclusions and whether the rule is active are mutable state on the rule, not a version, because they change over a beamtime; everything else changes by copy, and records say which version made them.
A paused rule fires on nothing; when it is resumed, datasets that arrived meanwhile are fired on like any other, because the rule's promise is that every matching dataset after its bound is reduced, and a user who wants a gap left alone excludes it or moves the bound.
The trigger loop runs rules, and nothing else does.

**A batch is the records under one label.**
A request may carry a **label** and a **member key**, and the latest record under a label and member key supersedes the earlier ones: one field on the request, one query in the record store, and the outputs of superseded records are the first to evict.
A person submitting a temperature scan picks the label and keys the members by temperature or run number; a rule's records carry the rule's name as their label, stable across the rule's versions, and the dataset as their member key; an interactive tool's slot (D10) is a label with no member key.
A **batch** is the records under one label, and nothing else.
It is not stored, because the set of members is nothing but those records: the table of what was reduced with which values is the query, latest per member key under the label, with each record's rule version, lookup entry, and typed values as columns and the rule's exclusions as rows without a record, and the ISIS batch file is a rendering of that table.
Cancelling a batch is cancelling the queued and running records under its label; the batch semantics are under D6.
A run the selector missed is added by submitting it by hand under the rule's label, and it appears in the same table; a member a person corrects is a new record under the same label and member key, and it supersedes the rule's.

**One operation makes both: apply.**
Apply takes a rule, or a template with its lookup, a set of datasets, and per-member typed values; it fills the template through the lookup for each dataset and returns a group to preview through validate and submit whole (D6).
A person at a batch form calls it with datasets they typed.
The trigger loop calls it with one dataset at a time, as datasets arrive.
Three deliberate operations call it with a query: the **backlog**, the datasets before a new rule's bound that its selector matches, offered when the rule is created; the **reprocess**, the datasets whose latest record under the rule's label came from an older rule version, offered when the rule moves to a new template or lookup version; and the **rerun**, the members under a label that have no completed record.
Each shows through validate what would change for every member before anything is created, and nothing reruns on its own.
The backend never skips a request because an equal one completed earlier; a run that silently did not happen is a decision the user cannot see, and [snakemake.md](snakemake.md) records what that cost elsewhere.

**The trigger loop keeps no memory.**
It fires a rule on a candidate, a new dataset or a completed record, when the selector matches, the candidate lies after the rule's bound, no record exists under the rule's label with the candidate as member key, the candidate is not excluded, and its SciCat entry does not carry our snapshot (D11).
Every clause is a query, so the loop has no state to lose at a restart: what arrived while the backend was down is fired on when it comes back, and the trigger status, the reason any dataset of the proposal fired or did not, is the same query answered for one dataset.
A retry is the same query once more: a failed record under the label whose reason the retry policy names is resubmitted while the records under that member key number fewer than the limit.

**Two kinds of batching.**
Batching for convenience is many independent requests from one template with per-member differences, made by a person or by a rule without a series key.
Batching for merging is one request whose parameter is a collection of references, whether the workflow sums them inside or a map-combine (D6) does it; the set is on the record, so the manual case needs nothing new.
Under a rule the set is derived: the selector and series key place each dataset in a series, and each arrival submits the member's run and a combine request (D15), chained to the previous combine when the workflow declares a contribution and over all members' outputs when it does not.
Successive combines of one series supersede each other under the rule's label, the series value being their member key, so the UI shows one curve per sample that grows; a series of k runs costs k-1 combines, and the superseded ones are the first evicted.
The rule never waits for a series to be complete, because nobody at the instrument can say when it is: the user decides to measure one more angle, and none of ISIS's interfaces waits either.
A series of fixed roles, a scatter and its transmission, is the same rule with the combine fired only when every role is present.
The rule says whether its combine is published (D11); by default it is not.
Series membership is not stored: the members are the records under the rule's label, and which series each belongs to is asked of the source when a combine is submitted, so a metadata correction at the instrument moves a run between series, the next combine reflects it, and earlier records are untouched because they hold resolved references; an exclusion added after a member's record exists drops it from the next combine the same way.

**Precedence is one ladder.**
Template, then lookup entry, then the values the submitter typed, and a blank at any rung falls through to the next.
The record stores the resolved result, and its submission names the entry that applied and the typed values, kept apart, so that a reprocess under a new template or lookup version carries what was typed and recomputes what was filled.

**In pandas terms.**
The batch table is a frame, and the pieces above are how it is built:

| Here | In pandas terms |
|---|---|
| Batch table | A frame: one row per member, the member key as index, parameters as columns |
| Template | Column defaults, one row broadcast over the frame |
| Lookup | An as-of or interval join with tolerance against the dataset metadata, the wildcard as fallback; two matches are an error, not the nearest |
| Precedence ladder | `typed.combine_first(lookup).combine_first(template)`; a blank is a NaN falling through |
| Typed values beside resolved values | Keeping the source frames next to the result frame, instead of writing the result back into the cells |
| Selector | A boolean mask over the dataset metadata frame |
| Series key | `groupby(series_key)` |
| Chained combine | A cumulative reduction within the group; the superseded partials are its intermediate values |
| Latest per label and member key | `groupby(member_key).last()` over the records |

The picture is exact for the view and wrong for the store: a frame is a stored, mutable table, and Mantid's runs table was one, which is where staleness by reset and the write-back into cells came from; here the records are the append-only log and the frame is a query over them.
In a notebook the client interface speaks the picture anyway: the batch table comes back as a DataFrame, and apply accepts one, member key as index and typed values as columns; the ISIS batch CSV is that frame on disk.
That frame is also the member table of D15, each row labelled by its member key, so a batch summed as one request and a batch of independent runs are one table used two ways; pandas stays at the client, and the wrapper maps field names to the workflow's keys.
Which parameters vary across the members, and so make up the table's columns, follows from the request at hand rather than from a declaration on the spec: what the form filled per member, or the data references of a session's growing list.
Two words clash and should be read with care: a series here is a groupby group, not a pandas Series, and apply here is a merge and fill, not `DataFrame.apply`.

**Why.**
The template, the lookup, and the rule are what a person edits and what a UI shows; the records are what happened.
Keeping the two apart is the lesson of ISIS's autoreduction, where the rule and the record were one row and correcting the rule rewrote history, and of the third review pass here, which removed every second copy of the records.
Treating batch and automatic reduction as one mechanism is the lesson of Mantid's reflectometry interface, where a batch tab is exactly this rule, settings, lookup table, autoprocessing search, exclusions, and one runs table, and autoprocessing is the mode of the batch that appends rows; the skeleton's trigger loop had tagged its records as a batch before the text said so.
One label field serves a batch, a rule, and a slot, so one query lists, supersedes, cancels, and evicts for all three, and the status page of automatic reduction is the batch table of the form.
One rule shape covers both kinds of batching in their automatic form, and the only state it keeps beyond its definition is the exclusions and whether it is active.

**Cost.**
Reprocessing after a template change is a client operation over a query, not a stored diff.
A rule's bound is one more thing to get right at creation; the default, the newest dataset the source knows, means a rule made mid-beamtime reduces the backlog only when asked.
The acquisition must write the fields a lookup or a selector matches on into the catalogue; that is a requirement on the instrument, to be stated to the instrument teams early.
A series a person defines by hand, "these runs, and keep combining as more arrive", has no place here; it would be a rule with typed members instead of a selector, and is left out until someone asks for it.

## Ownership, publication, and deployment

**The record store is not a catalogue (also D11).**
Our store answers what was computed; SciCat answers what was measured and what was finalized.
A record holds nothing from SciCat that it did not need to make a decision, so a UI that wants a sample name or the list of a proposal's runs asks SciCat, and a batch member key such as a temperature is supplied by the submitter, not looked up.
SciCat is needed at two moments, resolving a stand-in and publishing; a resolved reference never needs it again, so work on data already referenced continues when the catalogue is slow.
Local mode has no catalogue: a folder is its dataset source, read when asked, and the store holds nothing about the files in it.

**Only finalized data enters SciCat (D11).**
Stage outputs and unreviewed outputs stay in our store, because data in SciCat cannot be removed through the regular API.
Publication is an explicit, idempotent operation on an output, triggered by a user after inspection or by an automatic-reduction rule.
The SciCat entry carries a self-contained provenance snapshot: the raw PIDs the output derives from, the resolved parameters, the spec identity, the package versions and environment; it can be read without any service of ours, and our record is then a copy of it.
A publication may name the PID it supersedes, which the snapshot records, since an entry in SciCat is never removed.
It reads a disk copy: an output that exists only in a session is first written out, and a record whose result was computed from state held from an earlier run is first recomputed in a throwaway process, so that what enters SciCat was computed cold and the record describes it exactly.
The backend records the intent to publish before writing to SciCat and the PID after; the output then has a second durable copy, so a miss on it becomes a download rather than a recompute.
The trigger loop recognizes a published output by the snapshot in its SciCat entry, not by a table of ours, and never fires on it or on records made from its own template; otherwise automatic reduction would reprocess its own output.
A record bound in-process from a notebook is refused for publication unless the client overrides.

**Instrument plus proposal scopes everything (D12).**
Both are mandatory on every record.
Run-number resolution, UI navigation, templates, and authorization by SciCat membership operate within a **proposal**, the experiment allocation that owns data and defines who may access it.
Instrument scientists and commissioning use long-lived proposals.
Artefacts produced there and consumed by every user proposal (direct beam, beam centre, processed vanadium, masks, lookup tables) are marked instrument-shared and readable from any proposal on that instrument; without that, every external user would need membership in the commissioning proposal.
Their disk copies are exempt from retention, because a recompute would run under a user who cannot read the commissioning inputs.
Templates and lookups from such a proposal, the instrument defaults, are marked instrument-shared the same way; a rule is bound to the proposal whose datasets it selects.
A deployment is one backend per instrument, with its own record store and data store; several share a host while load is low.
With one or two users per instrument, of whom at most one works with large volumes, a single backend process serves views comfortably.
Nothing in the model needs cross-instrument state, and a facility-wide entry point, if ever wanted, is a thin front that routes to the instrument backend.

## Changes needed in the workflow spec

The spec in scipp/ess#690 is assumed merged as-is, with these extensions.
They are small in the vocabulary and change the shape of the output side while the PR is open.

**Inputs are parameters of data-reference type (D13).**
The spec has one parameter model and no separate input section.
The parameter vocabulary gains a **data reference** type: a field holding a reference to a file or array output, optionally constrained by kind (raw NeXus file, scipp array, opaque file) and, for arrays, by the same `ArraySpec` that outputs declare.
A parameter of this type is what we call an **input**.
A dataset reference satisfies any kind: the consumer's kind decides how it is materialized, and loading a file as a scipp array fails if it is not scipp HDF5.
A UI choosing a value for such a field asks the **picker**, a client query that returns candidates of matching kind as rows of one shape, a reference, its kind, and display fields: outputs from the record store, and datasets from every dataset source the client has, SciCat for a proposal or a folder in the local application.
Nothing is stored to make that list, and a further place to pick from is another dataset-source implementation, not a change to the picker.
A field may be a union of a literal and a reference, for values such as a beam centre that a user may type in or take from a previous run.
Every difference between an input and a parameter, in this framework, is behaviour selected by the field's type: resolution of run numbers and PIDs, materialization to a file, provenance edges, validation timing, and which widget a UI shows.
esslivedata separates the two because its inputs are streams routed at runtime; here every input is a value known at submission.

**Collections on both sides (also D13).**
The vocabulary gains a **collection**: a list or a dict of values of one declared type, usable as a parameter and as an output.
A reference may name a whole output or one element of it by key.
This gives every mapping between outputs and inputs with one mechanism.
Many-to-one is a collection-typed parameter of references, such as the runs to sum.
One-to-many is a collection output, per detector bank or per angle, consumed whole or element by element.
Many-to-many is a group whose members each reference one element of a pending output by key.
Keys are declared on the spec where the author can, such as bank names, and free otherwise.
Elements of a collection output are stored and served individually, so reading one bank does not load the rest.
No current workflow needs fan-out whose keys are known only after reading the data: Bifrost groups by rotation inside its pipeline, and imaging has no tomography grouping.
If one arises, it is a rule on the completed producer, one template instantiation per key, and not a scheduler feature; Snakemake put it in the scheduler, as checkpoints, and it became the most confusing part of the tool.

**Outputs are a typed model in the same vocabulary (also D13).**
A spec declares its outputs as a model class, mirroring parameters, with a JSON Schema in the serialized form.
An array output is a data-reference field constrained by `ArraySpec`, which gains a `binned` flag; a beam centre is a vector with unit, a fit result a float with unit, a CIF file a reference of kind "opaque file".
Output fields may be optional.
Title and description are field metadata.
A downstream parameter may take a reference to any output field whose type matches, so chaining is a type check between two fields of the same vocabulary.
The spec as proposed allows non-array outputs but gives them no type, which breaks "outputs can be inputs" for exactly the values, such as beam centres and direct beams, that most often feed the next workflow.
Storage placement, inline or in the data store, stops being a spec concept.

**Cheap parameters, an optional code revision, and declared failure reasons (also D13).**
A spec declares which parameters are cheap to change once the workflow is warm, and may carry a code revision.
Both are read by the framework and by UIs and mean nothing to a throwaway run.
A spec may also declare named failure reasons, each with a message; a workflow that fails for a declared reason returns it, the record carries its name, and a UI can explain it and a rule's retry policy can match it.

**A contribution output (also D13, D15).**
A spec may mark one output as its contribution and declare which parameters finalize reads, at which point the binding must supply contribute, combine, and finalize, and a combine request over that spec is valid.
The binding derives the same split from the graph, since each stage knows which parameters it reads, and refuses a spec whose declaration disagrees with it, so that a wrong declaration is a bind error rather than a wrong result.
The declaration itself stays because the backend validates a combine request without importing workflow code, and a combine form has to know which fields it has.
A combine request that reaches the runner carrying a contribute parameter is refused there as well.
The contribution is typed like any output, so a combine's reference to one is the same type check as chaining; what it holds, a numerator and a denominator or more, is the author's.

**One built-in spec, `file` (D1).**
No workflow, one output of file type, and no kind, so the rule above that a file satisfies any kind is checked at materialization rather than at submission.

**Cost.**
The backend must walk the request's values to find references and the spec's JSON Schema, including nested models, to check them.
Structural validation of an array output against its `ArraySpec` happens in the runner at completion, since pydantic cannot check a scipp object.

## Components

- **Backend**: validates a request in three layers, finds the references by walking the request's values, checks each against the type of the output it names, resolves stand-ins to references, creates the record, and hands the request to a launcher.
  Single writer to the record store; exactly one backend process per record store.
- **Client interface**: the backend's Python interface, including validate, apply, views, and the picker (D13).
  This *is* the API.
- **Launcher**: pluggable: session, subprocess, cluster.
  Its interface and the data store's are the two seams where implementations are swapped; both are kept narrow and stable from the first implementation, because retrofitting an interface under existing implementations cost Snakemake a major version.
  Two execution shapes; placement is relative to the session holding a run's inputs, and a group runs in one shape.
  Publishes which specs its environment can run, so the backend can reject unrunnable requests at submission.
- **Runner**: materializes inputs, validates parameters with the real parameter class, calls the workflow, or for a combine request its combine and finalize stages (D15), stores outputs, writes a completion marker to the disk tier, reports to the backend.
  In a session it keeps the workflow callable between runs.
  Never touches the record store.
- **Session**: a runner plus a private memory cache, belonging to one client.
  Created and closed by the client; closing drops its cache.
  In local mode it is the client's own process.
- **Data store**: a registry of disk copies and a disk tier, addressed by reference: record plus output name plus optional key, or a dataset identity for a copy of a dataset.
  Serves runners from the cache of their process when it can, and views from a cache or by partial reads from disk.
  A catalogue file's location comes from SciCat at dispatch and a local file's is its path; a store copy of a local file is kept until dropped explicitly or with its proposal.
- **Record store**: create, read, update status, and queries: records by proposal, time, template version, or rule version; the records under a label and the latest per label and member key; records that reference output X of record Y, or dataset D.
  Holds the runners' logs beside the records.
  Carries a schema version; drops a proposal's records together, never one.
- **Dataset source**: yields new datasets for a proposal as PID plus the metadata fields it declares for the instrument, and persists nothing.
  SciCat, a folder for the local application, and a fake for tests; the picker lists from every source the client has.
- **Trigger loop**: runs the active rules (D14): applies a rule to each candidate, a new dataset or a completed record, that its selector matches after its bound and that has no record under its label; apply fills the template through the lookup and submits, and for a rule with a series key also submits a combine request over the series (D15); never on a candidate the rule excludes or whose SciCat entry carries our snapshot.
  Resubmits a failed record as a retry record when the rule's retry policy names its failure reason, up to the rule's limit.
  Keeps no state: every decision is a query over the records, so a restart changes nothing.
  Has its own visible status: last fire, last refusal with its structured errors, and for any dataset of the proposal the reason it fired or did not, an exclusion included.
- **Publisher**: writes an output to SciCat together with its provenance snapshot.
  Idempotent: the resulting PID is recorded on the output, and publishing it again returns the PID.

## Failure handling

Kept together so it can be read as one piece.

- **Status state machine.** submitted, waiting (pending inputs), dispatched (launcher job ID recorded), running, paused (transient infrastructure failure), completed, failed, cancelled.
  Retry is a new record pointing at the old one, never a status reset.
  A failed record says which of three things happened: the workflow reported a declared failure reason, the workflow code raised, or the framework could not run it; a person, a UI, and a retry rule read the three differently.
- **Completion does not depend on the backend being up.** A throwaway runner writes a completion marker with its outputs to the disk tier before exit; the report through the backend's API is the fast path.
  The marker is written after every output is flushed, and it is the only signal reconciliation trusts: the presence of an output file proves nothing, because a file written by a cluster job becomes visible on other hosts after a delay.
  On restart the backend reconciles dispatched runs against the launcher and the markers, so "finished while the backend was down" is completed, not failed.
- **Runner liveness.** A throwaway runner sends periodic signals; the backend marks a silent run failed after a timeout unless a completion marker exists, and rejects a report that arrives after that.
  This is the lesson of esslivedata's stuck "active" jobs (scipp/esslivedata#823) and of ADR 0008: observe, do not trust acknowledgements.
  Session runs have no liveness timeout; session loss is their failure event.
- **Backend restart.** Records are durable; queued requests are re-dispatched, running ones reconciled as above, paused ones stay paused.
- **External cancellation** (cluster preemption) is detected by the same reconciliation.
- **Cancel of a running request** asks the launcher to stop it; dependents are cancelled.
- **Session loss.** A closed or crashed session drops its cache and its warm workflow.
  Runs in flight there fail; in local mode the session is the client, so there is nothing to resubmit until the user starts again.
- **Logs outlive outputs.** A throwaway runner's stdout and stderr are kept in the record store beside the record; a session run logs to its client's process.
  The structured failure reason covers the failures that were foreseen, and the log is for the ones that were not, such as a process killed for memory.
  Logs are small and live as long as the record; retention applies to the data store only.
- **Failure surfacing** is in scope from the start: a failed record carries a structured reason, so a user sees why a run failed without reading logs, and a trigger loop that is refused at submission is as visible as a run that failed.
  Facilities that built automatic reduction report that the monitoring UI was most of the value.
- **Transient infrastructure failure pauses the run.** A runner that cannot reach an input location or the data store retries at increasing intervals, then reports paused with the cause and exits; the backend also pauses a run at dispatch when a location it resolves is unreachable.
  The record keeps its status and its place in the chain: dependents stay waiting, and the liveness timeout does not apply.
  Resume is the backend dispatching the same record again, when an operator asks or when a reachability probe finds the location back; it is not a retry and makes no new record.
  A paused run is not a failed run: a shared filesystem that is slow for an hour must not cost a retry record per run.
- **Multi-tenancy.** The backend checks proposal access on every reference it resolves or serves, not only at submission.
  Cluster jobs run under the submitting user's account.

## Execution modes mapped onto the model

- **Manual**: submit one request, inspect outputs, resubmit with changed parameters.
- **Interactive**: manual inside a session (D2), with a warm workflow (D8); each series of reruns, whether from a slider, a plot selection, or a growing list of runs, is a slot (D10).
- **Batch**: apply over datasets a person typed or a query returned; the records under one label (D6, D14).
- **Automatic**: the trigger loop applying a rule to each dataset as it arrives (D7, D14).
- **Chaining and map-combine**: pending outputs as inputs (D6).
- **Combining a series**: contribute, combine, finalize (D15); chained through disk in shared mode, through memory in a session.
- **Publication**: explicit publish of an output (D11).

## Explicitly deferred

HTTP transport, real SciCat integration, cluster launcher with its download tokens, view workers and the chunked on-disk layout for dense data, UI framework, UI state in the record store, metrics, agent-facing API.
Remote sessions, on the backend host or in a client process on the user's machine: a session launcher, an idle timeout, and a cap on sessions.
Upload of records from a private local record store to a shared backend, carrying everything a chosen record reaches through its references and nothing downstream of it.
Provisional outputs of a running run, for progress display during chunk-wise processing.
A versioned collection record, appended to by the client and referenced by version, if lists of runs to accumulate grow well beyond a few thousand entries and resending them whole becomes a cost.
Resource hints on the spec for the cluster launcher, such as memory as a function of input size and of the attempt number, which a retry record knows from its link to the record it retries.

## Technology proposals

- pydantic for all data models (the spec already requires it).
- Standard-library sqlite3 for the record store; Postgres later.
- scipp HDF5 for stored scipp data; pluggable serializers for other output types.
- scitacean for SciCat access.
- FastAPI for the HTTP transport when it comes.
- Tiled (bluesky) is a candidate for the disk tier, the HTTP data transport, and per-node access control, behind the data-store interface.
  It has a catalog with search, remote slicing over chunked storage, a policy plugin for access, and a Python client, on SQLite or Postgres.
  It has no scipp semantics, so units, variances, bin edges, masks, and binned data would be a convention we own; it has no server-side reductions, so views stay ours; and it knows nothing of memory caches.
  Decide after the D3 spike, and ask the Tiled developers about a scipp structure family and about reductions.
- No workflow engine, no Dask, no message broker in local mode.

## Open questions

Decisions the team needs to make; my recommendation in brackets.

- **Template sharing.** Both stages of a split workflow share most parameters.
  Facilities that tried template inheritance moved to version-controlled read-only templates with per-dataset substitution.
  [No inheritance. A template may be derived from another by copy, and the record keeps the origin.]
- **Remote sessions.** Where a session runs when interactive use moves to the shared web UI: on the backend host, in the user's own application, or as an interactive cluster job with queue latency at session start.
  [Decide once local sessions exist.]
- **Name of the backend component.** It clashes with esslivedata's "backend services".
  [Keep it unless the two projects are documented together.]
- **Retention policy** for disk copies in shared mode: how long each kind of run's outputs is kept, with superseded slot runs the shortest and automatic-reduction outputs the longest, and the analysis window after which a proposal's records are dropped.
  [One order: outputs of superseded records first, then an intermediate flag the spec puts on an output, meaning cheap to recompute from its inputs, then the kind of run. Authors know which outputs are throwaway, and Snakemake's `temp` and `protected` flags show they get it right.]
- **Local paths after a drop.** A local file's path stays on the submission after its bytes are dropped, until the proposal is dropped.
  [Keep it: a path is not data, and provenance needs it.]
- **Two notebooks on one machine.** The sketch gives each its own store; referencing a result across notebooks needs a local transport.
  [Separate stores now; a local socket form of the HTTP transport later, which also serves the local application.]
- **SciCat push mechanism** for new datasets, if the deployment offers one, and how far ingestion lags the file.
  ISIS's interfaces discover runs from the archive because the catalogue lagged or failed, and their outputs are consequently unknown to it.
  [Measure the lag before phase 1. A filesystem-watching dataset source is the fallback behind the same interface, but a catalogue dataset's identity is its PID, so it can only get ahead of the catalogue and wait, never replace it.]

## Next step

Review this document with the team before implementing.
Then a spike on the two decisions with the most hidden risk, D3 and D6: a data store with private caches and a disk-only registry, an atomic group submit with pending outputs, and a launcher that runs a chain in one session but a batch in throwaway processes, exercised by a fake workflow with two accumulation keys (D15), run one-shot, as a batch with one combine, and as a chained series, with the grouping helper checking that the three agree.
Once the fake holds, a Tiled-backed disk tier as a second implementation of the same interface, checking that a scipp data array with units, variances, bin edges, and a mask survives the round trip.
The two designated testing seams are the fake dataset source and the session launcher; no browser tests in the skeleton.
The full walking skeleton, all components in local mode with no HTTP and no UI, follows if the spike holds.
The skeleton exists as the package `essapps` under `packages/`, import `ess.apps`, laid out for the scipp/ess monorepo: both execution shapes, the group submit with pending outputs, the warm sciline wrapper built on sciline's `Stage`, labels and member keys with the latest-per-label query, dataset references with a folder source and the picker, contributions with contribute, combine, and finalize over sciline's `Aggregation` and combine requests, the lookup, the rule, `apply`, and the memoryless trigger loop, publication, and a LoKI session notebook on the real esssans workflow bound in-process. Not in it: the Tiled-backed disk tier, a SciCat dataset source, the opaque-combine branch of a rule, the fold, HTTP, and a store for templates and rules.

## Glossary

Where esslivedata uses a word differently, the clash is noted.

- **Accumulation key**: a node of a workflow at which per-member intermediates are added; the contribution is the value there. Usually two, a numerator and a denominator, so that normalisation comes after the sum. One accumulator sits at each.
- **Accumulator**: an object that takes contributions by `push` and holds their sum as its value, one per accumulation key. sciline's `Buffered` makes one from a combine function; a running total holds one array instead of one per member.
- **Aggregation**: sciline's object for a declared contribution: the contribute stage, an accumulator per accumulation key, and the finalize stage. It holds nothing between calls, so whoever loops holds the contributions.
- **Annotations**: labels and notes attached to a record after the fact; mutable, outside provenance, read by nothing in the framework.
- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results. In esslivedata "backend services" are the Kafka worker processes; unrelated.
- **Apply**: the client operation that fills a template through a lookup for a set of datasets and returns a group to preview and submit whole. Called by a batch form, by the trigger loop per arrival, and by the backlog, reprocess, and rerun operations. In a notebook it accepts a DataFrame, member key as index and typed values as columns.
- **Batch**: the records under one label, made by a person from a template or by a rule; not a stored unit. In esslivedata a batch is a bundle of messages; unrelated.
- **Client interface**: the backend's Python interface, including validate, apply, views, and the picker. The API.
- **Collection**: a list or dict of values of one declared type, as a parameter or an output. A reference may name one element of a collection output by key.
- **Combine request**: a request that references contributions, from member records and optionally a previous combine, and produces the combined contribution and the finalized outputs. Chained when each references the previous. The runner executes it by pushing the referenced contributions into fresh accumulators and calling finalize on their values.
- **Contribution**: a workflow's output at its accumulation keys; opaque to the framework, additive by declaration. The workflow that declares one exposes contribute, combine, and finalize, which a sciline wrapper takes from an aggregation.
- **Data reference**: a field type: a parameter or output declared to hold a reference to a file or an array rather than a literal. Easy to confuse with *reference*, which is the value such a field holds.
- **Data store**: where the bytes of large outputs live: a registry of disk copies and a disk tier, owned by the backend. Each process that holds data also has a private memory cache, which the store serves from but never registers.
- **Dataset**: data the framework did not compute: a SciCat dataset, identified by its PID, or a file on a user's disk, identified by the instrument and run number it carries or else by its path. The second form of reference. Not a record: no request, no status.
- **Dataset source**: where datasets are discovered and listed; persists nothing. SciCat for a proposal, a folder in the local application.
- **Group**: several requests submitted atomically that may reference each other's outputs before they exist.
- **Input**: a parameter of data-reference type. In esslivedata inputs are data streams and genuinely differ from parameters; here they do not.
- **Label**: a field on a request, with an optional member key; the latest record per label and member key supersedes the earlier ones. A rule's name for the records it makes, a name the submitter picks for a batch, a slot for an interactive tool.
- **Launcher**: decides where a run executes and starts it there.
- **Lookup**: stored, versioned data beside a template: ordered entries that match dataset metadata and supply template fills, with at most one wildcard. What ISIS calls a lookup table, a cycle mapping, or a per-row user file.
- **Local mode**: client, backend, launcher, session, and data store in one Python process. **Shared mode**: the backend as a service used by many people; the **shared service** is that backend's process, which also holds a memory cache.
- **Picker**: the client query behind an input field: candidates of matching kind from the record store and from every dataset source, as rows of one shape.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
- **Recompute**: an explicit operation that runs a record's request again and yields a new record linked to the old one.
- **Record store**: the database of records. Records are never deleted one at a time; a proposal's records are dropped together.
- **Reference**: a value, "output X of record Y", optionally with an element key, usable as any parameter whose type matches. The only way a request names data.
- **Retention**: how long a disk copy is kept within its proposal's lifetime; it applies to bytes, never to single records.
- **Rule**: stored, versioned data that makes requests from datasets: a selector with a lower bound, a template and lookup, a retry policy, exclusions, an active state, and optionally a series key and a combine. Applied by the trigger loop to each arrival and by a person to a set at once; a rule is to a batch what a template is to a request.
- **Run record**: a run request plus what happened to it. Called "run", never "job", except for the launcher's own job IDs: in esslivedata a job is a running streaming workflow.
- **Run request**: everything needed to execute a workflow once.
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue. **PID**: SciCat's persistent identifier for a dataset.
- **Series**: the datasets a rule keys together by a metadata value, combined again whenever one joins.
- **Session**: a runner plus a private memory cache, belonging to one client, keeping the outputs of its runs and the workflow itself in memory. A cache over records.
- **Slot**: a label an interactive tool owns, with no member key. The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs. Defined in scipp/ess#690.
- **Stage**: the part of a sciline graph from named inputs to named outputs, with everything else computed once and held at its frontier. The warm workflow is a stage whose inputs are the cheap parameters; contribute and finalize are the two stages of an aggregation. A **stage output**, an output one spec produces and another takes (D4), is such a boundary value kept as a record.
- **Submission**: the field on a run record that says how its request was made: the template version, the rule version and lookup entry when a rule filled it, and the values the submitter typed beyond template and lookup. Explanation, not provenance.
- **Template**: a saved, versioned run request with some fields left blank.
- **Throwaway process**: a subprocess or cluster job that runs one request and exits; the execution shape of shared mode.
- **Trigger loop**: applies the active rules to new datasets and completed records. Keeps no state: every decision is a query over the records.
- **View**: a small piece of an output's data for display, computed by the process holding a copy. Not a run.
- **Vocabulary**: the set of types the workflow spec allows for parameters and outputs.
- **Warm workflow**: the workflow object kept alive in a session between runs. One per spec version.
- **Workflow**: the scientific code that turns inputs into results. Typically a sciline pipeline; the framework does not care.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).

## Index of decisions

Numbering follows reading order. It is provisional until the wider review and stable after it; then add at the end.

| | Decision | Where |
|---|---|---|
| D1 | Every value is an output of a record or a dataset; stand-ins resolve at submission; records are dropped by proposal, never singly; recompute is explicit | Records and references |
| D2 | Requests are stateless; execution may be stateful inside a session, which is a private cache | Choice 1 |
| D3 | The data store registers disk copies only; memory caches are private; two execution shapes; three lifetimes: session, proposal, catalogue | Choice 1 |
| D4 | Reuse across requests means a workflow boundary | Choice 1 |
| D5 | The record store is ours, small, and implementation-agnostic; the backend is its single writer | Choice 2 |
| D6 | One scheduling primitive: pending outputs as inputs; map and combine are optional uses of it | Choice 2 |
| D7 | The dataset source is abstracted, declares the matchable metadata fields, and persists nothing; not Kafka | Choice 2 |
| D8 | Framework-to-workflow contract: a callable from parameters to outputs; declared cheap parameters; three validation layers | Choice 3 |
| D9 | The client interface is the API; validate is separate from submit; HTTP later; notebook first | Choice 4 |
| D10 | Interactive plotting: views are not runs and return plain arrays; event data is never viewed; reruns live in slots, which are labels | Choice 4 |
| D11 | Only finalized data enters SciCat; publication reads a cold disk copy; the record store is not a catalogue | Ownership, publication, and deployment |
| D12 | Instrument plus proposal scopes everything; one backend per instrument | Ownership, publication, and deployment |
| D13 | One type vocabulary: inputs are data-reference parameters, outputs a typed model, collections on both sides | Spec changes |
| D14 | Templates, lookups, and rules are the stored data requests are made from; a rule is to a batch what a template is to a request; a batch is the records under one label, not a stored unit; one apply operation serves the form, the loop, and reprocessing; the loop keeps no memory; a rule keys datasets into series and never waits | Rules |
| D15 | An additive combine is declared: a contribution output at the workflow's accumulation keys, with contribute, combine, and finalize; a combine request chains through disk, a session, or a warm runner, and the partial is always recomputable | Combining |

## Review log

Reviewed before being shown to the team by independent AI reviewers from distinct angles: architectural consistency, fit with the real ess workflows read from source, operations and failure modes, prior art at other facilities, plain-language readability, lessons from esslivedata, a fresh reader, a maintainer's view of long-term pain, minimality, and adversarial scenarios.
The first pass shaped the failure-handling section, the record fields for resolved values and package versions, the serializer rule, instrument-shared artefacts, slots, and the open questions.
The second pass removed the mechanisms that created a second copy of truth or an implicit action: a registry that tracked copies in memory, recompute triggered by reads, identical-request reuse, record deletion by reachability, memory budgets in sessions, and slots as a backend object.
It also corrected the description of what a warm workflow reuses against the workflow source, and added the binned flag, optional map-combine, the completion marker, and intent-to-publish.
The third pass removed what would have made the record store a second catalogue: file records created by discovery with copied metadata and stored mount paths, and records kept forever; file records are now created on reference and hold identity only, catalogue locations are asked of SciCat, and records live as long as their proposal.
A fourth pass read the sketch against Snakemake's history, in [snakemake.md](snakemake.md); it added the runner's log to the record, completion by marker alone, batch rerun as a client operation with a request-equality query, data-dependent fan-out as a trigger rule, the record-replay test helper, the deferred resource hints, and a recommendation on the retention question.
A fifth pass read it against AiiDA's history, in [aiida.md](aiida.md); it added UUID run IDs, annotations beside the record, the group ID, three kinds of failure with reasons declared on the spec, the paused status for transient infrastructure failure, retry by reason in the trigger loop, the session-restart rule for code changes, and the export rule for the deferred upload.
A sixth pass read it against Mantid's ISIS batch interfaces and FIA, in [mantid.md](mantid.md); it added the lookup as versioned data used by batch and the trigger loop, the lookup entry on the record, rules as data, exclusions as annotations on file records, the explicit reprocess operation when a loop moves to a new version, the slot as a label usable by the trigger loop, and three open-question entries: not waiting for a series, batch definitions, and catalogue lag.
A seventh pass read the three prior-art passes together for incremental creep and consolidated what they had added: template, lookup, and rule are one section with one decision, D14; a batch is a tag rather than a stored unit, because the set of members is a query over records; the rule replaces the batch definition and holds the exclusions, so annotations are notes again; the record gets one submission field in place of a group ID, a template version, and a lookup entry; the group ID, the request-equality query, and the trigger loop's use of slots were dropped; logs moved to the record store; paused runs got a mechanism; and the open questions on waiting for a series and on batch definitions closed.
An eighth pass asked whether accumulation could be deferred at all, read how ess.sans, ess.reflectometry, ess.powder, ess.bifrost, and the streaming module combine runs, and found the sketch had three answers that did not meet; it added D15, the declared additive combine with its three stages, removed the accumulation special case from the warm workflow, and made a series combine a chained request rather than a recombination of member outputs.
A ninth pass asked whether batch and automatic reduction were more unified than the sketch had set out to make them, and found that a rule is to a batch what a template is to a request, which is what Mantid's reflectometry batch tab already is; it merged the slot and the batch ID into one label with an optional member key, made a rule's records a batch under the rule's name, named the one apply operation behind the form, the trigger loop, and the backlog, reprocess, and rerun operations, made the trigger loop stateless by putting a lower bound on the rule's selector, gave the rule an active state, put the typed values on the submission so that a reprocess carries them, and moved labels and apply from phase 2 into phase 1.
A tenth pass, prompted by the team review's confusion over "no filenames" and file records, asked what a file record served and found nothing that a dataset identity does not: the PID is the identity, the checksum and the split of identity from location do the work against stale paths, a raw file is viewable only through a preview run, and the trigger loop already took datasets and records as two kinds of candidate; it replaced file records with the dataset as a second form of reference, stores nothing per dataset, since the proposal check happens at submission and the data store registers only the copies it makes, keyed by reference in either form, took a local file's identity from the run identity it carries rather than a hash at submission, made a folder a dataset source for the local application, and named the picker, the query behind an input field, so that listing what can be picked is a query over the record store and the dataset sources rather than a table of ours.
An eleventh pass read the sketch against scipp/sciline#245, the proposal to replace map/reduce with stages and aggregations composed outside the graph, in [stages.md](stages.md); it renamed accumulation point to accumulation key, sciline's word for the same thing, named the stage, the accumulator, and the aggregation behind the warm workflow and the declared combine, made the split between contribute's and finalize's parameters a declaration the binding checks against the graph, and required the members of one combine to agree on the parameters contribute reads.

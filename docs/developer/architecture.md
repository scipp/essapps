# Architecture sketch for ESS data-reduction applications

Living document.
Records the choices made so far, the alternatives that were on the table, why each choice was made, and what it costs.
Companion to [scoping.md](scoping.md), which states goals and scope.
Nothing here is implemented yet.
Technology choices at the end are proposals.
A reading edition with diagrams is [architecture.html](architecture.html); its wording follows this file.

## How to read this

The design rests on one modelling idea and four choices.
The idea comes first, under "Records and references".
The four choices follow, each with the options that were rejected, so that the team can disagree with a choice rather than only with its consequences.
Everything after that is consequence: the rules that fall out, the changes the workflow spec needs, the components that implement it all, and how failures are handled.
Decisions carry stable identifiers D1 to D14 so discussion can point at them.
They are indexed at the end, next to the glossary.
Terms are introduced where they are first needed.

## The picture in one paragraph

A user, a script, or an automatic trigger submits a *run request*: which workflow to run, with which parameters, on which input data.
The *backend* checks the request, writes it down as a *run record*, and asks a *launcher* to start a *runner* somewhere: in the same process, in a subprocess, or on the cluster.
The runner fetches the inputs, calls the scientific workflow code, and stores the results.
Any output of a record, whether a single number or a large array, can be an input of the next request, named as *output X of record Y*.
Raw files from the catalogue and uploaded files are records too, so every input is named the same way.
Interactive work happens in a *session*: a long-lived process that keeps the workflow and its data in memory between runs, while the records look the same as for any other run.
Everything the backend knows is in the records, so any result can be traced back to raw data and parameters, and any result can be recomputed if it was thrown away.
Batch reduction is many requests made from one *template*.
Automatic reduction is a loop that makes requests from a template whenever new data appears.
Publishing a result to the data catalogue is a separate, deliberate step.

## Records and references

The framework has one way to name a piece of data: *output X of record Y*.
This section is what follows from taking that seriously (D1).

A **run request** is everything needed to execute a workflow once: which spec, the parameter values, and the instrument, proposal, and submitter.
A **spec** is a workflow's declared interface: name, version, parameters, outputs.
It is independent of how the workflow is implemented or where it runs, and is defined in scipp/ess#690.
A request is complete: sufficient to reproduce the outputs from scratch.
It is plain, JSON-serializable data, even when it never leaves a process.

A **run record** is the request plus what happened to it: run ID, status, timestamps, output values, the resolved parameter values including defaults, the software versions of the runner environment, and optionally a batch ID, a template version, and the record this one retries.
It is immutable once the run completes, except for status, and it may be dropped once past its retention and referenced by nothing (choice 1).
Resolved values and package versions are what make "recompute from the record" true; without them a changed default or a package upgrade silently changes what a record means.
Small output values are stored in the record; large ones are held by the data store, and the record says only that they exist.
Which of the two applies is decided by type and is invisible to clients.

A **reference** is a value, "output X of record Y", that any parameter field may hold instead of a literal, provided the field's type matches the output's.
It is the only way a request names data.
A reference never contains a path, and the data store's internal keys never appear in a request.
A reference may name a **pending output**, one whose record has not completed yet; the backend holds the request until it does.
The record keeps references in reference form, so **provenance**, the chain from any result back to raw data, parameters, and software, is the graph you get by following them.

**Files are records too.**
A SciCat dataset or an uploaded file is a **file record**: a record of one built-in spec, `file`, which has no workflow and one output, the file.
The dataset source creates one file record per SciCat PID; an upload creates a new one.
A raw file from the catalogue and a result from last week are therefore the same thing to a request, and a new raw file and a completed processing stage are the same kind of event to automatic reduction.

**Stand-ins resolve at submission; data materializes at execution.**
Users submit a local path, a PID, or a run number (per instrument).
The backend turns each into a reference to a file record before persisting anything, creating the record if the PID has none yet, because provenance must not depend on a search that could give a different answer later.
The runner materializes references at execution time: files as local paths, arrays as scipp objects.
Local files that must reach a remote runner are uploaded into the data store first, which creates their file record.

**Whether an output is usable is two questions**: the record's status, and whether the data store holds a copy.
A missing copy is recomputed from the record.

| Output of | Where the truth lives | On a missing copy | Copies evictable |
|---|---|---|---|
| A run record | The record: parameters, references, versions | Recompute | Yes |
| A file record from SciCat | The PID; a mounted facility filesystem is its disk tier | Download again | Yes |
| A file record from an upload | The copy in the data store | Nothing to recover from | No: pinned |
| A published run record | The record, and now also the PID | Download rather than recompute | Yes |

Two more kinds of data complete the model.
A **template** is a stored, immutable, versioned partial run request, from a version-controlled file such as instrument defaults, or from a user saving a request.
A **batch** is a set of independent runs from one template with per-member overrides, tagged with a batch ID.

**Why this is the foundation.**
One addressing scheme serves inputs, views, publication, and provenance, and the scheduler has one kind of dependency.
There is no "which record produced this" query, because the reference names the record.
A handle with its own origin and lifecycle would be three special cases of this: the origin is the producing record, and the lifecycle is record status plus copy availability.

**Cost.**
One record per dataset the source discovers, so the record store holds a dataset table.
A published output must not come back as a second record, so the dataset source checks PIDs against records.

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
  Execution may happen in a **session**: a long-lived process belonging to one client, which keeps the workflow object and the outputs of earlier runs in memory.
  Memory never crosses a process; anything that must reach another process goes through disk.

**Choice.**
The last option, with two invariants that keep it safe (D2).
Session identity never appears in a record.
Everything a session holds can be recomputed from records, so a session is a cache, and losing one costs only time.
This is esslivedata's lesson applied the other way round: ephemeral identities are dangerous when other things depend on them, so nothing depends on this one.

A session holds one **warm workflow** per spec: the workflow object kept alive between runs, so a rerun recomputes only what a changed parameter affects.
Tuning a stage output and its consumer together, such as vanadium processing and a sample reduction, uses two, chained through the session's memory.

From the framework's side there is one kind of rerun: a new, complete request.
Changing a threshold and adding one more run to a list of runs to sum look the same, a full parameter set that differs from the previous one in one field, and the record of each rerun stands on its own.
Whether the session reuses unchanged intermediates or folds a new list element into an accumulator is a difference the workflow's adapter discovers by comparing the two parameter sets (choice 3), not a kind of request.
The list is resent whole on every rerun; it holds references, not data, so at the few thousand entries expected it stays under a megabyte.

Initially sessions exist only in **local mode**, where client, backend, launcher, session, and data store are one Python process: a notebook, or a local application.
In **shared mode** the backend runs as a service used by many people.
Batch and automatic reduction run there as a stateless service, and the shared web UI submits and inspects.

**The data store follows from the choice (D3).**
It is one component: one registry and one disk tier, owned by the backend, plus a **memory tier** in every process that holds data, which means each session and the shared service in shared mode.
A copy in a memory tier is visible only to its process.
Every memory tier has a budget, and copies in memory or in a local download cache are evictable, because the record can bring them back.
Treating memory as an evictable cache is also what resolved esslivedata's memory problems (scipp/esslivedata#1274).
A runner asks the store for an input and hands it an output; the store serves from the memory tier of that process when it can and otherwise reads or writes disk.
The runner never knows about tiers, and the launcher decides only where a run executes; whether anything is written falls out of that. Remote execution is a different launcher, not a different execution model.

Where a run executes decides whether its data touches disk:

- **In the session holding its inputs.** Inputs come from that memory tier and outputs stay there; nothing is written.
  This is local mode, and also a chain of dependent requests that the launcher places in one runner.
  When something outside the session later needs such an output, publication or a request placed elsewhere, the store writes the copy out from the session; if the session is gone, the output is recomputed from its record.
- **In a throwaway process.** The store there has no lasting memory tier, so outputs go to disk before the run reports completion.
  This covers shared mode, the subprocess launcher, and fan-out members.
  The shared service loads an output into its memory tier on first access: written once, loaded once, every further view served from memory.
- **No consumer left.** Once retention expires, the output is dropped.
  A later request or view recomputes it from its record.
  A record has a retention of its own and is dropped once past it and referenced by nothing; references are the only way to name data, so that is a reachability check.

Every run's outputs have at least one consumer: the client that submitted the run, which will plot them, chain them, or both.
Local mode is the degenerate deployment where client, session, and the whole data store are one process; the client interface is the same as in shared mode.

**A rule that follows: reuse across requests is a workflow boundary (D4).**
A value that other requests reference must be an output of a run of its own, with its own record.
Workflow authors therefore cut a workflow into separate specs exactly where such a value arises, and nowhere else.
Two reasons produce a cut.
Reuse: one artefact feeds many runs, such as processed vanadium, a beam centre, or a direct beam, which sample reductions, batch, and automatic reduction all take as an input.
Iteration without a session: an expensive stage whose result is tuned from a stateless runner or from the shared web UI, such as loading and preprocessing a large run before adjusting its post-processing.
Inside a session the second reason disappears: one unsplit spec is enough, because the warm workflow recomputes only what changed, and the loaded data stays an **intermediate**, a value inside the workflow such as a sciline graph node, which the framework never sees: no record, so no reference, and it dies with the session.
The first reason holds in a session too, because the artefact needs a record of its own before batch can reuse it.
A **stage output** such as processed vanadium is an ordinary output with a record; the word only names the role.
Splitting is the only strategy that works on a fire-and-forget remote runner, and it lets the UI tell which stage is cheap, because it is a separate workflow.
Deriving the split automatically from a sciline graph is possible, as `ess.reduce.streaming.StreamProcessor` does for live data, but that is a tool on the workflow side; the framework does not know about it.

**Why.**
Batch, automatic reduction, and provenance are properties of the record, not of the process that executes it, so pinning "stateless" at the record level costs them nothing.
A stage output can be huge, and writing one to disk is wasted work when its only consumer is in the same process; recompute is often cheaper than storage.
Making a session's memory an ordinary tier of the store, rather than a special case, is what lets a remote session be added later as one more launcher and one more tier.

**Cost.**
Two lifetimes for the workflow object in the runner: once per run, or once per session.
Remote sessions need a memory budget per session, a cap on sessions, idle timeouts, and a store protocol between a session's memory tier and the registry.
That is why they are deferred, and why the shared web UI initially has no interactive loop.
Shared mode pays one disk write and one read per output and a process start per run.
Placement is a launcher decision and must be explicit in its interface; whether a chained consumer exists is only known for requests submitted together as a group (choice 2).
In shared mode the registry must know which memory tiers hold a copy, trivially so while every tier is in the backend's own process.
The author chooses where to cut a workflow, and the cut is not always clean: processed vanadium in diffraction is binned on the sample's edges, so the stage output must be oversampled.
Both stages of a split workflow typically share parameters; see the open question on templates.
For an unsplit workflow in a session the spec does not say which parameters are cheap to change, so the UI cannot choose a slider over a run button; see deferred.
Every rerun in a session is a complete record, so a series of N slider moves or N appended runs is N records, and only the last is ever needed; slots (choice 4) and record retention keep that from being what a person sees.

### Choice 2: Own the records and the scheduling, or adopt an engine (D5, D6, D7, D8)

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
The **record store** is SQLite on local disk in local and single-backend mode, Postgres if the backend ever scales.
Records contain nothing sciline-specific.
The store carries a schema version, and a stored parameter set that no longer matches its spec version must fail loudly, never be silently defaulted; schema versioning was left unresolved in esslivedata and needed hand-run migrations (scipp/esslivedata#915).

**Identical requests reuse outputs (D6).**
A request whose content matches a completed record with the same package versions may reuse that record's outputs instead of running.
That is what makes a dragged slider or a batch rerun with mostly unchanged members affordable.

**The backend is the single writer.**
Exactly one backend process per record store, enforced by a startup lock.
The runner reaches storage for data and the backend's API for everything else, and never touches the record store; there are no database credentials on compute nodes.
Raw files that already sit on the facility filesystem are resolved to their path by the backend at dispatch; only otherwise does the runner download, using a short-lived token issued by the backend.

**One scheduling primitive: pending outputs as inputs (D7).**
Map is a batch with a shared fixed input and a per-member parameter, a file or a chunk range.
Combine is one request whose inputs are references to the map outputs, submitted before they exist.
A group of requests is submitted atomically and gets its IDs back; inside the group, requests refer to each other by local alias.
The backend holds a request until every record it references has completed, fails it if any of them fails, and cancels it if any is cancelled.
Groups must be acyclic; a request waiting on an input that never arrives times out.
Merge strategy, memory during merge, and chunk semantics belong to the combine workflow.
Batch members are independent: no ordering between them, and rerunning a member is a new record with the same batch ID; anything else is chaining.

**The dataset source is abstracted (D8).**
Its interface: for a proposal, yield new datasets as PID plus metadata; the backend turns each into a file record.
A SciCat implementation, polling or push as the deployment allows, and an in-memory fake.
Arrival may be out of order; the interface does not promise a monotonic cursor.

**Why.**
No engine gives the stateless request the whole design rests on.
Single writer avoids the multi-client ownership problems that produced most of esslivedata's hard bugs (scipp/esslivedata#1285, #714, ADR 0007), and one runner gives one code path for execution.
Pending outputs as inputs is the smallest addition that covers temperature scans, angle series, mask series, and large-file chunking.
Real combines are not sums: reflectometry uses a variance-weighted overlap fit, SANS concatenates events; so merging cannot be a framework concern.
Two dataset-source implementations from day one keep tests off SciCat.

**Cost.**
We own dependency handling, failure propagation, and cancellation: a small scheduler.
"No broker" is a local-mode decision to be re-examined when the cluster launcher is built.
Fan-out whose size is only known after reading the file, such as grouping by rotation angle inside one file, is not covered; see open questions.

### Choice 3: The contract with workflow code (D9)

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
One callable (D9).
Spec identity is bound to an implementation via Python entry points.
The entry point returns a callable that takes the validated parameter model and returns the output model.
A stateless runner constructs it and calls it once.
A session runner constructs it once per spec version and calls it for every run, so the callable may keep state between calls; that is the warm workflow.
The framework never imports sciline.

Data-reference fields are materialized by kind:

| Reference kind | Arrives in the callable as | Why |
|---|---|---|
| Raw NeXus file | local path | Loading NeXus is workflow-specific: which detector banks, which monitors. |
| Opaque file (CIF, ORSO, ...) | local path | The framework cannot know the format. |
| scipp array | scipp object | The one format the framework writes itself, so it can read it; served from memory when the store holds a copy, from scipp HDF5 otherwise. |

Outputs are returned as objects matching the output model.
The runner validates them against it, stores vocabulary-typed small values inline, and hands the rest to the data store, which serializes scipp objects to scipp HDF5 when they must reach disk and requires other types to come with their own serializer, declared with the output.
Parameter validation authority lies with the process that owns the parameter class (ADR 0001 in scipp/ess#690), which is the runner; the backend validates against JSON Schema only.
Chunk-wise processing of one large file, as the NMX workflow does, happens inside the callable and is invisible to the framework.

A sciline workflow meets the contract through an adapter that diffs each call's parameters against the previous call and recomputes only what lies downstream of the change.
That adapter is `ess.reduce.streaming.StreamProcessor`, or a sibling sharing its machinery: parameters expected to change are its context keys, and list-valued parameters that accumulate, such as a growing list of runs to sum, are its dynamic keys, fed with the list's new elements.
Any other change, including removing a list element, resets the adapter.
Which parameters may change cheaply is declared on the workflow side, next to the adapter.

**Why.**
Arrays arriving as objects is what lets a chain in a session stay in memory while workflow code looks the same in every mode.
One callable rather than a separate incremental protocol keeps one execution path: the only difference between runners is whether the callable is kept.
Reuse inside the adapter is correct by construction from the sciline graph, which is stronger than the record-level reuse rule of D6.
The declaration of cheap parameters cannot be avoided, because caching every intermediate is not affordable and the graph does not know compute cost.
Serialization of outputs must be pluggable because the outputs that get published are often not scipp objects.

**Cost.**
Two validation points, with the runner's being the authoritative one.
The runner loads scipp arrays whole.
Accumulation over a list is exact only when nothing else changed; the adapter must reset otherwise, and nothing outside it can check that it does.
Incremental accumulation is also exact only for quantities that combine element by element; anything that depends on the whole list, such as normalisation by the summed monitor counts, must be an accumulator on the right node, which is the workflow author's job and not checkable from outside.

### Choice 4: How clients reach the system (D10, D11)

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
The Python client interface is the API (D10).
Every UI, notebook, or service reaches the backend only through it; HTTP is a later transport for the same interface, not a second API.
Requests, records, templates, and references are plain data even in local mode.
Clients observe change by pulling with a version counter, never by callbacks carrying data.
No standalone local application initially: a notebook on the library is the local application.
When one is wanted, it is the same web UI hosted in the local process against the in-process interface; the shared deployment is the same UI over the HTTP transport.
The UI framework is chosen after the backend skeleton exists; a Python-driven web framework satisfies API-first if it only uses the client interface.
Qt is out.
UI state such as layouts and plot configuration lives in the record store, not in a second per-dashboard store, which was an anti-pattern in esslivedata (scipp/esslivedata#1076, #1070).

**Views are not runs (D11).**
A view is a request for a small piece of an output's data for display: label-based slicing, reduction over dimensions such as sum or mean, and downsampling to a display resolution.
Views are part of the client interface, so there is one endpoint in every mode, and a view result is small by construction.
They are served by the data store from the memory tier holding a copy, are not recorded, and are never inputs to a run.
When a user wants to compute further from a slice they found interactively, the slice specification becomes a parameter of the next workflow.
Overlaying several runs in one plot is several views; anything beyond slicing, reduction, and downsampling, including the difference of two runs, is a workflow.
Data larger than the memory budget is sliced by partial reads from disk, so dense arrays are stored chunked in a layout that supports that.
Event data cannot be sliced from disk and is loaded whole or histogrammed by a workflow first.
In local mode a client may also turn a reference into a scipp object directly, so plopp and the full scipp API work in a notebook; that is a convenience on top of views, not a second mechanism.

**Plot selections are ordinary parameters (also D11).**
Interactive tools bind to parameters of shared vocabulary types: range, rectangle, polygon.
Each selection change submits a new stateless run of the cheap stage.

**Interactive reruns live in slots (also D11).**
A *slot* is a series of runs that supersede each other: a slider moving a threshold, a selection being dragged, or a list of runs to sum growing by one.
A request may carry a slot key.
The client interface supplies one whenever a request reruns a warm workflow, so a notebook user gets a slot without asking for one, and can opt out or fork a slot from its current record to compare two variants side by side.
Forking must be a one-line client operation, or people will skip slots.
A new request in a slot cancels queued requests in that slot.
A slot has a current record and a history; superseded records are short-retention and dropped once nothing references them.
Tools address slots, not records: a listing shows one row per slot, replay replays its current record, and inspection shows the current record with its diff against the previous one, which is "one more file" for an accumulation series and "one value changed" for a slider.
Slot runs are placed in the submitter's session, which until remote sessions exist means local mode.

**Why.**
One API keeps the UI out of backend internals.
Exploring a 4D volume by dragging through 2D slices must not create records, and the frontend must never receive the volume; the slice-becomes-parameter rule keeps provenance exact without making views part of it.
The slot is the one interactive concept in the framework, and it is one field on the request: the stable identity that a plot, a record browser, and a replay tool all need across superseded runs, the same split between a stable data key and a per-result key that esslivedata needed (scipp/esslivedata#1062).
Without it a session's reruns are hundreds of complete, near-identical records, which is the right model for provenance and the wrong thing to show a person.

**Cost.**
A local application in one process shares the interpreter between UI and runs, so a long run blocks the UI unless the session moves to a subprocess, which needs the remote-session machinery.
A view vocabulary in the client interface, and a chunking decision at write time for dense data.
Through the client interface a user explores declared outputs only; a notebook can compute any node of a sciline workflow.
Interactive feedback is restricted to sessions, and initially to local mode.

## Ownership and publication

Two rules that do not belong to any choice above.

**Only finalized data enters SciCat (D12).**
Stage outputs and unreviewed outputs stay in our store, because data in SciCat cannot be removed through the regular API.
Publication is an explicit, idempotent operation on an output, triggered by a user after inspection or by an automatic-reduction rule.
The PID is recorded on the output, which now has a second durable copy, so a miss on it becomes a download rather than a recompute.
The dataset source recognizes such a PID and does not create a file record for it; otherwise the same data would exist twice and a rule that fires on new datasets would fire on our own output.

**Instrument plus proposal scopes everything (D13).**
Both are mandatory on every record.
Run-number resolution, UI navigation, templates, and authorization by SciCat membership operate within a **proposal**, the experiment allocation that owns data and defines who may access it.
Instrument scientists and commissioning use long-lived proposals.
Artefacts produced there and consumed by every user proposal (direct beam, beam centre, processed vanadium, masks, lookup tables) are marked instrument-shared and readable from any proposal on that instrument; without that, every external user would need membership in the commissioning proposal.

## Changes needed in the workflow spec

The spec in scipp/ess#690 is assumed merged as-is, with these extensions.
They are small in the vocabulary and change the shape of the output side while the PR is open.

**Inputs are parameters of data-reference type (D14).**
The spec has one parameter model and no separate input section.
The parameter vocabulary gains a **data reference** type: a field holding a reference to a file or array output, optionally constrained by kind (raw NeXus file, scipp array, opaque file) and, for arrays, by the same `ArraySpec` that outputs declare.
A parameter of this type is what we call an **input**.
The file output of a file record satisfies any kind: the consumer's kind decides how it is materialized, and loading a file as a scipp array fails if it is not scipp HDF5.
Lists of references are allowed; comparing or combining runs is a workflow with such a list as input.
A field may be a union of a literal and a reference, for values such as a beam centre that a user may type in or take from a previous run.
Every difference between an input and a parameter, in this framework, is behaviour selected by the field's type: resolution of run numbers and PIDs, materialization to a file, provenance edges, validation timing, and which widget a UI shows.
esslivedata separates the two because its inputs are streams routed at runtime; here every input is a value known at submission.

**Outputs are a typed model in the same vocabulary (also D14).**
A spec declares its outputs as a model class, mirroring parameters, with a JSON Schema in the serialized form.
An array output is a data-reference field constrained by `ArraySpec`; a beam centre is a vector with unit, a fit result a float with unit, a CIF file a reference of kind "opaque file".
Title and description are field metadata.
A downstream parameter may take a reference to any output field whose type matches, so chaining is a type check between two fields of the same vocabulary.
The spec as proposed allows non-array outputs but gives them no type, which breaks "outputs can be inputs" for exactly the values, such as beam centres and direct beams, that most often feed the next workflow.
Storage placement, inline or in the data store, stops being a spec concept.

**One built-in spec, `file` (D1).**
No workflow, one output of file type, and no kind, so the rule above that a file satisfies any kind is checked at materialization rather than at submission.

**Cost.**
The backend must walk the request's values to find references and the spec's JSON Schema, including nested models, to check them.
Structural validation of an array output against its `ArraySpec` happens in the runner at completion, since pydantic cannot check a scipp object.

## Components

- **Backend**: validates a request against the spec's JSON Schema, finds the references by walking the request's values, checks each against the type of the output it names, resolves stand-ins to file records, creates the record, and hands the request to a launcher.
  Single writer to the record store; exactly one backend process per record store.
- **Client interface**: the backend's Python interface, including views.
  This *is* the API.
- **Launcher**: pluggable: session, subprocess, cluster.
  Placement is relative to the session holding a run's inputs.
  Publishes which specs its environment can run, so the backend can reject unrunnable requests at submission.
  May run a group of dependent requests in one runner process.
- **Runner**: materializes inputs, validates parameters with the real parameter class, calls the workflow, stores outputs, reports to the backend.
  In a session it keeps the workflow callable between runs.
  Sends periodic liveness signals while running.
  Never touches the record store.
- **Session**: owns a runner and a memory tier of the data store.
  Created and closed by a client; closing drops its copies.
  In local mode it is the client's own process.
- **Data store**: one lookup and listing for every large output, addressed as record plus output name.
  The registry knows which memory tiers and which disk hold a copy.
  Serves runners from the memory tier of their process when it can, and views from the memory tier holding a copy or by partial reads from disk.
  An upload has nothing to download from, so its disk copy is pinned.
  A SciCat file on a mounted facility filesystem has no local copy; the mount is its disk tier.
- **Record store**: create, read, update status, and one query: records that reference output X of record Y.
  Carries a schema version.
- **Dataset source**: yields new datasets for a proposal as PID plus metadata; the backend creates one file record per PID, and none for a PID that one of our own records already carries because we published it.
  One real implementation (SciCat) and one fake for tests.
- **Trigger loop**: on a completed record, or a group of them, matching a rule, instantiate a template and submit.
  A new raw file and a finished processing stage are the same kind of event.
- **Publisher**: writes an output to SciCat together with its provenance.
  Idempotent: the resulting PID is recorded on the output, and publishing it again returns the PID.

## Failure handling

Kept together so it can be read as one piece.

- **Status state machine.** submitted, waiting (pending inputs), dispatched (launcher job ID recorded), running, completed, failed, cancelled.
  Retry is a new record pointing at the old one, never a status reset.
- **Runner liveness.** The runner sends periodic signals; the backend marks a silent run failed after a timeout and reconciles dispatched runs against the launcher (cluster queue, process liveness) on startup.
  This is the lesson of esslivedata's stuck "active" jobs (scipp/esslivedata#823) and of ADR 0008: observe, do not trust acknowledgements.
- **Backend restart.** Records are durable; queued requests are re-dispatched, running ones reconciled as above.
- **External cancellation** (cluster preemption) is detected by the same reconciliation.
- **Cancel of a running request** asks the launcher to stop it; dependents are cancelled.
- **Session loss.** A closed or crashed session drops its copies and its warm workflow.
  Runs in flight there fail and the client resubmits; anything else is recomputed from records on demand.
- **Failure surfacing** is in scope from the start: a user must see why a run failed without reading logs.
  Facilities that built automatic reduction report that the monitoring UI was most of the value.
- **Slow or missing shared filesystem.** Fetching inputs has a timeout and a distinct failure status.
- **Multi-tenancy.** The backend checks proposal access on every reference it resolves or serves, not only at submission.
  Cluster jobs run under the submitting user's account.

## Execution modes mapped onto the model

- **Manual**: submit one request, inspect outputs, resubmit with changed parameters.
- **Interactive**: manual inside a session (D2), with a warm workflow (D9) driven by plot selections in a slot (D11).
  Local mode only until remote sessions exist.
- **Batch**: template plus overrides (D7).
- **Automatic**: trigger loop plus template (D8).
- **Map, combine, chunking**: batch plus dependent combine (D7).
- **Publication**: explicit publish of an output (D12).

## Explicitly deferred

HTTP transport, real SciCat integration, cluster launcher, the data store's view implementation, spill policy, UI framework, metrics, agent-facing API.
Remote sessions: a session launcher, a per-session memory budget and idle timeout, and the data store's protocol between a session's memory tier and the registry.
A hint in the spec for which parameters are cheap to change in a session, so the UI can offer live feedback.
Provisional outputs of a running run, for progress display during chunk-wise processing.
A versioned collection record, appended to by the client and referenced by version, if lists of runs to accumulate grow well beyond a few thousand entries and resending them whole becomes a cost.
The memory budget itself is not deferred: every memory tier needs one from the start.

## Technology proposals

- pydantic for all data models (the spec already requires it).
- Standard-library sqlite3 for the record store; Postgres later.
- scipp HDF5 for stored scipp data; pluggable serializers for other output types.
- scitacean for SciCat access.
- FastAPI for the HTTP transport when it comes.
- No workflow engine, no Dask, no message broker in local mode.

## Open questions

Decisions the team needs to make; my recommendation in brackets.

- **Groups as the automatic-reduction unit.** A reflectivity curve needs four angle runs plus a reference.
  "On new dataset, instantiate template" cannot say "wait until the series is complete".
  [Trigger rules match groups, and inputs and overrides are list-valued.]
- **Data-dependent fan-out.** Grouping events by rotation angle inside one file yields a member list only after a planning step reads the file.
  [A planning run whose output is a list of requests, which the trigger loop or client then submits.
  Keep it out of the backend.]
- **Template sharing.** Both stages of a split workflow share most parameters.
  Facilities that tried template inheritance moved to version-controlled read-only templates with per-dataset substitution.
  [No inheritance. A template may be derived from another by copy, and the record keeps the origin.]
- **Remote sessions.** Where a session runs when interactive use moves to the shared web UI: on the backend host under a shared memory budget, or as an interactive cluster job with queue latency at session start.
  [Decide once local sessions exist; a cluster job if memory turns out to be the constraint.]
- **Name of the backend component.** It clashes with esslivedata's "backend services".
  [Keep it unless the two projects are documented together.]
- **Memory budget semantics.** Who decides to spill, and how a runner reports memory use, including variances.
- **Retention policy** for outputs and records in shared mode: how long each kind is kept before it may be dropped, with slot history the shortest and automatic-reduction records the longest.
- **SciCat push mechanism** for new datasets, if the deployment offers one.

## Next step

Review this document with the team before implementing.
Then a spike on the two decisions with the most hidden risk, D3 and D7: a memory-tiered data store, an atomic group submit with pending outputs, and a launcher that runs a chain in one session but a fan-out in several processes, exercised by a fake map-combine pair.
The two designated testing seams are the fake dataset source and the session launcher; no browser tests in the skeleton.
The full walking skeleton, all components in local mode with no HTTP and no UI, follows if the spike holds.

## Glossary

Where esslivedata uses a word differently, the clash is noted.

- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results. In esslivedata "backend services" are the Kafka worker processes; unrelated.
- **Batch**: many runs made from one template, each with small differences. In esslivedata a batch is a bundle of messages; unrelated.
- **Data reference**: a field type: a parameter or output declared to hold a reference to a file or an array rather than a literal. Easy to confuse with *reference*, which is the value such a field holds.
- **Data store**: where the bytes of large outputs live. One registry and disk tier owned by the backend, plus a memory tier in every process that holds data.
- **Dataset source**: where new datasets are discovered; each becomes a file record.
- **File record**: a record of the built-in `file` spec, standing for one SciCat dataset or upload; its single output is that file.
- **Input**: a parameter of data-reference type. In esslivedata inputs are data streams and genuinely differ from parameters; here they do not.
- **Intermediate**: a value inside a workflow, held by the warm workflow. No record, so no reference.
- **Launcher**: decides where a run executes and starts it there.
- **Local mode**: client, backend, launcher, session, and data store in one Python process. **Shared mode**: the backend as a service used by many people.
- **Map and combine**: split work across many runs, then merge their outputs in one run.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
- **Record store**: the database of records.
- **Reference**: a value, "output X of record Y", usable as any parameter whose type matches. The only way a request names data.
- **Run record**: a run request plus what happened to it. Called "run", never "job": in esslivedata a job is a running streaming workflow.
- **Run request**: everything needed to execute a workflow once.
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue. **PID**: SciCat's persistent identifier for a dataset.
- **Session**: a long-lived process belonging to one client, keeping the outputs of its runs and the workflow itself in memory. A cache over records.
- **Slot**: a series of runs in one session that supersede each other, with a current record and a history. The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs. Defined in scipp/ess#690.
- **Stage output**: an output of one spec that requests of another spec take as input. An ordinary output; the word names the role.
- **Template**: a saved, versioned run request with some fields left blank.
- **Trigger loop**: watches for completed records that match a rule and submits runs automatically.
- **Warm workflow**: the workflow object kept alive in a session between runs. One per spec.
- **Workflow**: the scientific code that turns input files into results. Typically a sciline pipeline; the framework does not care.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).

## Index of decisions

Numbering follows reading order. It is provisional until the wider review and stable after it; then add at the end.

| | Decision | Where |
|---|---|---|
| D1 | Every value is an output of a record; files are records too; stand-ins resolve at submission | Records and references |
| D2 | Requests are stateless; execution may be stateful inside a session | Choice 1 |
| D3 | Data is tiered; memory never crosses a process; one runner, pluggable launch | Choice 1 |
| D4 | Reuse across requests means a workflow boundary; no caching inside a workflow | Choice 1 |
| D5 | The record store is ours, small, and implementation-agnostic; the backend is its single writer | Choice 2 |
| D6 | Identical requests with identical package versions reuse outputs | Choice 2 |
| D7 | Map, combine, and chunking use one primitive: pending outputs as inputs; batch members are independent | Choice 2 |
| D8 | The dataset source is abstracted; not Kafka | Choice 2 |
| D9 | Framework-to-workflow contract: a callable from parameters to outputs | Choice 3 |
| D10 | The client interface is the API; HTTP later; notebook first | Choice 4 |
| D11 | Interactive plotting: views are not runs, plot selections are ordinary parameters, reruns live in slots | Choice 4 |
| D12 | Only finalized data enters SciCat | Ownership and publication |
| D13 | Instrument plus proposal scopes everything | Ownership and publication |
| D14 | One type vocabulary: inputs are data-reference parameters, outputs a typed model | Spec changes |

## Review log

This document was reviewed by six independent AI reviewers before being shown to the team, from these angles: architectural consistency, fit with real ess workflows, operations and failure modes, prior art at other facilities, plain-language readability, and lessons from esslivedata.
Their findings shaped the failure-handling section, the record fields for resolved values and package versions, the placement rules in D3, the serializer rule in D9, instrument-shared artefacts in D13, slots in D11, and the open questions.
The unification of inputs, parameters, and outputs in one vocabulary (D14) followed from the reviewers' observation that the spec had no input declarations and no types for non-array outputs.
A later pass on interactive use found that the file-based contract in D9 contradicted the in-memory chain in D3, and that stateless execution made interactive loops disk-bound in shared mode; the session model in D2, D3, and D9 is the result.

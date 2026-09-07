# Architecture sketch for ESS data-reduction applications

Living document.
Records the decisions reached so far, why they were made, and what they cost.
Companion to [scoping.md](scoping.md), which states goals and scope.
Nothing here is implemented yet.
Technology choices at the end are proposals.

## The picture in one paragraph

A user, a script, or an automatic trigger submits a *run request*: which workflow to run, with which parameters, on which input data.
The *backend* checks the request, writes it down as a *run record*, and asks a *launcher* to start a *runner* somewhere: in the same process, in a subprocess, or on the cluster.
The runner fetches the inputs, calls the scientific workflow code, and stores the results.
Any result of a run, whether a single number or a large array, can be an input of the next request; large results are stored separately and named by *handles*.
Interactive work happens in a *session*: a long-lived process that keeps the workflow and its data in memory between runs, while the records look the same as for any other run.
Everything the backend knows is in the records, so any result can be traced back to raw data and parameters, and any result can be recomputed if it was thrown away.
Batch reduction is many requests made from one *template*.
Automatic reduction is a loop that makes requests from a template whenever new data appears.
Publishing a result to the data catalogue is a separate, deliberate step.

## Terms

Plain-language definitions.
The esslivedata project has its own glossary that uses some of these words differently, see the notes.

Five of these terms are easy to confuse, so the distinctions first.
A *handle* is a value: the identity of one stored piece of data.
A *data reference* is a field type: a parameter or output declared to hold a handle.
An *output reference* is a second kind of value that any parameter field may hold instead of a literal: "output X of run Y", for data a run has produced or will produce.
It exists because small outputs are stored inline and have no handle, and because requests submitted together refer to each other before run IDs exist.
The *data store* is the backend's one durable home for data: it knows every handle's origin, owns the bytes of uploads and of results written to disk, and knows which copies exist.
A *data cache* holds copies in memory and serves views; there are several, one per session plus a shared one, and the data store says which one to ask.
Clients never talk to a cache; they ask the backend, which asks the cache.

- **Workflow**: the scientific code that turns input files into results.
  Typically a sciline pipeline, but the framework does not care.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs.
  Independent of how the workflow is implemented or where it runs.
  Defined in scipp/ess#690.
- **Handle**: a ticket naming one stored piece of data that is too large to keep inside a record, without saying where the data currently is.
- **Data reference**: a parameter or output type whose value is a handle.
  A parameter of this type is what we call an **input**; there is no separate input declaration (D15).
  In esslivedata inputs are data streams and genuinely differ from parameters; here they do not.
- **Output reference**: "output X of run Y", usable as the value of any parameter with a matching type (D16).
- **Run request**: everything needed to execute a workflow once.
- **Run record**: a run request plus what happened to it: status, times, who submitted it, output values or handles, software versions.
  Called "run", never "job", to avoid a clash with esslivedata, where a job is a running streaming workflow.
- **Template**: a saved, versioned run request with some fields left blank.
- **Batch**: many runs made from one template, each with small differences.
  In esslivedata a batch is a bundle of messages; unrelated.
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results.
  In esslivedata "backend services" are the Kafka worker processes; unrelated.
- **Launcher**: decides where a run executes and starts it there.
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **Data store**: the backend's durable home for data, whether a raw file known to SciCat, an upload, or a result: every handle's origin, the bytes on disk, and which copies exist where.
- **Data cache**: a process holding copies of data in memory under a budget and serving views.
  Every session has one; shared mode adds a long-lived shared cache.
- **Session**: a long-lived process belonging to one client, in which runs may be placed and which keeps their outputs, and the workflow itself, in memory.
  A cache over records: session identity never enters a record, and everything a session holds can be recomputed.
- **Record store**: the database of run records.
- **Dataset source**: where new datasets are discovered.
- **Trigger loop**: watches the dataset source and submits runs automatically.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
- **SciCat**: the facility's data catalogue. **PID**: SciCat's persistent identifier for a dataset.
- **Pending output**: an output of a run that has not finished yet, usable as input to another request.
- **Map and combine**: split work across many runs, then merge their outputs in one run.
- **Local mode**: client, backend, launcher, session, and data cache all inside one Python process: a notebook, or a local application.
  Contrast **shared mode**: the backend runs as a service used by many people.
- **Warm workflow**: the workflow object kept alive in a session between runs, so a rerun recomputes only what a changed parameter affects.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).

## Core model

Six kinds of data.
All are plain, JSON-serializable values, even when passed around inside one process.

- **Spec**: identity (`name`, `version`), parameter model, output model.
  Both models use the same type vocabulary.
  Inputs are parameters of data-reference type (D15); outputs are a typed model like parameters (D16).
- **Handle**: `(origin, id)` plus a lifecycle state: pending, available, evicted, failed.
  The origin is a run, SciCat, or an upload, and it determines the miss path.
  The state is independent of the run that produces it.
  A handle names a computation result, not particular bytes; see D5 for what pins the bytes.
  Small output values are stored inside the record and have no handle.
- **Run request**: spec identity, parameter values, instrument, proposal, submitter.
  Stateless and complete: sufficient to reproduce the outputs from scratch.
  Handles appear inside the parameter values, in the data-reference fields.
  Any field may instead hold an output reference to another request, including a pending one; the backend substitutes the value or handle when it becomes available.
- **Run record**: the request plus run ID, status, timestamps, output values (inline when small, handles when large), the resolved parameter values including defaults, software versions of the runner environment, and optionally batch ID, template version, and the record this one retries.
  Immutable once the run completes, except status.
  The request keeps output references in their reference form, so provenance is the graph you get by following references and handles to the records that produced them.
- **Template**: a stored, immutable, versioned partial run request.
  May originate from a version-controlled file (instrument defaults) or from a user saving a request.
- **Batch**: a set of independent runs from one template with per-member overrides, tagged with a batch ID.

## Components

- **Backend**: validates a request against the spec's JSON Schema, finds the data-reference fields by walking that schema, resolves their values to handles, creates the record, allocates handles for the outputs in state pending, and hands the request to a launcher.
  Single writer to the record store.
  Exactly one backend process per record store.
- **Client interface**: the backend's Python interface.
  This *is* the API.
  Every UI, notebook, or service reaches the backend only through it.
  HTTP is a later transport for the same interface, not a second API.
  Clients observe change by pulling with a version counter, never by callbacks carrying data.
  Views (D17) are part of it: a client never addresses a data cache.
  The backend forwards a view request to the cache holding a copy and returns the result, which is small by construction, so there is one endpoint in every mode.
- **Launcher**: pluggable: session, subprocess, cluster.
  Placement is relative to the session holding a run's inputs (D3).
  Publishes which specs its environment can run, so the backend can reject unrunnable requests at submission.
  May run a group of dependent requests in one runner process.
- **Runner**: materializes inputs, validates parameters with the real parameter class, calls the workflow, stores outputs, reports to the backend.
  In a session it keeps the workflow callable between runs (D6).
  Sends periodic liveness signals while running.
  Never touches the record store.
- **Session**: owns a runner and a data cache.
  Created and closed by a client; closing drops its copies.
  In local mode it is the client's own process.
  Initially sessions exist only in local mode (D1).
- **Data store**: one lookup and listing for every handle, regardless of origin.
  Each handle's origin is the record that produced it, a SciCat PID, or an upload.
  Records which data caches hold a copy of a handle and routes view requests there.
  Copies in memory or in a local download cache are evictable for every handle; what differs is the miss path: recompute from the record, re-download from SciCat, or nothing.
  Uploads are the only handles whose origin is the data store itself, so they are never evicted.
  A SciCat file on a mounted facility filesystem has no local copy; the mount is its disk tier.
  A result from last week and a raw file are the same thing to a request.
- **Data cache**: holds copies in memory under a budget and serves views (D17).
  Several of them: one in each session's process, and in shared mode a long-lived shared cache that loads outputs from disk on first access.
- **Record store**: create, read, update status, and two queries: records that consumed a handle, and the record that produced a handle.
  Carries a schema version.
- **Dataset source**: yields new datasets for a proposal as handle plus metadata.
  One real implementation (SciCat) and one fake for tests.
- **Trigger loop**: on a new dataset or group of datasets matching a rule, instantiate a template and submit.
- **Publisher**: writes the data behind a handle to SciCat together with its provenance.
  Idempotent: the resulting PID is recorded, and publishing the same handle again returns it.

## Decisions

Each entry gives the decision, why, and what it costs.
Numbering is stable; add at the end.

### D1 Requests are stateless; execution may be stateful inside a session

**Decision.** The stateless run request is the only unit the backend and the record store know.
Execution may be stateful: a run placed in a session may reuse the workflow object and the data that earlier runs in that session left in memory.
Two invariants keep this safe.
Session identity never appears in a record.
Everything a session holds can be recomputed from records, so a session is a cache, and losing it costs only time.
Initially sessions exist only in local mode; batch and automatic reduction run as a stateless service, and the shared web UI submits and inspects.

**Why.** Batch and automatic reduction need requests that can be made without a human present, and provenance needs requests that fully describe the result.
Both are properties of the record, not of the process that executes it.
Interactive work needs the opposite of stateless execution: sub-second reruns with multi-gigabyte intermediates, as in SANS, kept in memory.
esslivedata's experience with ephemeral identities that everything else had to compensate for (scipp/esslivedata#1042, ADR 0008) is why the session must stay invisible to the records, not a reason to avoid state.

**Cost.** Two lifetimes for the workflow object in the runner: once per run, or once per session.
Remote sessions need a memory budget per session, a cap on sessions, idle timeouts, and view routing to session processes.
That is why they are deferred, and why the shared web UI initially has no interactive loop.

### D2 Incremental recompute by splitting workflows, not by framework magic

**Decision.** Workflow authors split an expensive stage from a cheap, tweakable stage into separate specs when the consumer of the intermediate is not in the same session: batch, automatic reduction, and artefacts shared between runs such as processed vanadium.
The intermediate is an ordinary output, usable as input to the next stage.
Inside a session the split is unnecessary: a warm workflow recomputes only what a changed parameter affects (D6).

**Why.** Splitting is the only strategy that works on a fire-and-forget remote runner.
Provenance of intermediates is exact.
The UI can tell which stage is cheap, because it is a separate workflow.
Deriving the split automatically from a sciline graph is possible, as `ess.reduce.streaming.StreamProcessor` does for live data, but that is a tool on the workflow side; the framework does not know about it.

**Cost.** The author chooses where to cut, and the cut is not always clean.
For example, processed vanadium in diffraction is binned on the sample's edges, so the intermediate must be oversampled.
Both stages typically share parameters, see the template open question.
For an unsplit workflow in a session, the spec does not say which parameters are cheap to change, so the UI cannot choose a slider over a run button; see deferred.

### D3 Handles are opaque and data is tiered; memory never crosses a process

**Decision.** A handle never contains a path.
Data in memory lives in exactly one process; there is no shared memory across processes or machines.
A data cache holds copies of handles in memory under a budget and serves views (D17); every session has one, and shared mode adds a long-lived shared cache.
The data store records which caches hold a copy of a handle and routes view requests there.
A copy in memory or on local disk may be evicted for any handle whose origin is elsewhere.
Every run's outputs have at least one consumer: the client that submitted the run, which will plot them, chain them, or both.
Where a run executes, and where its outputs go, depends on the session holding its inputs:

- **In the session.** The run executes in the session process, reads its inputs from memory, and leaves its outputs there; nothing is written.
  This is local mode, and also a chain of dependent requests that the launcher places in one runner.
- **Outside any session.** The runner writes outputs to disk before reporting completion, because disk is the only way to reach another process.
  This covers shared mode, the subprocess launcher, and fan-out (map) members.
  The shared data cache loads an output into memory on first access: written once, loaded once, every further view served from memory.
- **No consumer left.** Once retention expires, the output is dropped.
  A later request or view recomputes it from its record.

The shared backend holds large data only in that bounded, evictable cache: retention is a policy, and a miss is served by recomputing from the record or re-downloading from SciCat.
Local mode is the degenerate deployment where client, session, and data cache are one process; the client interface is the same as in shared mode.

**Why.** Intermediates can be huge, and writing one to disk is wasted work when its only consumer is in the same process.
Sharing memory across machines would mean a distributed memory layer, and scipp objects are not chunk-aware, so such a layer would work badly and cost a lot.
Recompute is often cheaper than storage.
The cache view is also what resolved esslivedata's memory problems (scipp/esslivedata#1274).
Making the session's memory a data cache like any other, rather than a special case, is what lets a remote session be added later as one more launcher and one more cache, with views routed to it like to any other.

**Cost.** Placement is a launcher decision and must be explicit in its interface.
Whether a chained consumer exists is only known for requests submitted together as a group (D13).
Shared mode pays one disk write and one read per output and a process start per run; interactive loops there wait for remote sessions (D1).
The copy registry in the data store is one more thing to keep consistent, trivially so while there is one cache.

### D4 Only finalized data enters SciCat

**Decision.** Intermediates and unreviewed outputs stay in our store.
Publication is an explicit, idempotent operation on a handle, triggered by a user after inspection or by an automatic-reduction rule.
SciCat inputs are handles with origin `scicat` and the PID as ID, so raw files and our outputs are the same type to a workflow.

**Why.** Data in SciCat cannot be removed through the regular API.

### D5 The record store is ours, small, and implementation-agnostic

**Decision.** SQLite on local disk in local and single-backend mode, Postgres if the backend ever scales.
Records contain nothing sciline-specific.
A record stores the resolved parameter values (defaults filled in) and the versions of the workflow packages the runner used.
A request whose content matches a completed record with the same package versions may reuse that record's outputs instead of running.
No workflow engine.

**Why.** Engines (Aiida, Snakemake, Nextflow, Prefect) bake the dependency graph into code rather than records, and none gives a stateless request that a notebook, a trigger loop, and a batch UI can all emit.
Resolved values and package versions are what make "recompute from the record" true; without them a changed default or a package upgrade silently changes what a record means.
Reuse of identical requests is what makes a dragged slider or a batch rerun with mostly unchanged members affordable.
Schema versioning was left unresolved in esslivedata and needed hand-run migrations (scipp/esslivedata#915); a stored parameter set that no longer matches its spec version must fail loudly, never be silently defaulted.

**Cost.** We own dependency handling, failure propagation, and cancellation (D13, and the failure section).
That is a small scheduler.
Facilities that went this way for the remote case eventually added a message broker (ISIS, SNS, Diamond, ESRF).
"No broker" is therefore a local-mode decision to be re-examined when the cluster launcher is built.

### D6 Framework-to-workflow contract: a callable from parameters to outputs

**Decision.** Binding from spec identity to implementation via Python entry points.
The entry point returns a callable that takes the validated parameter model and returns the output model.
A stateless runner constructs it and calls it once.
A session runner constructs it once per spec version and calls it for every run, so the callable may keep state between calls; that is the warm workflow.
Data-reference fields are materialized by kind: raw NeXus and opaque files arrive as local paths; scipp arrays arrive as scipp objects, loaded by the runner from scipp HDF5 unless a copy is already in memory.
Outputs are returned as objects matching the output model; the runner validates them against it, stores vocabulary-typed small values inline, serializes scipp objects to scipp HDF5, and requires other types (CIF, ORSO) to come with their own serializer, declared with the output.
The framework never imports sciline.

A sciline workflow meets the contract through an adapter that diffs each call's parameters against the previous call and recomputes only what lies downstream of the change.
That adapter is `ess.reduce.streaming.StreamProcessor`, or a sibling sharing its machinery: parameters expected to change are its context keys, and list-valued parameters that accumulate, such as a growing list of runs to sum, are its dynamic keys, fed with the list's new elements.
Any other change, including removing a list element, resets the adapter.
Which parameters may change cheaply is declared on the workflow side, next to the adapter.

**Why.** Loading NeXus is workflow-specific (which detector banks, which monitors), so the framework cannot do it.
Scipp HDF5 is the one format the framework writes itself, so it can also read it; arrays arriving as objects is what lets a chain in a session stay in memory (D3) while workflow code looks the same in every mode.
One callable rather than a separate incremental protocol keeps D7's single execution path: the only difference between runners is whether the callable is kept.
Reuse inside the adapter is correct by construction from the sciline graph, which is stronger than the record-level reuse rule of D5.
The declaration of cheap parameters cannot be avoided, because caching every intermediate is not affordable with event data and the graph does not know compute cost.
Chunk-wise processing of one large file, as the NMX workflow does, happens inside the callable and is invisible to the framework.
Serialization of outputs is generic for scipp objects and must be pluggable because the outputs that get published are often not scipp objects.
Parameter validation authority lies with the process that owns the parameter class (ADR 0001 in scipp/ess#690), which is the runner; the backend validates against JSON Schema only.

**Cost.** Two validation points, with the runner's being the authoritative one.
The runner loads scipp arrays whole.
Accumulation over a list is exact only when nothing else changed; the adapter must reset otherwise, and nothing outside it can check that it does.

### D7 One runner, pluggable launch, backend as single writer

**Decision.** Remote execution is a different launcher, not a different execution model.
The runner reaches storage for data and the backend's API for everything else.
No database credentials on compute nodes.
Raw files that already sit on the facility filesystem are resolved to their path by the backend at dispatch; only otherwise does the runner download, using a short-lived token issued by the backend.
The backend takes a startup lock so a second instance cannot open the same record store.

**Why.** One code path for execution.
Single writer avoids the multi-client ownership problems that produced most of esslivedata's hard bugs (scipp/esslivedata#1285, #714, ADR 0007).

### D8 References resolve at submission, handles materialize at execution

**Decision.** Users submit references: local path, SciCat PID, or run number (per instrument).
The backend resolves them to handles before persisting anything.
The runner turns handles into local files at execution time.
Local files that must reach a remote runner are uploaded into the data store first, where they are recorded as uploads and therefore never evicted.

**Why.** Provenance must not depend on a search that could give a different answer later.
Uploaded files have no producing record and no catalogue entry, so evicting them would destroy data.

### D9 Instrument plus proposal scopes everything

**Decision.** Mandatory on every record.
Run-number resolution, UI navigation, templates, and authorization (SciCat membership) operate within a proposal.
Instrument scientists and commissioning use long-lived proposals.
Artefacts produced there and consumed by every user proposal (direct beam, beam centre, processed vanadium, masks, lookup tables) are marked instrument-shared and readable from any proposal on that instrument.

**Why.** It is the unit users think in and the unit SciCat authorizes by.
Without instrument-shared artefacts, every external user would need membership in the commissioning proposal.

### D10 Plot selections are ordinary parameters

**Decision.** Interactive tools bind to parameters of shared vocabulary types (range, rectangle, polygon).
Each selection change submits a new stateless run of the cheap stage.
A request may carry a *slot* key chosen by the client; a new request in the same slot cancels queued requests in that slot.
Runs in a slot are short-retention.
Slot runs are placed in the submitter's session (D3), which until remote sessions exist means local mode.

**Why.** No special interactive concept in the framework.
The slot key is the stable identity a plot needs across superseded runs; esslivedata needed the same split between a stable data key and a per-result key (scipp/esslivedata#1062).

**Cost.** Restricting interactive feedback to sessions, and initially to local mode, is acceptable if it keeps things simple.

### D11 The dataset source is abstracted

**Decision.** Interface: for a proposal, yield new datasets with metadata.
A SciCat implementation, polling or push as the deployment allows, and an in-memory fake.
Arrival may be out of order; the interface does not promise a monotonic cursor.
Not Kafka.

**Why.** Two implementations from day one.
Kafka would couple the backend to the streaming infrastructure and reduce data that is not yet catalogued.
Reliable command delivery over Kafka was a long struggle in esslivedata (scipp/esslivedata#856); a request row in a database is the durable desired state that struggle concluded was needed.

### D12 Batch members are independent

**Decision.** A batch is template plus dataset list plus per-member overrides, producing independent records.
No ordering between members.
Rerunning a member is a new record with the same batch ID.

**Why.** Anything else is chaining and is expressed with D13.

### D13 Map, combine, and chunking use one primitive: pending outputs as inputs

**Decision.** Map is a batch with a shared fixed input and a per-member parameter (file, or chunk range).
Combine is one request whose inputs are the map outputs, submitted before they exist.
A group of requests is submitted atomically and gets its IDs back; inside the group, requests refer to each other by local alias.
The backend holds a request until its pending inputs are available, fails it if any input fails, and cancels it if any input is cancelled.
Groups must be acyclic; a request waiting on an input that never arrives times out.
Merge strategy, memory during merge, and chunk semantics belong to the combine workflow.

**Why.** This is the smallest addition that covers temperature scans, angle series, mask series, and large-file chunking.
Real combines are not sums: reflectometry uses a variance-weighted overlap fit, SANS concatenates events.
So merging cannot be a framework concern.

**Cost.** See D5 on owning a small scheduler.
Fan-out whose size is only known after reading the file (grouping by rotation angle inside one file) is not covered; see open questions.

### D14 The client interface is the API; HTTP later; notebook first

**Decision.** Requests, records, templates, and handles are plain data even in local mode.
In local mode a client may also turn a handle into a scipp object directly, so plopp and the full scipp API work in a notebook; that is a convenience on top of views (D17), not a second mechanism.
No standalone local application initially; a notebook on the library is the local application.
When one is wanted, it is the same web UI hosted in the local process, next to backend, session, and data cache, against the in-process client interface; the shared deployment is the same UI over the HTTP transport.
The UI framework is chosen after the backend skeleton exists.
API-first does not imply TypeScript; a Python-driven web framework satisfies it if it only uses the client interface.
Qt is out.
UI state such as layouts and plot configuration lives in the record store, not in a second per-dashboard store.

**Why.** A UI that reaches into backend internals was the source of esslivedata's cross-session races (scipp/esslivedata#1046, #1098, ADR 0007).
Qt would be a third UI with its own testing story.
esslivedata's per-dashboard YAML config store was an anti-pattern (scipp/esslivedata#1076, #1070).

**Cost.** A local application in one process shares the interpreter between UI and runs, so a long run blocks the UI unless the session moves to a subprocess, which needs the remote-session machinery.

### D15 Inputs are parameters of data-reference type

**Decision.** The spec has one parameter model and no separate input section.
The parameter vocabulary gains a data-reference type: a handle, optionally constrained by kind (raw NeXus file, scipp array, opaque file) and, for arrays, by the same `ArraySpec` that outputs declare.
Lists of references are allowed; comparing or combining runs is a workflow with such a list as input.
A field may be a union of a literal and a reference, for values such as a beam centre that a user may type in or take from a previous run.

**Why.** Every difference between an input and a parameter, in this framework, is behaviour selected by the field's type: resolution of run numbers and PIDs, materialization to a file, provenance edges, validation timing, and which widget a UI shows.
None of it needs a second declaration system.
esslivedata separates the two because its inputs are streams routed at runtime; here every input is a value known at submission.
Sharing `ArraySpec` between outputs and reference constraints makes "outputs can fulfil inputs" a check between two values of the same type.

**Cost.** The backend must walk a JSON Schema, including nested models, to find reference fields.
This is a small extension to the vocabulary in scipp/ess#690.

### D16 Outputs are a typed model in the same vocabulary as parameters

**Decision.** A spec declares its outputs as a model class, mirroring parameters, with a JSON Schema in the serialized form.
An array output is a data-reference field constrained by `ArraySpec`.
A beam centre is a vector with unit, a fit result a float with unit, a CIF file a reference of kind "opaque file".
Title and description are field metadata.
Whether an output value is stored inline in the record or in the data store under a handle is a framework decision based on size and type, invisible in the spec.
A downstream parameter may take an output reference to any output field whose type matches.

**Why.** The spec in scipp/ess#690 allows non-array outputs but gives them no type, which breaks "outputs can be inputs" for exactly the values, such as beam centres and direct beams, that most often feed the next workflow.
With one vocabulary on both sides, chaining is a type check between two fields.
Handles stop being a spec concept and become a storage detail.

**Cost.** The output side of scipp/ess#690 changes shape while the PR is open.
Structural validation of an array output against its `ArraySpec` happens in the runner at completion, since pydantic cannot check a scipp object.

### D17 Views are not runs

**Decision.** A view is a request for a small piece of a handle's data for display: label-based slicing, reduction over dimensions (sum, mean), and downsampling to a display resolution.
Views are served by whichever data cache holds a copy (D3), are not recorded, and are never inputs to a run.
When a user wants to compute further from a slice they found interactively, the slice specification becomes a parameter of the next workflow (D10).
Overlaying several runs in one plot is several views; anything beyond slicing, reduction, and downsampling, including the difference of two runs, is a workflow.
Data larger than the memory budget is sliced by partial reads from disk, so dense arrays are stored chunked in a layout that supports that.
Event data cannot be sliced from disk and is loaded whole or histogrammed by a workflow first.

**Why.** Exploring a 4D volume by dragging through 2D slices must not create records, and the frontend must never receive the volume.
The slice-becomes-parameter rule keeps provenance exact without making views part of it.

**Cost.** A view vocabulary in the client interface, and a chunking decision at write time for dense data.
Through the client interface a user explores declared outputs only; a notebook can compute any node of a sciline workflow.

## Failure handling

Kept out of the decisions above so it can be read as one piece.

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
- **Multi-tenancy.** The backend checks proposal access on every handle dereference, not only at submission.
  Cluster jobs run under the submitting user's account.

## Execution modes mapped onto the model

- **Manual**: submit one request, inspect outputs, resubmit with changed parameters.
- **Interactive**: manual inside a session (D1), with a warm workflow (D6) driven by plot selections in a slot (D10).
  Local mode only until remote sessions exist.
- **Batch**: template plus overrides (D12).
- **Automatic**: trigger loop plus template (D11).
- **Map, combine, chunking**: batch plus dependent combine (D13).
- **Publication**: explicit publish of a handle (D4).

## Explicitly deferred

HTTP transport, real SciCat integration, cluster launcher, the data cache's view implementation, spill policy, UI framework, metrics, agent-facing API.
Remote sessions: a session launcher, a per-session memory budget and idle timeout, and view routing to session processes.
A hint in the spec for which parameters are cheap to change in a session, so the UI can offer live feedback.
Provisional outputs of a running run, for progress display during chunk-wise processing.
The memory budget itself is not deferred: every data cache needs one from the start.

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
- **Retention policy** for derived data in shared mode.
- **SciCat push mechanism** for new datasets, if the deployment offers one.

## Next step

Review this document with the team before implementing.
Then a spike on the two decisions with the most hidden risk, D3 and D13: a data store with copy routing and one local data cache, an atomic group submit with pending outputs, and a launcher that runs a chain in one session but a fan-out in several processes, exercised by a fake map-combine pair.
The two designated testing seams are the fake dataset source and the session launcher; no browser tests in the skeleton.
The full walking skeleton, all components in local mode with no HTTP and no UI, follows if the spike holds.

## Review log

This document was reviewed by six independent AI reviewers before being shown to the team, from these angles: architectural consistency, fit with real ess workflows, operations and failure modes, prior art at other facilities, plain-language readability, and lessons from esslivedata.
Their findings shaped the failure-handling section, the record fields for resolved values and package versions, handle lifecycle states, the memory-versus-fan-out rule in D3, the serializer rule in D6, instrument-shared artefacts in D9, slots in D10, and the open questions.
The unification of inputs and parameters in D15, and of outputs with the same vocabulary in D16, followed from the reviewers' observation that the spec had no input declarations and no types for non-array outputs.
A later pass on interactive use found that the file-based contract in D6 contradicted the in-memory chain in D3, and that stateless execution made interactive loops disk-bound in shared mode; the session model in D1, D3, and D6 is the result.

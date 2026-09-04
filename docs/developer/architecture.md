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
Results are named by *handles*, and a handle can be the input of the next request.
Everything the backend knows is in the records, so any result can be traced back to raw data and parameters, and any result can be recomputed if it was thrown away.
Batch reduction is many requests made from one *template*.
Automatic reduction is a loop that makes requests from a template whenever new data appears.
Publishing a result to the data catalogue is a separate, deliberate step.

## Terms

Plain-language definitions.
The esslivedata project has its own glossary that uses some of these words differently, see the notes.

- **Workflow**: the scientific code that turns input files into results.
  Typically a sciline pipeline, but the framework does not care.
- **Spec**: the declared interface of a workflow: name, version, parameters, inputs, outputs.
  Independent of how the workflow is implemented or where it runs.
  Defined in scipp/ess#690.
- **Handle**: a ticket naming one piece of data, without saying where the data currently is.
- **Run request**: everything needed to execute a workflow once.
- **Run record**: a run request plus what happened to it: status, times, who submitted it, output handles, software versions.
  Called "run", never "job", to avoid a clash with esslivedata, where a job is a running streaming workflow.
- **Template**: a saved, versioned run request with some fields left blank.
- **Batch**: many runs made from one template, each with small differences.
  In esslivedata a batch is a bundle of messages; unrelated.
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results.
  In esslivedata "backend services" are the Kafka worker processes; unrelated.
- **Launcher**: decides where a run executes and starts it there.
- **Runner**: the process that executes one run.
- **Output store**: where results live, in memory or on disk, looked up by handle.
- **Record store**: the database of run records.
- **Dataset source**: where new datasets are discovered.
- **Trigger loop**: watches the dataset source and submits runs automatically.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
- **SciCat**: the facility's data catalogue. **PID**: SciCat's persistent identifier for a dataset.
- **Pending output**: an output of a run that has not finished yet, usable as input to another request.
- **Map and combine**: split work across many runs, then merge their outputs in one run.
- **In-process mode**: backend, launcher, and runner all inside one Python process, for notebooks.
  Contrast **shared mode**: the backend runs as a service used by many people.
- **Warm instance**: a workflow kept loaded in memory so repeated runs with changed parameters are fast.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).

## Core model

Six kinds of data.
All are plain, JSON-serializable values, even when passed around inside one process.

- **Spec**: identity (`name`, `version`), parameter model, declared outputs.
  Declared inputs are not in scipp/ess#690 yet; see open questions.
- **Handle**: `(store, id)` plus a lifecycle state: pending, available, evicted, failed.
  The state is independent of the run that produces it.
  A handle names a computation result, not particular bytes; see D5 for what pins the bytes.
- **Run request**: spec identity, parameter values, input handles, instrument, proposal, submitter.
  Stateless and complete: sufficient to reproduce the outputs from scratch.
  An input may be a pending output of another request.
  A parameter value may be a handle, since some workflows produce values that are parameters of the next workflow (beam centre, direct beam, reference measurement).
- **Run record**: the request plus run ID, status, timestamps, output handles, the resolved parameter values including defaults, software versions of the runner environment, and optionally batch ID, template version, and the record this one retries.
  Immutable once the run completes, except status.
  Provenance is the graph you get by following handles to the records that produced them.
- **Template**: a stored, immutable, versioned partial run request.
  May originate from a version-controlled file (instrument defaults) or from a user saving a request.
- **Batch**: a set of independent runs from one template with per-member overrides, tagged with a batch ID.

## Components

- **Backend**: validates a request against the spec's JSON Schema, resolves references to handles, creates the record, allocates output handles in state pending, and hands the request to a launcher.
  Single writer to the record store.
  Exactly one backend process per record store.
- **Client interface**: the backend's Python interface.
  This *is* the API.
  Every UI, notebook, or service reaches the backend only through it.
  HTTP is a later transport for the same interface, not a second API.
  Clients observe change by pulling with a version counter, never by callbacks carrying data.
- **Launcher**: pluggable: in-process, subprocess, cluster.
  Publishes which specs its environment can run, so the backend can reject unrunnable requests at submission.
  May run a group of dependent requests in one runner process.
- **Runner**: fetches inputs to local files, validates parameters with the real parameter class, calls the workflow, stores outputs, reports to the backend.
  Sends periodic liveness signals while running.
  Never touches the record store.
- **Output store**: tiered, memory and disk, looked up by handle.
  Behaves like a cache for derived data: an evicted handle can be recomputed from its record.
  Files a user uploaded are not derived and are never evicted.
- **Record store**: create, read, update status, and two queries: by input handle and by output handle.
  Carries a schema version.
- **Dataset source**: yields new datasets for a proposal as handle plus metadata.
  One real implementation (SciCat) and one fake for tests.
- **Trigger loop**: on a new dataset or group of datasets matching a rule, instantiate a template and submit.
- **Publisher**: writes an output handle to SciCat together with its provenance.
  Idempotent: the resulting PID is recorded, and publishing the same handle again returns it.

## Decisions

Each entry gives the decision, why, and what it costs.
Numbering is stable; add at the end.

### D1 Runs are stateless; warm instances are a local optimization

**Decision.** The stateless run request is the only unit the backend knows.
A shared backend never keeps a live workflow between runs.
A runner co-located with the user (notebook, per-user local service) may keep a warm instance and serve reruns incrementally, invisible to the record store.

**Why.** Batch and automatic reduction need requests that can be made without a human present.
Provenance needs requests that fully describe the result.
Live workflow instances on a shared machine have lifetime, memory, and ownership problems.
esslivedata's experience with ephemeral identities that everything else had to compensate for is the cautionary tale (scipp/esslivedata#1042, ADR 0008).

**Cost.** Interactive loops on remote runners pay full cost per iteration unless the workflow is split (D2).
For multi-gigabyte event-level intermediates, as in SANS, that can be too slow, see open questions.

### D2 Incremental recompute by splitting workflows, not by framework magic

**Decision.** Workflow authors split an expensive stage from a cheap, tweakable stage into separate specs.
The intermediate is an ordinary output, usable as input to the next stage.

**Why.** It is the only strategy that works on a fire-and-forget remote runner.
Provenance of intermediates is exact.
The UI can tell which stage is cheap, because it is a separate workflow.
Deriving the split automatically from a sciline graph is possible, as `ess.reduce.streaming.StreamProcessor` does for live data, but that is a tool on the workflow side that emits specs; the framework does not know about it.

**Cost.** The author chooses where to cut, and the cut is not always clean.
For example, processed vanadium in diffraction is binned on the sample's edges, so the intermediate must be oversampled.
Both stages typically share parameters, see the template open question.

### D3 Handles are opaque and data is tiered; memory never crosses a process

**Decision.** A handle never contains a path.
The output store has a memory tier and a disk tier.
Data in memory lives in exactly one runner process.
Data that another process will consume is written to disk before the producing run is reported complete.
A launcher may run a chain of dependent requests in one process so the intermediate stays in memory.
Fan-out (map) runs execute in separate processes in shared mode, so their outputs are on disk by construction.
The shared backend keeps no large long-lived data: retention is a policy, and recomputing from the record is what happens on a miss.

**Why.** Intermediates can be huge; forcing every one to disk is wasteful, and recompute is often cheaper than storage.
Sharing memory across machines would mean a distributed memory layer, and scipp objects are not chunk-aware, so such a layer would work badly and cost a lot.
The cache view is also what resolved esslivedata's memory problems (scipp/esslivedata#1274).

**Cost.** Placement is a launcher decision and must be explicit in its interface.
In-process mode, the "backend" is the runner too, so the no-large-data rule applies to shared mode only.

### D4 Only finalized data enters SciCat

**Decision.** Intermediates and unreviewed outputs stay in our store.
Publication is an explicit, idempotent operation on a handle, triggered by a user after inspection or by an automatic-reduction rule.
SciCat inputs are handles with store `scicat` and the PID as ID, so raw files and our outputs are the same type to a workflow.

**Why.** Data in SciCat cannot be removed through the regular API.

### D5 The record store is ours, small, and implementation-agnostic

**Decision.** SQLite on local disk in in-process and single-backend mode, Postgres if the backend ever scales.
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
"No broker" is therefore an in-process-mode decision to be re-examined when the cluster launcher is built.

### D6 Framework-to-workflow contract: files in, objects out

**Decision.** Binding from spec identity to implementation via Python entry points.
Inputs arrive as local file paths.
Parameters arrive as the validated pydantic model.
Outputs are returned as objects; the runner serializes scipp objects to scipp HDF5, and other types (CIF, ORSO, plain JSON) must come with their own serializer, declared with the output.
The framework never imports sciline.

**Why.** Loading NeXus is workflow-specific (which detector banks, which monitors), so the framework cannot do it.
Serialization of outputs is generic for scipp objects and must be pluggable because the outputs that get published are often not scipp objects.
Parameter validation authority lies with the process that owns the parameter class (ADR 0001 in scipp/ess#690), which is the runner; the backend validates against JSON Schema only.

**Cost.** Two validation points, with the runner's being the authoritative one.

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
Local files that must reach a remote runner are uploaded into the output store first and marked as user-provided, which exempts them from eviction.

**Why.** Provenance must not depend on a search that could give a different answer later.
Uploaded files have no producing record, so evicting them would destroy data.

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
Interactive stages default to a local launcher.

**Why.** No special interactive concept in the framework.
The slot key is the stable identity a plot needs across superseded runs; esslivedata needed the same split between a stable data key and a per-result key (scipp/esslivedata#1062).

**Cost.** Restricting interactive feedback to local launchers is acceptable if it keeps things simple.

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

**Decision.** Requests, records, templates, and handles are plain data even in-process.
In-process mode may turn a handle into a scipp object directly, so plopp works with no slicing service.
That is the one legitimate difference between in-process and shared mode.
No standalone local application initially; a notebook on the library is the local application.
The shared web UI is built once against the HTTP transport.
Its framework is chosen after the backend skeleton exists.
API-first does not imply TypeScript; a Python-driven web framework satisfies it if it only uses the client interface.
Qt is out.
UI state such as layouts and plot configuration lives in the record store, not in a second per-dashboard store.

**Why.** A UI that reaches into backend internals was the source of esslivedata's cross-session races (scipp/esslivedata#1046, #1098, ADR 0007).
Qt would be a third UI with its own testing story.
esslivedata's per-dashboard YAML config store was an anti-pattern (scipp/esslivedata#1076, #1070).

## Failure handling

Kept out of the decisions above so it can be read as one piece.

- **Status state machine.** submitted, waiting (pending inputs), dispatched (launcher job ID recorded), running, completed, failed, cancelled.
  Retry is a new record pointing at the old one, never a status reset.
- **Runner liveness.** The runner sends periodic signals; the backend marks a silent run failed after a timeout and reconciles dispatched runs against the launcher (cluster queue, process liveness) on startup.
  This is the lesson of esslivedata's stuck "active" jobs (scipp/esslivedata#823) and of ADR 0008: observe, do not trust acknowledgements.
- **Backend restart.** Records are durable; queued requests are re-dispatched, running ones reconciled as above.
- **External cancellation** (cluster preemption) is detected by the same reconciliation.
- **Cancel of a running request** asks the launcher to stop it; dependents are cancelled.
- **Failure surfacing** is in scope from the start: a user must see why a run failed without reading logs.
  Facilities that built automatic reduction report that the monitoring UI was most of the value.
- **Slow or missing shared filesystem.** Fetching inputs has a timeout and a distinct failure status.
- **Multi-tenancy.** The backend checks proposal access on every handle dereference, not only at submission.
  Cluster jobs run under the submitting user's account.

## Execution modes mapped onto the model

- **Manual**: submit one request, inspect outputs, resubmit with changed parameters.
- **Interactive**: manual with a cheap chained stage (D2) driven by plot selections in a slot (D10).
- **Batch**: template plus overrides (D12).
- **Automatic**: trigger loop plus template (D11).
- **Map, combine, chunking**: batch plus dependent combine (D13).
- **Publication**: explicit publish of a handle (D4).

## Explicitly deferred

HTTP transport, real SciCat integration, cluster launcher, slicing service for remote UIs, memory budget and spill policy, warm instances, UI framework, metrics, agent-facing API.

## Technology proposals

- pydantic for all data models (the spec already requires it).
- Standard-library sqlite3 for the record store; Postgres later.
- scipp HDF5 for stored scipp data; pluggable serializers for other output types.
- scitacean for SciCat access.
- FastAPI for the HTTP transport when it comes.
- No workflow engine, no Dask, no message broker in in-process mode.

## Open questions

Decisions the team needs to make; my recommendation in brackets.

- **Spec input declarations.** The spec in scipp/ess#690 has a parameter model and outputs but no declared inputs.
  Chaining and handle-valued parameters need inputs to be named roles with types.
  esslivedata's auxiliary-source model is the shape to port.
  [Extend the spec; this is a follow-up to scipp/ess#690.]
- **Groups as the automatic-reduction unit.** A reflectivity curve needs four angle runs plus a reference.
  "On new dataset, instantiate template" cannot say "wait until the series is complete".
  [Trigger rules match groups, and inputs and overrides are list-valued.]
- **Data-dependent fan-out.** Grouping events by rotation angle inside one file yields a member list only after a planning step reads the file.
  [A planning run whose output is a list of requests, which the trigger loop or client then submits.
  Keep it out of the backend.]
- **Template sharing.** Both stages of a split workflow share most parameters.
  Facilities that tried template inheritance moved to version-controlled read-only templates with per-dataset substitution.
  [No inheritance. A template may be derived from another by copy, and the record keeps the origin.]
- **Warm instances for large event intermediates.** SANS keeps a multi-gigabyte event-level intermediate so rebinning is cheap.
  On a remote runner every selection reruns from disk.
  [Keep interactive stages on local launchers; revisit warm instances if that proves insufficient.]
- **Name of the backend component.** It clashes with esslivedata's "backend services".
  [Keep it unless the two projects are documented together.]
- **Memory budget semantics.** Who decides to spill, and how a runner reports memory use, including variances.
- **Retention policy** for the output store in shared mode.
- **SciCat push mechanism** for new datasets, if the deployment offers one.

## Next step

Review this document with the team before implementing.
Then a spike on the two decisions with the most hidden risk, D3 and D13: a memory-tiered store, an atomic group submit with pending outputs, and a launcher that runs a chain in one process but a fan-out in several, exercised by a fake map-combine pair.
The two designated testing seams are the fake dataset source and the in-process launcher; no browser tests in the skeleton.
The full walking skeleton, all components in-process with no HTTP and no UI, follows if the spike holds.

## Review log

This document was reviewed by six independent AI reviewers before being shown to the team, from these angles: architectural consistency, fit with real ess workflows, operations and failure modes, prior art at other facilities, plain-language readability, and lessons from esslivedata.
Their findings shaped the failure-handling section, the record fields for resolved values and package versions, handle lifecycle states, the memory-versus-fan-out rule in D3, the serializer rule in D6, instrument-shared artefacts in D9, slots in D10, and the open questions.

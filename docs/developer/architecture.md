# Architecture sketch for ESS data-reduction applications

Living document.
Records the choices made so far, the alternatives that were on the table, why each choice was made, and what it costs.
Companion to [scoping.md](scoping.md), which states goals and scope.
A walking skeleton of it exists under `packages/essapps`; see "Next step".
Technology choices at the end are proposals.
A reading edition with diagrams is [architecture.html](architecture.html); its wording follows this file.

## How to read this

The design rests on one modelling idea and four choices.
The idea comes first, under "Records and references".
The four choices follow, each with the options that were rejected, so that the team can disagree with a choice rather than only with its consequences.
Everything after that is consequence: the rules that fall out, the changes the workflow spec needs, the components, and how failures are handled.
Decisions carry stable identifiers D1 to D13 so discussion can point at them; they are indexed at the end, next to the glossary.
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

A **run record** is the request plus what happened to it: run ID, status, timestamps, output values, the resolved parameter values including defaults, the package versions and the environment of the runner, how the spec was bound to code, and whether the workflow object was reused from an earlier run.
Optionally it carries a batch ID, a template version, and one link to the record it derives from, with the reason: retry, recompute, or copy.
A failed run carries a structured failure reason, so that a user sees why without reading logs.
Resolved values and package versions are what make "recompute from the record" true; without them a changed default or a package upgrade silently changes what a record means.
The environment is recorded as an opaque name and revision, at ESS a conda environment, so that a recompute can be checked against it; the framework does no more with it than record and compare.
A record is immutable once the run completes, except for status, and is never deleted on its own.
Small output values are stored in the record; large ones are held by the data store, and the record says only that they exist.
Which of the two applies is decided by type and is invisible to clients.

A **reference** is a value, "output X of record Y", that a parameter field of matching type may hold instead of a literal.
It may also name one element of a collection output, "output X of record Y, key k".
It is the only way a request names data.
A reference never contains a path, and the data store's internal keys never appear in a request.
A reference may name a **pending output**, one whose record has not completed yet; the backend holds the request until it does.
The record keeps references in reference form, so **provenance**, the chain from any result back to raw data, parameters, and software, is the graph you get by following them.

**Files are records too.**
A SciCat dataset or a file on a user's disk is a **file record**: a record of one built-in spec, `file`, which has no workflow and one output, the file.
A file record has an origin, which is its identity for provenance, and one or more locations, which are where its bytes can be opened.
The origin is a PID, or a local path with the file's checksum; a location is a path on a named filesystem, such as the facility mount or a user's machine, or a copy in the data store.
A file record for a catalogue file is created when a request first references it, never by discovery, and holds only what resolution and authorization need: the PID, its proposal and instrument; the PID is the unique key.
Where its bytes are is asked of SciCat at dispatch and cached at most, because SciCat moves files to archive and back and edits metadata while our records are immutable; a location the store did not write is SciCat's answer or the user's path, never a copy of the catalogue.
Listing a folder in a local application creates one file record per file, a row each and no bytes moved; the checksum is taken the first time a runner reads the file and stored on the record then.
A raw file from the catalogue and a result from last week are therefore the same thing to a request, and a new raw file and a completed processing stage are the same kind of event to automatic reduction.

**Stand-ins resolve at submission.**
Users submit a local path, a PID, or a run number, which is unique within an instrument and proposal.
The backend turns each into a reference before persisting anything, because provenance must not depend on a search that could give a different answer later: a run number is looked up in SciCat; a PID whose entry carries our provenance snapshot resolves to the run record named there while the store still has it, and any other PID to a file record, created if it has none yet.
A path under the facility filesystem resolves to the PID of the dataset that owns it; any other path becomes a file record with that path as its origin.
Nothing is downloaded or copied at submission, and SciCat is not needed again once a reference exists.

**Whether an output is usable is two questions**: the record's status, and whether the data store holds a copy.
A missing copy is reported as such, never silently recomputed.
Getting it back is an explicit operation, **recompute**, which submits the record's request again and yields a new record linked to the old one; published outputs and catalogue files are instead downloaded again.
Recompute is exact only in the environment the record names, and refuses to run elsewhere unless the client overrides.

| Output of | Where the truth lives | On a missing copy | Copies evictable |
|---|---|---|---|
| A run record | The record: parameters, references, versions | Recompute, explicitly | Yes |
| A file record from SciCat | The PID; SciCat says where the bytes are | Download again | Yes |
| A file record from a local path | The path and checksum | Nothing to recover from, unless a store copy was made | A store copy, only by an explicit drop |
| A published run record | The PID, whose entry carries the provenance snapshot | Download rather than recompute | Yes |

Two more kinds of data complete the model.
A **template** is a stored, immutable, versioned partial run request, from a version-controlled file such as instrument defaults, or from a user saving a request.
Saving a request makes a template with its data-reference fields blank and every other field literal; the user may blank more.
A template moves to a new spec version by copy, and a batch rerun under the copy is a new batch whose records link to the old ones.
A **batch** is a set of independent runs from one template with per-member overrides, tagged with a batch ID and a member key chosen by the submitter, such as the run number or the temperature, so that a batch of two thousand is listed and labelled by something meaningful.

**Why this is the foundation.**
One addressing scheme serves inputs, views, publication, and provenance, and the scheduler has one kind of dependency.
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
Whether the workflow reuses unchanged intermediates or folds a new list element into an accumulator is a difference the workflow discovers by comparing the two parameter sets (choice 3), not a kind of request.
The list is resent whole on every rerun; it holds references, not data, so at the few thousand entries expected it stays under a megabyte.

**The data store follows from the choice (D3).**
It is one component owned by the backend: a registry and a disk tier.
Every process that holds data, a session or the shared service, also keeps a private in-memory cache of outputs.
The registry knows disk copies only.
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
The shared service's cache has a byte budget and evicts least recently used first, with outputs superseded in a slot (choice 4) before anything else.
Records live as long as their proposal plus an analysis window set by the facility: long enough to find last week's result and to chain to yesterday's vanadium, and no longer, because what is worth keeping longer was published, and a published entry carries its own provenance (D11).
A proposal's records and disk copies are dropped together, exported as one JSON bundle first; nothing is deleted one record at a time.
This is safe because references never cross proposals except into instrument-shared artefacts, whose commissioning proposals are long-lived, so no reference can point into a dropped proposal.
Within a proposal, disk copies have a retention policy per kind of run, an open question; when it expires the bytes are dropped and the record stays.
A dropped copy is a missing copy, under the rule in "Records and references".
Store copies of local files are exempt from that policy: the framework cannot bring them back, so such a copy is dropped only by an explicit operation on the file record or with its proposal, after which every record that reaches it through references is no longer recomputable.
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
The backend resolves references to locations at dispatch: a file record's path, asked of SciCat for a catalogue file, is handed to the runner as is, anything else the runner fetches from the data store.

**One scheduling primitive: pending outputs as inputs (D6).**
A group of requests is submitted atomically and gets its IDs back; inside the group, requests refer to each other's outputs before they exist.
The backend holds a request until every record it references has completed, fails it if any of them fails, and cancels it if any is cancelled.
A request whose reference names a collection element that the completed producer does not have fails with a missing-key status; the producer is unaffected.
Groups must be acyclic; a waiting request has no timeout, because every record it waits on reaches a terminal state through its own failure handling.
Map and combine are one use of this: a batch of members plus one request whose inputs reference their outputs.
They are optional: a spec may instead take a collection of runs and iterate inside, as reflectometry does when it scales overlapping angle curves against each other and writes the scaled curves back per angle.
A combine's output may itself be a collection keyed by member.
Merge strategy and memory during a merge belong to the combine workflow; real combines are not sums.
Batch members are independent: no ordering between them, and rerunning a member is a new record with the same batch ID; anything else is chaining.
A batch is validated whole before any record is created, and cancelled whole by its batch ID, queued and running members alike.

**The dataset source is abstracted (D7).**
Its interface: for a proposal, yield new datasets as PID plus the metadata a trigger rule can match on.
It persists nothing: a dataset becomes a file record only when a rule fires and the submission it makes references the PID, like any other stand-in, and a rule that waits for a group keeps the datasets it has seen as its own state, rebuilt from SciCat after a restart.
A SciCat implementation, polling or push as the deployment allows, and an in-memory fake.
Arrival may be out of order and repeated; the interface does not promise a monotonic cursor.

**Why.**
No engine gives the stateless request the whole design rests on.
Single writer avoids the multi-client ownership problems that produced most of esslivedata's hard bugs (scipp/esslivedata#1285, #714, ADR 0007), and one runner gives one code path for execution.
Pending outputs as inputs is the smallest addition that covers a vanadium stage feeding a sample reduction submitted together, temperature scans, angle series, and automatic reduction over a group of runs.
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
The spec therefore declares which parameters are cheap to change, and the wrapper caches the nodes just upstream of them; that declaration is also what lets a UI offer a slider rather than a run button.
`ess.reduce.streaming.StreamProcessor` is the special case for a list-valued parameter that only grows, such as runs to sum: its accumulators are fed the new elements, and any other change, including removing an element, resets it.
The framework provides a test helper that drives a callable through a sequence of parameter sets warm and cold and asserts equal outputs; that is the one check on the wrapper's reuse rules, and every workflow with a warm form runs it.

**Validation** has three layers.
Shape: JSON Schema, in the backend, always.
Parameters: the pydantic model, which catches cross-field rules the schema cannot express.
The backend runs it too, by importing the spec module alone, which esslivedata keeps separate from the workflow factory precisely so that specs can be validated without importing workflow code; scipp/ess#690 must keep that separation, and the backend never imports a factory.
Runnability: every reference resolves to a record the user may read and, for a collection element, to a key the producer declares; files exist where the launcher would look; the launcher's environment has the spec.
Anything past that, such as a file that opens but lacks a monitor, is a run that fails fast, not validation.
The runner remains authoritative for parameters (ADR 0001 in scipp/ess#690); a disagreement with the backend's check is a deployment bug, and the backend refuses a spec it cannot import unless the request opts out.

**Why.**
Arrays arriving as objects is what lets a chain in a session stay in memory while workflow code looks the same in every mode.
One callable rather than a separate incremental protocol keeps one execution path: the only difference between runners is whether the callable is kept.
Reuse inside the wrapper is correct by construction from the sciline graph, given the declared cheap parameters; the declaration cannot be avoided, because caching every intermediate is not affordable and the graph does not know compute cost.
Serialization of outputs must be pluggable because the outputs that get published are often not scipp objects.

**Cost.**
Two validation points, with the runner's being the authoritative one.
The runner loads scipp arrays whole.
Reuse is exact only if the wrapper's rules are right; the test helper is the check, and the record's "workflow object reused" flag is what lets publication insist on a cold result (D11).
Accumulation over a growing list is exact only for quantities that combine element by element; anything that depends on the whole list, such as normalisation by summed monitor counts, must be an accumulator on the right node, which is the workflow author's job.
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
A **slot** is a label on a request; runs with the same label supersede each other: a slider moving a threshold, a selection being dragged, or a list of runs to sum growing by one.
The client interface supplies the label whenever a request reruns a warm workflow, so a notebook user gets a slot without asking for one; comparing two variants side by side is two labels, the second assigned when the user forks, and discarding a variant drops its label from the UI and nothing else.
The backend offers one query, the latest submitted record with a label, and treats outputs of superseded records as the first to evict.
Cancelling a slot's queued predecessors is one client call.
Tools list, replay, and inspect by slot; inspection shows the latest record with its diff against the previous one, which is "one more file" for an accumulation series and "one value changed" for a slider.
Slot runs execute in the submitter's session, which until remote sessions exist means local mode.

**Why.**
One API keeps the UI out of backend internals.
Exploring a 4D volume by dragging through 2D slices must not create records, and the frontend must never receive the volume; the slice-becomes-parameter rule keeps provenance exact without making views part of it.
The slot is the one interactive concept in the framework, and it is one field on the request: the stable identity that a plot, a record browser, and a replay tool all need across superseded runs, the same split between a stable data key and a per-result key that esslivedata needed (scipp/esslivedata#1062).
Without it a session's reruns are hundreds of complete, near-identical records, which is the right model for provenance and the wrong thing to show a person.

**Cost.**
A local application in one process shares the interpreter between UI and runs, so a long run blocks the UI unless the session moves to a subprocess, which needs the remote-session machinery.
A view vocabulary in the client interface, and a chunking decision at write time for dense data so that views on data larger than a cache can read partially from disk.
Through the client interface a user explores declared outputs only; a notebook can compute any node of a sciline workflow.
Interactive feedback is restricted to sessions, and initially to local mode.
Every workflow that ends in events carries a dense twin, one histogram call in the callable.

## Ownership, publication, and deployment

**The record store is not a catalogue (also D11).**
Our store answers what was computed; SciCat answers what was measured and what was finalized.
A record holds nothing from SciCat that it did not need to make a decision, so a UI that wants a sample name or the list of a proposal's runs asks SciCat, and a batch member key such as a temperature is supplied by the submitter, not looked up.
SciCat is needed at two moments, resolving a stand-in and publishing; a resolved reference never needs it again, so work on data already referenced continues when the catalogue is slow.
Local mode is the one place where our store is the only catalogue, and there it is a row per file in a folder.

**Only finalized data enters SciCat (D11).**
Stage outputs and unreviewed outputs stay in our store, because data in SciCat cannot be removed through the regular API.
Publication is an explicit, idempotent operation on an output, triggered by a user after inspection or by an automatic-reduction rule.
The SciCat entry carries a self-contained provenance snapshot: the raw PIDs the output derives from, the resolved parameters, the spec identity, the package versions and environment; it can be read without any service of ours, and our record is then a copy of it.
A publication may name the PID it supersedes, which the snapshot records, since an entry in SciCat is never removed.
It reads a disk copy: an output that exists only in a session is first written out, and a record whose workflow object was reused is first recomputed in a throwaway process, so that what enters SciCat was computed cold and the record describes it exactly.
The backend records the intent to publish before writing to SciCat and the PID after; the output then has a second durable copy, so a miss on it becomes a download rather than a recompute.
The trigger loop recognizes a published output by the snapshot in its SciCat entry, not by a table of ours, and never fires on it or on records made from its own template; otherwise automatic reduction would reprocess its own output.
A record bound in-process from a notebook is refused for publication unless the client overrides.

**Instrument plus proposal scopes everything (D12).**
Both are mandatory on every record.
Run-number resolution, UI navigation, templates, and authorization by SciCat membership operate within a **proposal**, the experiment allocation that owns data and defines who may access it.
Instrument scientists and commissioning use long-lived proposals.
Artefacts produced there and consumed by every user proposal (direct beam, beam centre, processed vanadium, masks, lookup tables) are marked instrument-shared and readable from any proposal on that instrument; without that, every external user would need membership in the commissioning proposal.
Their disk copies are exempt from retention, because a recompute would run under a user who cannot read the commissioning inputs.
Templates from such a proposal, the instrument defaults, are marked instrument-shared the same way.
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
The file output of a file record satisfies any kind: the consumer's kind decides how it is materialized, and loading a file as a scipp array fails if it is not scipp HDF5.
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

**Outputs are a typed model in the same vocabulary (also D13).**
A spec declares its outputs as a model class, mirroring parameters, with a JSON Schema in the serialized form.
An array output is a data-reference field constrained by `ArraySpec`, which gains a `binned` flag; a beam centre is a vector with unit, a fit result a float with unit, a CIF file a reference of kind "opaque file".
Output fields may be optional.
Title and description are field metadata.
A downstream parameter may take a reference to any output field whose type matches, so chaining is a type check between two fields of the same vocabulary.
The spec as proposed allows non-array outputs but gives them no type, which breaks "outputs can be inputs" for exactly the values, such as beam centres and direct beams, that most often feed the next workflow.
Storage placement, inline or in the data store, stops being a spec concept.

**Cheap parameters and an optional code revision (also D13).**
A spec declares which parameters are cheap to change once the workflow is warm, and may carry a code revision.
Both are read by the framework and by UIs and mean nothing to a throwaway run.

**One built-in spec, `file` (D1).**
No workflow, one output of file type, and no kind, so the rule above that a file satisfies any kind is checked at materialization rather than at submission.

**Cost.**
The backend must walk the request's values to find references and the spec's JSON Schema, including nested models, to check them.
Structural validation of an array output against its `ArraySpec` happens in the runner at completion, since pydantic cannot check a scipp object.

## Components

- **Backend**: validates a request in three layers, finds the references by walking the request's values, checks each against the type of the output it names, resolves stand-ins to file records, creates the record, and hands the request to a launcher.
  Single writer to the record store; exactly one backend process per record store.
- **Client interface**: the backend's Python interface, including validate and views.
  This *is* the API.
- **Launcher**: pluggable: session, subprocess, cluster.
  Two execution shapes; placement is relative to the session holding a run's inputs, and a group runs in one shape.
  Publishes which specs its environment can run, so the backend can reject unrunnable requests at submission.
- **Runner**: materializes inputs, validates parameters with the real parameter class, calls the workflow, stores outputs, writes a completion marker to the disk tier, reports to the backend.
  In a session it keeps the workflow callable between runs.
  Never touches the record store.
- **Session**: a runner plus a private memory cache, belonging to one client.
  Created and closed by the client; closing drops its cache.
  In local mode it is the client's own process.
- **Data store**: a registry of disk copies and a disk tier, addressed as record plus output name plus optional key.
  Serves runners from the cache of their process when it can, and views from a cache or by partial reads from disk.
  A catalogue file's location comes from SciCat at dispatch and a local file's is its path; a store copy of a local file is kept until dropped explicitly or with its proposal.
- **Record store**: create, read, update status, and queries: records by proposal, time, batch ID and member key, or slot label; records that reference output X of record Y.
  Carries a schema version; drops a proposal's records together, never one.
- **Dataset source**: yields new datasets for a proposal as PID plus matchable metadata to the trigger loop, and persists nothing; a dataset becomes a file record when a request references it.
  One real implementation (SciCat) and one fake for tests.
- **Trigger loop**: on a new dataset or a completed record, or a group of either, matching a rule, instantiate a template and submit; never on a dataset whose SciCat entry carries our snapshot.
  Bound to one template version; moving it to a new version is a deliberate operation, and records say which version made them.
  Has its own visible status: last fire, last refusal with its structured errors.
- **Publisher**: writes an output to SciCat together with its provenance snapshot.
  Idempotent: the resulting PID is recorded on the output, and publishing it again returns the PID.

## Failure handling

Kept together so it can be read as one piece.

- **Status state machine.** submitted, waiting (pending inputs), dispatched (launcher job ID recorded), running, completed, failed, cancelled.
  Retry is a new record pointing at the old one, never a status reset.
- **Completion does not depend on the backend being up.** A throwaway runner writes a completion marker with its outputs to the disk tier before exit; the report through the backend's API is the fast path.
  On restart the backend reconciles dispatched runs against the launcher and the markers, so "finished while the backend was down" is completed, not failed.
- **Runner liveness.** A throwaway runner sends periodic signals; the backend marks a silent run failed after a timeout unless a completion marker exists, and rejects a report that arrives after that.
  This is the lesson of esslivedata's stuck "active" jobs (scipp/esslivedata#823) and of ADR 0008: observe, do not trust acknowledgements.
  Session runs have no liveness timeout; session loss is their failure event.
- **Backend restart.** Records are durable; queued requests are re-dispatched, running ones reconciled as above.
- **External cancellation** (cluster preemption) is detected by the same reconciliation.
- **Cancel of a running request** asks the launcher to stop it; dependents are cancelled.
- **Session loss.** A closed or crashed session drops its cache and its warm workflow.
  Runs in flight there fail; in local mode the session is the client, so there is nothing to resubmit until the user starts again.
- **Failure surfacing** is in scope from the start: a failed record carries a structured reason, so a user sees why a run failed without reading logs, and a trigger loop that is refused at submission is as visible as a run that failed.
  Facilities that built automatic reduction report that the monitoring UI was most of the value.
- **Slow or missing shared filesystem.** Fetching inputs has a timeout and a distinct failure status.
- **Multi-tenancy.** The backend checks proposal access on every reference it resolves or serves, not only at submission.
  Cluster jobs run under the submitting user's account.

## Execution modes mapped onto the model

- **Manual**: submit one request, inspect outputs, resubmit with changed parameters.
- **Interactive**: manual inside a session (D2), with a warm workflow (D8); each series of reruns, whether from a slider, a plot selection, or a growing list of runs, is a slot (D10).
- **Batch**: template plus overrides (D6).
- **Automatic**: trigger loop plus template (D7).
- **Chaining and map-combine**: pending outputs as inputs (D6).
- **Publication**: explicit publish of an output (D11).

## Explicitly deferred

HTTP transport, real SciCat integration, cluster launcher with its download tokens, view workers and the chunked on-disk layout for dense data, UI framework, UI state in the record store, metrics, agent-facing API.
Remote sessions, on the backend host or in a client process on the user's machine: a session launcher, an idle timeout, and a cap on sessions.
Upload of records from a private local record store to a shared backend.
Provisional outputs of a running run, for progress display during chunk-wise processing.
A versioned collection record, appended to by the client and referenced by version, if lists of runs to accumulate grow well beyond a few thousand entries and resending them whole becomes a cost.

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

- **Groups as the automatic-reduction unit.** A reflectivity curve needs four angle runs plus a reference, and the last run arrives last.
  "On new dataset, instantiate template" cannot say "wait until the series is complete".
  [Trigger rules match groups, and inputs and overrides are list-valued.]
- **Template sharing.** Both stages of a split workflow share most parameters.
  Facilities that tried template inheritance moved to version-controlled read-only templates with per-dataset substitution.
  [No inheritance. A template may be derived from another by copy, and the record keeps the origin.]
- **Remote sessions.** Where a session runs when interactive use moves to the shared web UI: on the backend host, in the user's own application, or as an interactive cluster job with queue latency at session start.
  [Decide once local sessions exist.]
- **Name of the backend component.** It clashes with esslivedata's "backend services".
  [Keep it unless the two projects are documented together.]
- **Retention policy** for disk copies in shared mode: how long each kind of run's outputs is kept, with superseded slot runs the shortest and automatic-reduction outputs the longest, and the analysis window after which a proposal's records are dropped.
- **Origin paths after a drop.** A local file's path stays on its record after its bytes are dropped, until the proposal is dropped.
  [Keep it: a path is not data, and provenance needs it.]
- **Two notebooks on one machine.** The sketch gives each its own store; referencing a result across notebooks needs a local transport.
  [Separate stores now; a local socket form of the HTTP transport later, which also serves the local application.]
- **SciCat push mechanism** for new datasets, if the deployment offers one.

## Next step

Review this document with the team before implementing.
Then a spike on the two decisions with the most hidden risk, D3 and D6: a data store with private caches and a disk-only registry, an atomic group submit with pending outputs, and a launcher that runs a chain in one session but a batch in throwaway processes, exercised by a fake map-combine pair.
Once the fake holds, a Tiled-backed disk tier as a second implementation of the same interface, checking that a scipp data array with units, variances, bin edges, and a mask survives the round trip.
The two designated testing seams are the fake dataset source and the session launcher; no browser tests in the skeleton.
The full walking skeleton, all components in local mode with no HTTP and no UI, follows if the spike holds.
The skeleton exists as the package `essapps` under `packages/`, import `ess.apps`, laid out for the scipp/ess monorepo: both execution shapes, the group submit with pending outputs, the warm sciline wrapper with its test helper, slots, views, templates, the trigger loop, and publication, against example workflows and fakes; the Tiled-backed disk tier and a real instrument workflow are not in it yet.

## Glossary

Where esslivedata uses a word differently, the clash is noted.

- **Backend**: the one component that accepts requests, keeps the records, and owns the stored results. In esslivedata "backend services" are the Kafka worker processes; unrelated.
- **Batch**: many runs made from one template, each with small differences and a member key. In esslivedata a batch is a bundle of messages; unrelated.
- **Client interface**: the backend's Python interface, including validate and views. The API.
- **Collection**: a list or dict of values of one declared type, as a parameter or an output. A reference may name one element of a collection output by key.
- **Data reference**: a field type: a parameter or output declared to hold a reference to a file or an array rather than a literal. Easy to confuse with *reference*, which is the value such a field holds.
- **Data store**: where the bytes of large outputs live: a registry of disk copies and a disk tier, owned by the backend. Each process that holds data also has a private memory cache, which the store serves from but never registers.
- **Dataset source**: where new datasets are discovered; persists nothing, a dataset becomes a file record when a request references it.
- **File record**: a record of the built-in `file` spec, standing for one SciCat dataset or one file on a user's disk, with an origin for identity and locations where its bytes can be opened; its single output is that file.
- **Group**: several requests submitted atomically that may reference each other's outputs before they exist.
- **Input**: a parameter of data-reference type. In esslivedata inputs are data streams and genuinely differ from parameters; here they do not.
- **Launcher**: decides where a run executes and starts it there.
- **Local mode**: client, backend, launcher, session, and data store in one Python process. **Shared mode**: the backend as a service used by many people; the **shared service** is that backend's process, which also holds a memory cache.
- **Pending output**: an output of a record that has not completed yet, usable as input to another request.
- **Proposal**: the experiment allocation that owns data and defines who may access it.
- **Provenance**: the traceable chain from any result back to the raw data, parameters, and software that produced it.
- **Recompute**: an explicit operation that runs a record's request again and yields a new record linked to the old one.
- **Record store**: the database of records. Records are never deleted one at a time; a proposal's records are dropped together.
- **Reference**: a value, "output X of record Y", optionally with an element key, usable as any parameter whose type matches. The only way a request names data.
- **Retention**: how long a disk copy is kept within its proposal's lifetime; it applies to bytes, never to single records.
- **Run record**: a run request plus what happened to it. Called "run", never "job", except for the launcher's own job IDs: in esslivedata a job is a running streaming workflow.
- **Run request**: everything needed to execute a workflow once.
- **Runner**: the process that executes runs: one run and exit, or many in a session.
- **SciCat**: the facility's data catalogue. **PID**: SciCat's persistent identifier for a dataset.
- **Session**: a runner plus a private memory cache, belonging to one client, keeping the outputs of its runs and the workflow itself in memory. A cache over records.
- **Slot**: a label on requests that supersede each other. The unit of interactive work, and what tools list and replay.
- **Spec**: the declared interface of a workflow: name, version, parameters, outputs. Defined in scipp/ess#690.
- **Template**: a saved, versioned run request with some fields left blank.
- **Throwaway process**: a subprocess or cluster job that runs one request and exits; the execution shape of shared mode.
- **Trigger loop**: watches for completed records that match a rule and submits runs automatically.
- **View**: a small piece of an output's data for display, computed by the process holding a copy. Not a run.
- **Vocabulary**: the set of types the workflow spec allows for parameters and outputs.
- **Warm workflow**: the workflow object kept alive in a session between runs. One per spec version.
- **Workflow**: the scientific code that turns inputs into results. Typically a sciline pipeline; the framework does not care.
- Libraries: **pydantic** (data validation), **sciline** (workflow graphs), **scipp** (scientific arrays, with its own HDF5 file format), **scitacean** (SciCat access), **plopp** (plotting), **FastAPI** (HTTP services).

## Index of decisions

Numbering follows reading order. It is provisional until the wider review and stable after it; then add at the end.

| | Decision | Where |
|---|---|---|
| D1 | Every value is an output of a record; files are records too; stand-ins resolve at submission; records are dropped by proposal, never singly; recompute is explicit | Records and references |
| D2 | Requests are stateless; execution may be stateful inside a session, which is a private cache | Choice 1 |
| D3 | The data store registers disk copies only; memory caches are private; two execution shapes; three lifetimes: session, proposal, catalogue | Choice 1 |
| D4 | Reuse across requests means a workflow boundary | Choice 1 |
| D5 | The record store is ours, small, and implementation-agnostic; the backend is its single writer | Choice 2 |
| D6 | One scheduling primitive: pending outputs as inputs; map and combine are optional uses of it | Choice 2 |
| D7 | The dataset source is abstracted and persists nothing; not Kafka | Choice 2 |
| D8 | Framework-to-workflow contract: a callable from parameters to outputs; declared cheap parameters; three validation layers | Choice 3 |
| D9 | The client interface is the API; validate is separate from submit; HTTP later; notebook first | Choice 4 |
| D10 | Interactive plotting: views are not runs and return plain arrays; event data is never viewed; reruns live in slots | Choice 4 |
| D11 | Only finalized data enters SciCat; publication reads a cold disk copy; the record store is not a catalogue | Ownership, publication, and deployment |
| D12 | Instrument plus proposal scopes everything; one backend per instrument | Ownership, publication, and deployment |
| D13 | One type vocabulary: inputs are data-reference parameters, outputs a typed model, collections on both sides | Spec changes |

## Review log

Reviewed before being shown to the team by independent AI reviewers from distinct angles: architectural consistency, fit with the real ess workflows read from source, operations and failure modes, prior art at other facilities, plain-language readability, lessons from esslivedata, a fresh reader, a maintainer's view of long-term pain, minimality, and adversarial scenarios.
The first pass shaped the failure-handling section, the record fields for resolved values and package versions, the serializer rule, instrument-shared artefacts, slots, and the open questions.
The second pass removed the mechanisms that created a second copy of truth or an implicit action: a registry that tracked copies in memory, recompute triggered by reads, identical-request reuse, record deletion by reachability, memory budgets in sessions, and slots as a backend object.
It also corrected the description of what a warm workflow reuses against the workflow source, and added the binned flag, optional map-combine, the completion marker, and intent-to-publish.
The third pass removed what would have made the record store a second catalogue: file records created by discovery with copied metadata and stored mount paths, and records kept forever; file records are now created on reference and hold identity only, catalogue locations are asked of SciCat, and records live as long as their proposal.

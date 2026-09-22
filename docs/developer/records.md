# Records, references, and where data lives

This document covers the run request, the run record, the two forms of reference by which a request names data, where a run executes, and where its outputs live.
It is the detail behind [Requests, records, references](architecture.md#requests-records-references) and [Where a run executes](architecture.md#where-a-run-executes).

## Requests and records

Two runs from a notebook, the second taking an output of the first:

```python
client = local(root, instrument='dream', proposal='p1', submitter='me')
loaded = client.run(LOAD, {'run': dataset_ref(instrument='dream', run=1)})
hist = client.run(HISTOGRAM, {'data': loaded.ref('data'), 'bins': 8})
```

A **run request** is everything needed to execute a workflow once, and its parameter values alone reproduce the outputs.
It is plain JSON-serializable data even when it never leaves the process, and it names no session, process, or storage location.
The only path it may name is the identity of a local file that carries no run identity.

`RunRequest` in `ess.apps.records`:

| Field | Purpose |
|---|---|
| `spec` | name and version of the workflow interface being called |
| `params` | parameter values, with data fields holding references |
| `instrument`, `proposal` | the scope of the run, both mandatory |
| `submitter` | who asked |
| `label`, `member_key` | records under one label supersede each other, per member key |
| `origin` | template version, rule version, lookup version and entry, and what was pinned beyond them |

The last two serve batches and rules ([rules.md](rules.md)).
The origin is explanation and not provenance, because the resolved request alone reproduces the run.

A **run record** is the request plus what happened to it.
`RunRecord` adds to `request`:

| Field | Purpose |
|---|---|
| `id` | UUID, so records move between stores without renumbering |
| `status` | submitted, waiting, dispatched, running, completed, failed, or cancelled |
| `created`, `started`, `finished` | timestamps |
| `resolved_params` | parameter values after the spec's defaults were applied |
| `outputs`, `stored_outputs` | small values in the record, references to outputs whose bytes the data store holds |
| `package_versions`, `environment`, `binding` | what the run was computed with, and how the spec was bound to code |
| `reused` | whether the result came out of a stage the session already held |
| `checksums` | checksum of each local file read, keyed by its dataset reference |
| `derives_from` | link to the record this one retries, recomputes, or copies, with the reason |
| `supersedes` | the record that was latest under this label and member key when the request was accepted |
| `failure` | structured reason a run failed, so a user sees why without reading logs |
| `published` | PID per published output |

Which of the two output fields a value lands in is decided by its type and is invisible to clients.
A record is immutable once the run completes, except for its status and the publication state of its outputs.

**Resolved values, package versions, and the environment are what make "recompute this record" true.**
Without them a changed default or an upgraded package silently changes what a record means.
The environment is an opaque name and revision, at ESS a conda environment, which the framework records and compares and does no more with.
A recompute is exact only in that environment, and refuses to run elsewhere unless the client overrides.

**Which record is latest is a link, never a clock**, because a clock would order records wrongly once a rule, a retry, and a person's correction submit under one label from different hosts.
`derives_from` says why a request was made, `supersedes` says where the record sits under its label, and a retry carries both to the same record.

Two kinds of content sit beside the record rather than in it: the runner's console output, whose size is unbounded, and **annotations**.
Annotations are labels and notes a user attaches after inspection, such as "use this vanadium".
They are outside provenance, may change at any time, and nothing in the framework reads them.

## References

A **reference** is a value that a parameter field of matching type may hold instead of a literal.
It is the only way a request names data, and it has two forms:

| Form | Names | Skeleton | Example |
|---|---|---|---|
| Output reference | output X of record Y, optionally one element of a collection output | `OutputRef` | `{"record": "b41c…", "output": "data", "key": null}` |
| Dataset reference | data the framework did not compute | `DatasetRef` | `{"dataset": "pid:20.500.12269/abc"}` |

**A reference names data by identity, never by where the bytes are**, and a record keeps its references in that form.
The data store's internal keys never appear in a request.
A reference may name a **pending output**, one whose record has not completed yet, and the backend holds the request until it does.
Only an output reference can be pending, so the scheduler has one kind of dependency.

**Provenance is the graph obtained by following references backwards**, from a result to the parameters, datasets, and software versions of every run that contributed.
`Backend.provenance` walks it into a self-contained snapshot.
There is no separate provenance model and no "which record produced this" query, because the reference names the record.

## Datasets

**Datasets are the leaves.**
A **dataset** is data the framework did not compute: a SciCat dataset, or a file on a user's disk.
`dataset_ref` in `ess.apps.spec` gives it the one identity it has:

```python
dataset_ref(pid='20.500.12269/abc')          # pid:20.500.12269/abc
dataset_ref(instrument='dream', run=1)       # run:dream/1
dataset_ref(path='/data/local.nxs')          # path:/data/local.nxs
```

The instrument and run number of a local file are what a PID is minted from, so a file that carries them needs no path, and the path is an identity only for a file that carries neither.
Identity is not location: where a catalogue dataset's bytes are is asked of SciCat at dispatch and cached at most, because SciCat moves files to archive and back and edits metadata while our records are immutable.

**Nothing is stored per dataset.**
Its proposal is checked against the request's at submission, when its SciCat entry is read anyway, and the data store registers a dataset only when it copies its bytes.
The checksum of a local file goes on the record of the run that read it, so a recompute can tell whether it read the same bytes, and the path the submitter typed stays on the origin.
A dataset has no request, no status, and nothing to recompute, so a dataset reference is never pending.
Datasets come from a dataset source ([rules.md](rules.md#the-dataset-source)).

**Stand-ins resolve at submission**, because provenance must not depend on a search that could give a different answer later.
A user may type a run number, a PID, or a path, and the backend turns it into a reference before it persists anything.
A run number is looked up in SciCat, where it is unique within an instrument and proposal, and a path under the facility filesystem resolves to the PID of the dataset that owns it.
A PID resolves to the run record named in its provenance snapshot while the store still has it, and otherwise to a dataset reference.
Any other path becomes a local dataset.
Nothing is downloaded at submission, and SciCat is not needed again once a reference exists.

**Whether an output is usable is two questions**: the record's status, and whether the data store holds a copy.
A missing copy is reported as such, never silently recomputed, because a silent recompute hides both its cost and the loss of the bytes.
`Client.recompute` submits the record's request again and yields a new record linked to the old one, while published outputs and catalogue files are downloaded again instead.

| Data | Where the truth lives | On a missing copy | Copies evictable |
|---|---|---|---|
| An output of a run record | the record: parameters, references, versions | recompute, explicitly | yes |
| A catalogue dataset | the PID, and SciCat says where the bytes are | download again | yes |
| A local file | its identity and the user's path | nothing to recover from, unless a store copy was made | only by an explicit drop |
| A published run record | the PID, whose entry carries the provenance snapshot | download rather than recompute | yes |

## Where runs execute and where data lives

Batch and automatic reduction need runs that can be made without a person present, and provenance needs runs that describe their result completely.
Interactive work needs the opposite: reruns in under a second over intermediates of several gigabytes, as in SANS, kept in memory between reruns.
**The design pins "stateless" on the record and allows state in the process that executes it**, which costs batch, automatic reduction, and provenance nothing, because those are properties of the record and not of the process.

Execution may happen in a **session**: a process belonging to one client, which keeps the outputs of its runs in memory.
Two invariants keep that safe.

1. Session identity never appears in a record.
2. Everything a session holds can be recomputed from records.

A session is therefore a cache, and losing one costs time and nothing else.
The second invariant holds for every value the system keeps in memory, including a partial sum over a growing series, because every input to such a value is a dataset or an output that has a record.
Held state is an optimisation with a recompute fallback, never the only copy of a fact, which is what lets a growing series be aggregated on disk or in memory interchangeably ([aggregation.md](aggregation.md)).
It holds because reduction here consumes datasets, and it would not hold for a live stream, whose inputs are pulses with no records, which is esslivedata's problem and out of scope.
Ephemeral identities are dangerous when other things depend on them (scipp/esslivedata#1042), so nothing depends on this one.

A session is defined by its owner, not by where it runs.
Beside the outputs of its records it holds the **stages** of its workflows, which are what makes a rerun cheap ([stages.md](stages.md)).
The workflow itself holds nothing between runs.
In **local mode**, client, backend, launcher, session, and data store are one Python process: a notebook or a local application.
In **shared mode** the backend is a service for one instrument, batch and automatic reduction run there without sessions, and the shared web UI submits and inspects.
A session on the backend host, or in a client process with the backend elsewhere, is a later addition.

A run executes in one of two shapes, and a group of requests submitted together runs in one of them, never mixed:

| | In a session | In a throwaway process |
|---|---|---|
| Process | lives as long as its client wants | one run, then exit: a subprocess or a cluster job |
| Inputs | from the session's private cache | fetched from disk, resolved to paths at dispatch |
| Outputs | stay in memory, written only when asked | written to disk before completion is reported |
| Used for | interactive work | batch, automatic reduction, everything in shared mode |
| Skeleton | `SessionLauncher` | `SubprocessLauncher` |

An output leaves a session only when the client asks for it with `Client.write_out`, which publication and chaining to a request placed elsewhere do on the client's behalf.
A launcher says which shape it is through `needs_disk_inputs`, so placement is explicit in its interface.

### The data store

The data store is owned by the backend: a registry and a disk tier.
The registry knows disk copies only, keyed by reference in either form: outputs written to the disk tier, and copies the store made of datasets, a catalogue file downloaded or a local file taken in.
Every process that holds data also keeps a private in-memory cache of outputs.

**Memory caches are private.**
A cache is never registered and is invisible to every other process, and nothing is pulled out of a session by anyone but the session's own client.
A registry over processes the backend does not own would need every eviction, close, and crash reported, which is a cache-coherence protocol, and it would block backend requests on user processes that may be busy or gone.
An index over a pool of runners the backend does own, addressed by the reference they hold, is a different case and stays open as an additive option ([kept runners](stages.md#kept-runners)).

A runner asks `DataStore` for an input and hands it an output.
The store serves from the cache of its process when it can, and otherwise reads or writes disk, which the runner cannot tell apart.
A run executes where every input has a reachable location.
When an input has none there, the client first makes one: it copies a local file into the data store, or fetches a catalogue file onto a machine without the mount, which are one action in opposite directions.

## Lifetimes

There are three, and the framework sets none of them.
Memory lives as long as its session, a process with an operating-system limit owned by its client, while the shared service's cache has a byte budget and evicts least recently used first, outputs of superseded records before anything else.
Records live as long as their proposal plus an analysis window set by the facility: long enough to find last week's result and to chain to yesterday's vanadium, and no longer, because what is worth keeping longer was published and a published entry carries its own provenance.

**Records are not deleted one at a time.**
A proposal's records and disk copies are dropped together, exported as one JSON bundle first, because every field that can hold a record ID would otherwise be an edge in a garbage collector, and a row per run costs nothing.
Dropping a whole proposal is safe because references never cross proposals, except into instrument-shared artefacts, whose commissioning proposals are long-lived.

Within a proposal, disk copies have a retention policy per kind of run.
When it expires the bytes are dropped with `Client.drop` and the record stays, which is the missing-copy case above.
Store copies of local files are exempt, because the framework cannot bring them back.
Such a copy is dropped only by an explicit operation on it or with its proposal, after which every record that reaches it through references is no longer recomputable, and such copies count against a per-proposal quota.

## Reuse means a workflow boundary

**A value that other requests reference must be an output of a run of its own, with its own record.**
Workflow authors therefore cut a workflow into separate specs exactly where such a value arises, and nowhere else.
Three reasons produce a cut.

Reuse: one artefact feeds many runs, such as processed vanadium, a beam centre, or a direct beam, which sample reductions, batch, and automatic reduction all take as an input.
This reason holds inside a session too, because the artefact needs a record of its own before batch can reuse it.

Iteration without a session: an expensive step whose result is tuned from a throwaway runner or from the shared web UI, such as loading and preprocessing a large run before adjusting its post-processing.
Inside a session this reason disappears, because a stage recomputes only what a changed parameter affects and the loaded data stays a value the stage holds, with no record, no reference, and no life beyond the session.

Aggregation: a sum over runs is cut where the members' contributions are added, so that each member has a record of its own ([aggregation.md](aggregation.md)).

Splitting is the only strategy that works on a fire-and-forget remote runner, and it lets the UI tell which part is cheap, because that part is a separate workflow.

## The record store

The **record store** is ours: SQLite on local disk, Postgres if a backend ever needs it.
It holds the records, the reference edges between them, and the registry of disk copies, and contains nothing sciline-specific.
The store carries a schema version, and a stored parameter set that no longer matches its spec version must fail loudly and never be silently defaulted, because schema versioning was left unresolved in esslivedata and needed hand-run migrations (scipp/esslivedata#915).

Besides create, read, and update of status, the store answers a fixed set of queries:

- records by proposal, time, template version, or rule version;
- the records under a label, and the latest per label and member key, which is the record that no other supersedes ([rules.md](rules.md#labels-batches-and-slots));
- the records that reference output X of record Y, or dataset D.

Batch tables, trigger decisions, the current members of a series, and provenance are built from these.

**The backend is the single writer.**
Exactly one backend process serves one record store, enforced by a lock that a live backend renews and a dead one loses (`RecordStore` raises `StoreLockedError`).
Single writer avoids the multi-client ownership problems that produced most of esslivedata's hard bugs (scipp/esslivedata#1285, #714).
In local mode every notebook is its own backend with its own store, at a location the client chooses with a per-user default, so a second notebook must use another location.

The runner reaches storage for data and the backend's API for everything else.
It never touches the record store, so there are no database credentials on compute nodes.
The backend resolves references to locations at dispatch: a dataset's path is handed to the runner as it is, asked of SciCat for a catalogue dataset or taken from the user for a local file, and everything else the runner fetches from the data store.

## Scheduling: pending outputs as inputs

A group of requests is submitted atomically and gets its record IDs back.
Inside the group, requests refer to each other's outputs before those exist, naming a member of the group with `@name`:

```python
group = client.submit_group({
    'a': client.request(LOAD, {'run': run_a}),
    'b': client.request(LOAD, {'run': run_b}),
    'sum': client.request(SUM, {'runs': [OutputRef(record='@a', output='data'),
                                         OutputRef(record='@b', output='data')]}),
})
```

**A pending output as an input is the only scheduling primitive.**
The backend holds a request until every record it references has completed, fails it if any of them fails, and cancels it if any is cancelled.
A request whose reference names a collection element that the completed producer does not have fails with a missing-key status, and the producer is unaffected.
Groups must be acyclic, and a group is validated whole before any record is created.
A waiting request has no timeout, because every record it waits on reaches a terminal state through its own failure handling.

This one primitive covers a vanadium stage feeding a sample reduction submitted together, temperature scans, angle series, and aggregation over a series of runs.
An aggregation is one member request per run plus one combine request whose collection parameter references an output of each member ([aggregation.md](aggregation.md)).

**Members of a batch are independent.**
There is no ordering between them, and rerunning a member is a new record under the same label and member key.
Anything else is chaining.
A batch is cancelled whole by its label, queued and running members alike.

## Alternatives considered

*Stateful jobs*, esslivedata's model: a running workflow object is the unit, clients address it by identity, and a record is whatever it reports.
Interactive work is natural in it, but provenance and batch must be reconstructed from a job's history, and everything else must compensate for identities that vanish with the process (scipp/esslivedata#1042, ADR 0008).

*Stateless everywhere*: every run is a fresh process, and every input and output passes through disk.
This is simple and remote-friendly, but it makes interactive loops disk-bound and rebuilds the workflow on every rerun, so the feedback loop never gets below seconds.

*Shared memory across processes*: a distributed object store such as Dask's or Ray's, or a layer of our own, so that any process can reach any array.
scipp objects are not chunk-aware, so such a layer would work badly and cost a lot to build and to operate.

*A registry that tracks in-memory copies*, so that the backend knows which process holds which output and can route a request to it.
It needs every eviction, close, and crash reported by processes it does not own, which is the coherence protocol the private-cache rule avoids.

*A workflow engine* such as AiiDA, Snakemake, Nextflow, or Prefect.
Each bakes the dependency graph into code rather than into records and keeps provenance in its own model, and none gives a stateless request that a notebook, a trigger loop, and a batch UI can all emit.

*A message broker, with Kafka for dataset events.*
Facilities that built their own scheduler for the remote case eventually added a broker (ISIS, SNS, Diamond, ESRF), so a cluster deployment may end here.
Kafka for dataset discovery would couple the backend to the streaming infrastructure and would reduce data that is not yet catalogued.
Reliable command delivery over Kafka was a long struggle in esslivedata (scipp/esslivedata#856), which concluded that a request row in a database is the durable desired state that was missing.

*Every dataset as a record of a built-in `file` spec*, so that a reference has one form.
It buys a UUID over an identity SciCat already keeps, a spec with no workflow, and a rule to stop the record store from becoming a catalogue, and gives nothing the two forms do not, because "which records used this dataset" is the same index over references either way.

*References to intermediate results*, so that a value inside a workflow can be reused without cutting the workflow.
Such a value has no record, no parameters, and no versions, so provenance and recompute would stop at it.

## Costs

The workflow object has two lifetimes in the runner: once per run, or once per session.
Remote sessions need a session launcher, an idle timeout, and a cap on the number of sessions, which is why the shared web UI initially has no interactive loop.
Shared mode pays one disk write and one read per output, plus a process start per run.
Whether a chained consumer exists is known only for requests submitted together as a group, so placement cannot be inferred for a request submitted on its own.

The author chooses where to cut a workflow, and the cut is not always clean.
Processed vanadium in diffraction is rebinned onto the sample's edges without interpolation, so a stored dense vanadium is usable only for compatible binning, and the alternative is to keep it as events.

Every rerun in a session is a complete record, so a series of N slider moves is N records, and labels keep that from being what a person sees ([rules.md](rules.md#labels-batches-and-slots)).
A list of references is resent whole on every rerun, but it holds references and not data, so at the few thousand entries expected it stays under a megabyte.

We own dependency handling, failure propagation, and cancellation, which is a small scheduler.
Owning the record store also means it could become a second catalogue by accretion: a row per dataset discovered, metadata copied for display, paths that go stale, and the rules that stop that are in [operations.md](operations.md).
"No broker" is a local-mode decision, to be re-examined when the cluster launcher is built.

# Operations: the client interface, publication, deployment, and failure handling

This document covers how clients reach the system, how a result reaches SciCat, how a deployment is scoped, and what happens when a run does not complete.
It is one of the topic documents behind [architecture.md](architecture.md).
Views and slots, the other half of what a client does, are in [stages.md](stages.md#views).

## The client interface

A notebook, a shared web UI, a trigger loop, and eventually an agent all need to submit runs, inspect records, and plot results.
They all do it through one interface, the backend's Python interface:

```python
report = client.validate(request)       # structured errors per field
if report.ok:
    record = client.submit(request)
```

**The Python client interface is the API.**
Every UI, notebook, or service reaches the backend only through it, and HTTP is a transport under the same interface rather than a second API.
Requests, records, templates, and references are plain data even in local mode, so nothing about the interface changes with the transport.
The reason is that a UI which reaches into backend internals owns state it does not control, which is where esslivedata's cross-session races came from (scipp/esslivedata#1046, #1098, ADR 0007).

**HTTP is a transport under the interface.**
`Backend` is a protocol, the closed surface a client may ask of a backend, with plain data in and out; it is the transport boundary.
`LocalBackend` does the work, in the notebook's process or in the server's.
`RemoteBackend` satisfies the same protocol and forwards each call as one HTTP request to a server that holds a `LocalBackend`, and `Client` does not know which of the two it holds.
The server holds one lock and a poller thread, because the backend is single-threaded by design: every route body and the poll run under the lock, and the poll is where dispatched runs are reconciled and waiting ones dispatched.
`wait` is a loop over `record` on the client side, so the server never blocks on a client.
An output travels as the file the data store holds, its suffix naming the serializer; this is the data path Tiled would replace.
`write_out` returns the data store's own path, meaningful on the shared filesystem of the deployment; given a folder, it places a copy there on the caller's side, which over HTTP is a streamed download and the way large data leaves the service.
A publisher is server-side code, so publishers are named when the server starts and a client publishes by name.
Binding workflow code in-process is an affordance of `LocalBackend` and not on the protocol.

What the transport cost, in lines:

| Module | Lines | Holds |
|---|---|---|
| `server` | 263 | one route per protocol method, the lock, the poller |
| `remote` | 275 | `RemoteBackend`, `remote()` |
| `cli` | 350 | `essapps serve`, `specs`, `datasets`, `submit`, `wait`, `output`, `publish` |

Closing the backend surface into the protocol changed no behaviour.
The poller thread is the one structural addition: in local mode `wait` drives `poll`, and in the server nothing else would.

**Validate is a separate operation from submit.**
It returns structured errors per field and says which of the three validation layers ran (see [workflow-contract.md](workflow-contract.md)).
Submit refuses a request with any error.
A UI calls validate on every change, so that feedback arrives while the user is still in the form.
`apply` uses the same operation to preview a whole batch before anything is created ([rules.md](rules.md)).

**Clients observe change by pulling with a version counter.**
A client asks what changed since the version it last saw.
Callbacks that carry data are not offered, because a client that misses one has no way back to a consistent view.

**There is no standalone local application at first.**
A notebook on the client library is the local application.
When a desktop application is wanted, it is the same web UI hosted in the local process against the in-process interface, and the shared deployment is that UI over the HTTP transport.
The UI framework is chosen after the backend exists.
A Python-driven web framework satisfies this as long as it only uses the client interface.
Tiled, if it is adopted for the data path, nudges instead towards a JavaScript frontend, with plopp remaining the notebook path.
Qt is out, because it would be a third UI with its own testing story and no path to shared use.

UIs for batch and interactive work will grow more complicated than anyone currently anticipates.
The workflow contract and the view interface are therefore the two contracts that must not foreclose UI options, and everything else in a UI is replaceable.

## The record store is not a catalogue

Our record store answers what was computed, and SciCat answers what was measured and what was finalized.
A record holds nothing from SciCat that it did not need in order to make a decision.
A UI that wants a sample name or the list of a proposal's runs asks SciCat, and a batch member key such as a temperature is supplied by the submitter rather than looked up.

SciCat is needed at two moments, when a stand-in is resolved and when an output is published.
A resolved reference never needs it again, so work on data that is already referenced continues while the catalogue is slow or down.
Local mode has no catalogue at all: a folder is its dataset source, read when asked, and the store holds nothing about the files in it.

Without these rules the store would become a second catalogue by accretion, one row per dataset discovered, with metadata copied for display and paths that go stale.

## Publication

**Only finalized data enters SciCat.**
Stage outputs and unreviewed outputs stay in our store, because data in SciCat cannot be removed through the regular API.
Publication is an explicit, idempotent operation on one output, triggered by a person after inspection or by an automatic-reduction rule.
Publishing an output that already has a PID returns that PID.

**The SciCat entry carries a self-contained provenance snapshot.**
It holds the raw PIDs the output derives from, the resolved parameters, the spec identity, the workflow record's ID and the stage's inputs and outputs, the package versions, and the environment.
It can be read without any service of ours, and our record is then a copy of it.
`Backend.provenance` builds the snapshot by walking the record's references.
A publication may name the PID it supersedes, which the snapshot records, since an entry in SciCat is never removed.

**Publication reads a disk copy of a result that was computed cold.**
An output that exists only in a session is written out first.
A record whose result came out of a held stage is recomputed in a throwaway process first, so that what enters SciCat was computed from its workflow record and stage inputs alone and the record describes it exactly.
A record bound to workflow code in-process from a notebook is refused unless the client overrides, because such a record cannot be reproduced elsewhere.

**The intent to publish is recorded before SciCat is written, and the PID after.**
A published output then has a second durable copy, so a later miss on it is a download rather than a recompute.

**The trigger loop recognizes a published output by the snapshot in its SciCat entry.**
It never fires on such a dataset, and never on a record made from its own template.
Otherwise automatic reduction would reprocess its own output.

## Scope: instrument plus proposal

**Instrument and proposal are mandatory on every workflow record, and so on every stage record.**
Run-number resolution, UI navigation, templates, and authorization by SciCat membership all operate within a proposal, the experiment allocation that owns data and defines who may access it.
The backend checks proposal access on every reference it resolves or serves, not only at submission.
Cluster jobs run under the submitting user's account.

**Artefacts from commissioning proposals can be marked instrument-shared.**
Instrument scientists and commissioning use long-lived proposals.
A direct beam, a beam centre, processed vanadium, masks, and lookup tables are produced there and consumed by every user proposal, so they are readable from any proposal on that instrument.
Without that, every external user would need membership in the commissioning proposal.
Their disk copies are exempt from retention, because a recompute would run under a user who cannot read the commissioning inputs.
Templates and lookups from such a proposal, the instrument defaults, are marked the same way.
A rule is bound to the proposal whose datasets it selects.

## Deployment

**One backend per instrument**, each with its own record store and data store.
Several backends may share a host while load is low.
With one or two users per instrument, of whom at most one works with large volumes, a single backend process serves views comfortably.
Nothing in the model needs cross-instrument state.
A facility-wide entry point, if one is ever wanted, is a thin front that routes to the instrument backend.

## Failure handling

A record's status says where a run stands.

| Status | Meaning |
|---|---|
| `submitted` | accepted and recorded, not yet schedulable |
| `waiting` | at least one referenced output has not completed |
| `dispatched` | handed to a launcher, whose job ID is on the record |
| `running` | the runner has started the workflow |
| `paused` | transient infrastructure failure, waiting to be dispatched again |
| `completed` | outputs validated and stored |
| `failed` | see the record's structured failure reason |
| `cancelled` | stopped on request, or a record it references was cancelled |

**A retry is a new record that links to the failed one.**
A status is never reset, so the history of what was tried stays readable.

**A failed record carries a structured failure reason.**
It says which of three things happened: the workflow reported a failure reason its spec declares, the workflow code raised, or the framework could not run the workflow.
A person, a UI, and a rule's retry policy read the three differently.
Surfacing failures is in scope from the start, because facilities that built automatic reduction report that the monitoring UI was most of the value.
A trigger loop refused at submission is as visible as a run that failed.

**Completion does not depend on the backend being up.**
A throwaway runner writes a completion marker to the disk tier after every output is flushed, and reporting through the backend's API is the fast path.
The marker is the only signal reconciliation trusts, because the presence of an output file proves nothing: a file written by a cluster job becomes visible on other hosts after a delay.
On restart the backend reconciles dispatched runs against the launcher and the markers, so a run that finished while the backend was down is completed, not failed.
`SubprocessLauncher` implements this shape in the skeleton.

**A silent runner is not trusted.**
A throwaway runner sends periodic signals, and the backend fails a silent run after a timeout unless a completion marker exists, rejecting a report that arrives later.
This is the lesson of esslivedata's stuck "active" jobs (scipp/esslivedata#823) and of its ADR 0008: observe, do not trust acknowledgements.
Session runs have no liveness timeout, because losing the session is their failure event.

**Transient infrastructure failure pauses a run rather than failing it.**
A runner that cannot reach an input location or the data store retries at increasing intervals, then reports `paused` with the cause and exits.
The backend also pauses a run at dispatch when a location it resolves is unreachable.
The record keeps its place in the chain, dependents stay `waiting`, and the liveness timeout does not apply.
Resume is the backend dispatching the same record again, when an operator asks or when a reachability probe finds the location back.
It is not a retry and makes no new record.
A shared filesystem that is slow for an hour must not cost one retry record per run.

**Cancellation propagates to dependents.**
Cancelling a running request asks the launcher to stop it, and the requests that reference its outputs are cancelled.
A batch is cancelled whole by its label.
External cancellation, such as cluster preemption, is found by the same reconciliation that runs after a backend restart.
Queued requests are re-dispatched after a restart, and paused ones stay paused.

**Losing a session fails the runs in flight there, and nothing else.**
A closed or crashed session drops its memory cache and its held stages, all of which are recomputable from records.
In local mode the session is the client, so there is nothing to resubmit until the user starts again.

**Logs outlive outputs.**
A throwaway runner's stdout and stderr are kept in the record store beside the record, and a session run logs to its client's process.
The structured failure reason covers the failures that were foreseen, and the log is for the ones that were not, such as a process killed for memory.
Logs are small and live as long as the record, and retention applies to the data store only.

## Technology

These are proposals, not decisions.

- pydantic for all data models, which the workflow spec already requires.
- Standard-library `sqlite3` for the record store, Postgres later if a backend ever needs it.
- scipp HDF5 for stored scipp data, with pluggable serializers for other output types.
- scitacean for SciCat access.
- FastAPI and httpx for the HTTP transport, click for the CLI.
- No workflow engine, no Dask, and no message broker in local mode.

Tiled (bluesky) is a candidate for the disk tier, the HTTP data transport, and per-node access control, behind the data-store interface.
It has a catalog with search, remote slicing over chunked storage, a policy plugin for access, and a Python client, on SQLite or Postgres.
It has no scipp semantics, so units, variances, bin edges, masks, and binned data would be a convention we own.
It has no server-side reductions, so views stay ours, and it knows nothing of memory caches.
Two questions for the Tiled developers: a scipp structure family, and server-side reductions.
Decide when the disk tier is built.

## Alternatives considered

**The UI reaches into backend internals.**
Fastest to start with, and the source of esslivedata's cross-session races (scipp/esslivedata#1046, #1098, ADR 0007).
Rejected because the backend then has no way to keep its own invariants.

**A Qt desktop application.**
A third UI with its own testing story, and no path to shared use.
Rejected.

**An HTTP API and a TypeScript frontend from day one.**
API-first in the strict sense, but it front-loads a transport and a second language before there is a backend to serve, and it needs a plotting stack other than plopp.
Rejected in favour of the same interface with HTTP as a transport under it.

**Writing every output to the catalogue.**
This would make publication automatic and remove one step for the user.
Rejected because SciCat entries cannot be removed, so every unreviewed intermediate would be permanent.

## Costs

- A local application in one process shares the interpreter between the UI and the runs, so a long run blocks the UI unless the session moves to a subprocess, which needs the remote-session machinery.
- The client interface carries a view vocabulary, and dense data needs a chunking decision at write time so that views on data larger than a cache can read partially from disk.
- Publication costs a recompute whenever the result being published was served by a held stage.
- Instrument-shared artefacts are an access-control case that SciCat proposal membership does not cover, and their disk copies are exempt from retention.
- "No message broker" is a local-mode decision, to be re-examined when the cluster launcher is built.

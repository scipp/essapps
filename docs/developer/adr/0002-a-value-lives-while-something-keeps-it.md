# ADR 0002: A value lives while something keeps it, and nothing is written unless persisted

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-11-10

## Context

History holds records, not values ([ADR 0004](0004-history-is-append-only-lists.md)).
Values take memory, so something must decide when they go, and which are written.

- **Two deployments.** Interactive work runs in the user's process (`local()`), on a VISA desktop or a laptop, and ending the process frees everything. The service runs batch and automatic reduction, and work spread over a cluster, for hours or days. The service must bound leaks and peak memory by design, not rely on users freeing memory ([users](../../requirements/users.md)).
- **Unattended work.** The automatic reduction submits through one client and never releases anything. A batch client closes, and users read its outputs the next morning.
- **Several connections.** A user works on one proposal through several clients at once, such as a notebook next to an app, or a web page and the same page reloaded.
- **Writing costs.** A batch of 500 would write story S3's masked counts, an output meant for inspecting a reduction, once per record, 500 times. A volume that a web UI only shows cuts of (B4) is hundreds of GB. The framework cannot tell intermediate from final outputs.

## Decision

[`ess.apps.protocol`](../../../packages/essapps/src/ess/apps/protocol.py) states the rules in full.

**A value lives while something keeps it.**
The module docstring lists the keepers.
A record object, a reference, or a label is not one, since many objects name one record (Alternatives).
Releasing drops a value, never a record.

**The workspace keeps what its clients ask for**, until a client releases the record or the workspace closes.
A *workspace* holds the history and the values of one proposal.
A *client* is a connection to a workspace, and keeps nothing.
So every client of a workspace reads, references, and releases the same values.
Releasing is explicit, since only the clients know which outputs they still need.
Closing a client made with `Client(workspace, ...)` releases nothing.
Forgetting to release costs memory until the workspace closes.

**The binding is a black box.**
The framework keeps a value only until it hands it to the binding.
It cannot look inside the binding, so it does not track what the binding keeps after that.

**Work that nothing keeps is cancelled**, since its outputs would be dropped the moment they are computed.
A running workflow is not interrupted, so cancelling frees memory, not CPU.

**A submission references only what the workspace keeps, persisted values, and files.**
So whether it is refused depends only on what the workspace keeps, not on how far other work has come.

**Nothing is written unless persisted**, since the framework cannot tell which outputs are worth writing (Context).
A client chooses per output, with `persist=` at submission or at `client.freeze`.
The store is then the only keeper, as batch reduction and automatic reduction need.
A failed write fails the record, so a controller sees it like any other failed record.
The store keys a value by record ID and output name, so a record holds no file name and a write changes no record.
A persisted output stays readable after an upgrade removes its record's spec version, since the record stays in history, and is read as plain scipp data ([ADR 0009](0009-a-record-of-another-spec-stays-readable.md)).

**A workspace reads and writes persisted outputs through `Store`**, by record, output name, and type.
A file format belongs to each stored value, so a store may hold several.
A value's file format must let one array be read without the others, and the sizes, unit, and coordinates without the data, since `client.describe` reads no array and `select=` reads a part.
scipp's HDF5 layout does.
`Store` reads a value whole.
Reading a part is added with the first output that needs it.

**The same rules hold in the user's process and on the service.** What differs:

| | user's process (`local()`) | service |
|---|---|---|
| workspaces | one, behind the client that `local()` returns | one per proposal |
| values not persisted | in the process's memory | in the service's memory, bounded per workspace (Open) |
| the workspace closes | when its client closes, or with its process | when the service ends it (Open) |
| the store | given to `local(store=...)` | the proposal's area |

Workspaces share values only through the store.

**After a restart, only persisted records continue**, as system story H2 needs.
History and the store survive.
The values the workspace kept in memory do not.
Each pending record is

- failed, if it had started: the workspace ended while it ran, perhaps because of it, as an out-of-memory kill does, so running it again could end the process again;
- otherwise cancelled, unless a persist request names it, or a record named by a persist request reads it, directly or through other pending records;
- otherwise run, if every value it reads is a file, is written, or comes from a pending record that runs;
- failed otherwise, as the record of a freeze is, since its driver is gone.

## Alternatives considered

- **Each client keeps what it asks for.** A client would keep its records until it released them or ended, and another client, even one in the same process, would read a value only once persisted. The service would bound each client's memory by a *cap*, and end a client that makes no call within its *lease*, a time limit that every call renews. The lease frees the values of a client that vanished without releasing them, such as a closed browser tab, so the service would bound leaks without relying on users to free memory, as the Context asks. It is not chosen for two reasons. A second connection of the same user, such as a reloaded web page, could not read the values of the first (Context, several connections). Every call would name its client, and the core would check ownership per client, which is multi-tenancy inside the core.
- **One workspace for many proposals.** The core would separate the history, values, and files of each proposal, and check every call against them. A service gets the separation between proposals by running one workspace per proposal instead.
- **Record objects keep values, as dask futures do.** `client.records()`, `client.latest()`, and provenance each return a new object for the same record, so no single object could own the value. A dict of records (D2, kept for the morning check) would keep every value, and so would Jupyter's `Out[n]`.
- **Persisting a kept record later (`client.persist`).** A person could keep a result after seeing it without computing it again. No story or application needs it. A write that ends after its record has finished has no record to fail, so its failure would need a channel of its own, such as an error raised when the workspace closes. It needs two more kinds of event in history. It can be added when a story needs it.
- **Running a started record again once after a restart.** A reduction that exhausts memory would end the process a second time, and history would have to count the attempts.
- **Keep only on request (`keep=True`).** The flag must be given at submission, since a value dropped when its request finishes cannot be kept later without a race. Forgetting it shows only when a later `client.output` or reference is refused.
- **Views first, and a record made only at persist.** A read returns a view, a handle without a record. It needs a second handle type, records of upstream views that have no values, which a later `persist` cannot complete, and the states of accumulators kept inside views. A record is one entry in history, so making it lazily saves nothing worth that.
- **Sessions or nested scopes.** A session reopened by name is lost when the workspace restarts, and needs names and an expiry. A scope is a `with` block, which a notebook cannot hold across cells.
- **A submission reads any value that something keeps**, such as a released record that a pending request still reads. Whether the same code is refused would depend on whether that record has completed and been dropped yet.
- **A stage keeps its template's values until it is released**, so that the workspace could stage it again. Nothing drops what a stage computed, and a staging that failed most often fails again, such as on a corrupt file.
- **The service writes every output, or persists by default.** It writes, and reads back, outputs that nobody asked for, such as a volume that users only see through cuts. A cut read from a volume of hundreds of GB on disk, rewritten after each run, is too slow to follow a slider.
- **Values as a cache over history, recomputed when read.** Reading could take as long as the first computation. An earlier state of an accumulator could be recomputed only by reducing again every row pushed before it.

## Consequences

- A chain may release its intermediate records once the requests that read them are submitted. Releasing the last request cancels the whole chain, unless something else keeps a part of it.
- Values that are not persisted are lost when the workspace closes. The records stay in history.
- A client can release a value that another client still shows. A read of it then raises `NotReadable`, so an application asks `client.readable` for what it shows.
- A release by one client also cancels pending work that another client submitted, if nothing else keeps it. A read of that record then raises `RecordFailed`, not `NotReadable` (`WorkspaceProtocol.outputs`).
- A second connection, such as a reloaded web page, reads every value the workspace keeps. It reaches a stage or an accumulator of the first connection only if it is handed the object, since no client call lists them or reopens one by its ID ([README, open question 9](../README.md#open-questions)).
- A client that ends without releasing, such as a closed browser tab, leaves the values it asked for kept until another client releases them or the workspace closes.
- Nothing in the Decision bounds the memory of a workspace on the service. Until the open point on how the service bounds that memory is decided, the service does not meet the Context's requirement to bound leaks and peak memory by design.
- A kernel restart ends the process, its workspace, and everything the workspace keeps. Continuing after the user's process crashed is a non-goal ([requirements](../../requirements/README.md)).
- The placement symmetry ([accumulators.md, Symmetries](../accumulators.md#symmetries)) holds only if every output reads back from the store as the workflow returned it. The fake store keeps values in memory, so no test checks this.

## Open

To be designed with the service (scipp/essapps#27):

- Whether the users of one proposal share the values of its workspace in memory, or the service separates or bounds them per user. The history is one per proposal.
- How the service bounds the memory of a workspace: what counts against the bound, such as the drivers of accumulators and values waiting to be written, and what happens to a value that does not fit. Where a workspace's values are held when its work spreads over nodes.
- When the service ends a workspace that is idle, since a client that ends without releasing leaves its values kept.
- How the service holds an accumulator's driver. One cluster job per accumulator, with a memory size and deadline that its client declares, fits a spectroscopy volume of hundreds of GB. That rests on items Assumed or Open in [spectroscopy](../../requirements/spectroscopy.md), and on whether the service may submit cluster jobs ([systems](../../requirements/systems.md)). Most drivers hold little, such as 0.34 MB for LoKI's I(Q).
- A memory bound for every request. The plain request over two plus two LoKI runs needs 7.5 to 9.9 GB. Only the binding knows what a request needs.
- What a finish records besides the status, such as the software environment.
- When values are dropped from the store before history (system story H1), and by whom.
- The file format of each output type, and the layout of the store in a proposal's area.
- An upgrade while persisted records run. An orderly stop of the old instance waits for its running records, so the new version waits, for hours after an overnight batch, since only one workspace writes a proposal's history. If the old instance is killed instead, its running records fail at the new instance's start, and a person or the application submits them again, so the time they had run is lost. Either way, the upgrade closes every workspace of the old instance.

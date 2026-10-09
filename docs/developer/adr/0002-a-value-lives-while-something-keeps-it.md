# ADR 0002: A value lives while something keeps it, and nothing is written unless persisted

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-09

## Context

History holds records, not values ([ADR 0004](0004-history-is-append-only-lists.md)).
Values take memory, so something must decide when they go, and which are written.

- **Two deployments.** Interactive work runs in the user's process (`local()`), on a VISA desktop or a laptop, and ending the process frees everything. The service runs batch and automatic reduction, and work spread over a cluster, for hours or days. The service must bound leaks and peak memory by design, not rely on users freeing memory ([users](../../requirements/users.md)).
- **Unattended work.** The trigger loop submits through one client and never releases anything. A batch client closes, and users read its outputs the next morning.
- **Writing costs.** A batch of 500 would write story S3's masked counts, an output meant for inspecting a reduction, once per record, 500 times. A volume that a web UI only shows cuts of (B4) is hundreds of GB. The framework cannot tell intermediate from final outputs.

## Decision

README, [How long records and values are kept](../README.md#how-long-records-and-values-are-kept), states the rules in full.

**A value lives while something keeps it.**
The README lists the keepers.
A record object, a reference, or a label is not one, since many objects name one record (Alternatives).
Releasing drops a value, never a record.

**The client keeps what it asks for**, until it releases the record or ends, since only the client knows which outputs it still needs.
Forgetting to release costs memory until the client ends.

**The binding is a black box.**
The framework keeps a value only until it hands it to the binding.
It cannot look inside the binding, so it does not track what the binding keeps after that.

**Work that nothing keeps is cancelled**, since its outputs would be dropped the moment they are computed.
A running workflow is not interrupted, so cancelling frees memory, not CPU.

**A submission references only what its client keeps, persisted values, and datasets.**
So whether it is refused depends only on what its client keeps, not on how far other work has come.

**Nothing is written unless persisted**, since the framework cannot tell which outputs are worth writing (Context).
A client chooses per output.
`client.persist` keeps a value in memory and in the store.
`persist=` at submission or at `client.freeze` keeps it only in the store, as batch reduction and the trigger loop need.
A failed write fails a record that was persisted at submission, so a driver sees it like any other failed record.
The store keys a value by record ID and output name, so a record holds no file name and a write changes no record.

**The same rules hold in the user's process and on the service.** What differs:

| | user's process (`local()`) | service |
|---|---|---|
| values not persisted | in the process's memory | in the service's memory, under a *cap*, a memory limit per client |
| a client ends | at `close()`, which waits for pending writes, or with its process | at `close()`, or when its *lease*, a time limit that every call renews, runs out |
| the store | given to `local(store=...)` | the proposal's area |

The service's cap counts the values a client keeps, those its pending requests keep, and its held states.
Values with a persist request count against the proposal until they are written, and the cap never drops them.
Any other completed value that does not fit is dropped, and reading it raises with the reason.

**After a restart, only persisted records continue**, as system story H2 needs.
History and the store survive.
No client does.
Each pending record is

- cancelled, unless a persist request names it, or a record named by a persist request reads it, directly or through other pending records;
- run, if every value it reads is a dataset, is written, or comes from a pending record that runs;
- failed otherwise, as the record of an accumulator's state is, since its held state is gone.

## Alternatives considered

- **Record objects keep values, as dask futures do.** `client.records()`, `client.latest()`, and provenance each return a new object for the same record, so no single object could own the value. A dict of records (D2, kept for the morning check) would keep every value, and so would Jupyter's `Out[n]`.
- **Keep only on request (`keep=True`).** The flag must be given at submission, since a value dropped when its request finishes cannot be kept later without a race. Forgetting it shows only when a later `client.output` or reference is refused.
- **Sessions or nested scopes.** A session reopened by name is lost at a backend restart, and needs names and an expiry. A scope is a `with` block, which a notebook cannot hold across cells.
- **A submission reads any value that something keeps**, such as a released record that a pending request still reads. Whether the same code is refused would depend on whether that record has completed and been dropped yet.
- **A stage keeps its template's values until it is released**, so that the backend could stage it again. Nothing drops what a stage computed, and a staging that failed most often fails again, such as on a corrupt file.
- **The service writes every output, or persists by default.** It writes, and reads back, outputs that nobody asked for, such as a volume that users only see through cuts. A cut read from a volume of hundreds of GB on disk, rewritten after each run, is too slow to follow a slider.
- **Values as a cache over history, recomputed when read.** Reading could take as long as the first computation. An earlier state of an accumulator could be recomputed only by reducing again every row pushed before it.

## Consequences

- A chain may release its intermediate records once the requests that read them are submitted. Releasing the last request cancels the whole chain, unless something else keeps a part of it.
- An interactive client that persists nothing loses its values when it ends. Its records stay.
- A kernel restart ends the process, its backend, and everything it keeps. Continuing after the user's process crashed is a non-goal ([requirements](../../requirements/README.md)).
- The placement symmetry (README, [Symmetries](../README.md#symmetries)) holds only if every output reads back from the store as the workflow returned it. The fake store keeps values in memory, so no test checks this.

## Open

To be designed with the service (scipp/essapps#23, scipp/essapps#27, scipp/essapps#34):

- How the cap per client is set, where a client's values are held when its work spreads over nodes, and how the service notices a client whose process ended.
- How the service holds an accumulator's held state. One cluster job per accumulator, with a memory size and deadline that its client declares, fits a spectroscopy volume of hundreds of GB. That rests on items Assumed or Open in [spectroscopy](../../requirements/spectroscopy.md), and on whether the service may submit cluster jobs ([systems](../../requirements/systems.md)). Most held states are small, such as 0.34 MB for LoKI's I(Q).
- A memory bound for every request. The plain request over two plus two LoKI runs needs 7.5 to 9.9 GB. Only the binding knows what a request needs.
- What a finish records besides the status, such as the software environment.
- When values are dropped from the store before history (system story H1), and by whom.
- The file format of each output type, and the layout of the store in a proposal's area.
- An upgrade while persisted records run. If the old instance finishes them, the new version waits, for hours after an overnight batch, since only one backend writes history. If the new instance runs them again, the time they had run is lost. Either way, the upgrade ends every client of the old instance.

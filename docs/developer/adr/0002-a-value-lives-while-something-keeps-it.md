# ADR 0002: A value lives while something holds it, and nothing is written unless persisted

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-11-10

## Context

History holds records, not values ([ADR 0004](0004-history-is-append-only-lists.md)).
Values take memory, so something must decide when they go, and which are written.

- **Two deployments.** Interactive work runs in the user's process (`local()`), and ending the process frees everything. A service runs batch and automatic reduction for hours or days, and must bound leaks and peak memory without relying on users to free memory ([users](../../requirements/users.md)).
- **Unattended work.** The automatic reduction never releases anything. A batch client closes, and users read its outputs the next morning.
- **Several connections.** A user works on one proposal through several clients at once, such as a notebook next to an app, or a web page and the same page reloaded.
- **Writing costs.** A batch of 500 would write story S3's masked counts 500 times. A volume that a web UI only shows cuts of (B4) is hundreds of GB. The framework cannot tell intermediate from final outputs.

## Decision

An output value lives while something holds it: the workspace, a pending request that reads it, or the store.
The workspace of a proposal holds what any of its clients asks for, until a client releases the record or the workspace closes.
A client holds nothing, so every client of a workspace reads, references, and releases the same values.
Work whose outputs nothing would hold is cancelled.
A submission may reference only values the workspace holds, persisted values, and files.
Nothing is written unless a client persists it, per output, at submission or at freeze, and a failed write fails the record.
A store's file format must let one array be read without the others, and an array's sizes, unit, and coordinates without its data, for `client.describe` and `select=`.
After a restart, only work that a persist request needs continues.
[`ess.apps.protocol`](../../../packages/essapps/src/ess/apps/protocol.py) states the rules.

## Alternatives considered

- **Each client holds what it asks for**, with a memory cap and a lease per client. A reloaded web page could not read the values of the page before it, and the core would check ownership per client.
- **One workspace for many proposals.** The core would separate the history, values, and files of each proposal; one workspace per proposal separates them for free.
- **Record objects hold values, as dask futures do.** `client.records()` and provenance return a new object for the same record, and a dict of records or Jupyter's `Out[n]` would hold every value.
- **Persisting a held record later (`client.persist`).** No story needs it, and a write that fails after its record finished has no record to fail.
- **Running a started record again after a restart.** A reduction that exhausts memory would end the process a second time.
- **Hold only on request (`keep=True`).** A forgotten flag shows only when a later read or reference raises.
- **Views first, and a record made only at persist.** It needs a second handle type and records of upstream views without values, and saves only one history entry.
- **Sessions or nested scopes.** A session is lost when the workspace restarts, and a `with` block cannot span notebook cells.
- **A submission reads any value that something holds.** Whether the same code raises would depend on whether a released record has completed yet.
- **A stage holds its template's values until it is released**, to stage again. A staging that failed most often fails again, such as on a corrupt file.
- **The service writes every output, or persists by default.** It writes outputs nobody asked for, and a cut of a volume of hundreds of GB on disk is too slow to follow a slider.
- **Values as a cache over history, recomputed when read.** A read could take as long as the first computation, and an earlier state of an accumulator needs every row again.

## Consequences

- A release by one client also cancels pending work that another client submitted, if nothing else holds it.
- A client can release a value that another client still shows, so an application asks `client.readable` for what it shows.
- A client that ends without releasing, such as a closed browser tab, leaves its values in memory until another client releases them or the workspace closes.
- Values that are not persisted are lost when the workspace closes. The records stay in history.
- A kernel restart ends the process, its workspace, and everything in its memory. Continuing after the user's process crashed is a non-goal ([requirements](../../requirements/README.md)).

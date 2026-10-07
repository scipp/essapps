# ADR 0004: History is append-only lists, dropped per proposal once it is idle

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-07
- Supersedes: [ADR 0001](0001-history-as-an-event-log.md)

## Context

[ADR 0001](0001-history-as-an-event-log.md) made four decisions at once:

1. A snapshot of an accumulator is stored as the accumulator and the number of elements it covers, and each element is stored once, at its push.
2. Output values are not history.
3. History is kept as long as the proposal.
4. History is an append-only log of events, and records, labels, and the elements of accumulators are views built by applying the events in order.

Story D7 became linear through the first decision, since a snapshot no longer lists its elements.
The fourth framed the whole system: system.md described every part in terms of events and views.

A reading of the in-process backend found what depends on the log:

- Every query reads the views: status, records under a label, provenance, the checks of references, and the pushes that give the plain request of a state that a submission reads.
- Only a backend that starts on an existing log reads the events, to rebuild its views and run the records left pending. `local()` keeps its log in memory, so this never happens there; only the tests start a backend on a log file.
- No query asks for the state at an earlier time. Provenance follows records and pushes as they are.
- A restart cannot finish a pending record whose input had already completed, since values are not history. System story H2 needs persisted values for that, not only history ([ADR 0005](0005-nothing-is-written-unless-persisted.md)).

The third decision conflicts with the requirements ([tensions](../../requirements/tensions.md), "Finding results again versus a second catalogue"):

- When a proposal ends is not defined. If it lasts as long as the proposal's raw data, history is kept forever: a record of what ran next to SciCat, where the lasting history belongs.
- Users must find their results while they consider their work ongoing, as with an application they leave open: days to weeks for batch and automatic reduction. A limit is acceptable, and a result needed for longer goes to SciCat.

What the stories need from history:

- Records outlive the backend process: B6, H2, H3, and the trigger loop, which knows which datasets a rule has handled only from the records under the rule's label.
- A submission is stored whole or not at all.
- The order of the records under a label, and of the pushes into an accumulator, is kept.

- Persisted values are found again: which outputs were asked to be persisted, and whether the write succeeded.

Records never change, a record's status is set once, and pushes, persist requests, and writes are only added.
So history only grows until it is dropped, with or without a log.

## Decision

History is six lists, each only appended to:

| List | One item per | Holds |
|---|---|---|
| records | record | ID, time, proposal, submitter, request, output names, label, member |
| accumulators | opened accumulator | ID, proposal, template |
| finishes | finished record | record ID, status, failure message |
| pushes | push into an accumulator | accumulator ID, one row per table |
| persist requests | request to persist outputs of a record | record ID, output names |
| writes | write that ended | record ID, the outputs written, or why the write failed |

- A record never changes. Its status is its finish; a record without one is pending.
- A request that reads an accumulator reads a record of the plain request of the state, which the accumulator's template and its pushes give ([ADR 0008](0008-a-read-of-a-state-is-a-record.md)). History stores such a record as the accumulator and its number of pushes, and lists the rows when it is read, so history grows by a constant amount per push and per read. A record that `freeze` returns is stored the same way ([ADR 0003](0003-accumulators-add-in-place.md)).
- A record's status is that of its computation. A write that fails fails its persist request, not the record ([ADR 0005](0005-nothing-is-written-unless-persisted.md)).
- Output values are not history. [ADR 0002](0002-the-client-is-the-lifetime.md) says what keeps them, and [ADR 0005](0005-nothing-is-written-unless-persisted.md) how they are persisted.
- A proposal is idle while none of its clients is open, none of its records is pending, and none of its writes is pending. Once it has been idle for the retention period, days to weeks as the deployment sets it, its history is dropped as a whole. A result needed for longer is published.
- How a backend stores the lists is its choice. The in-process backend stores them as one event log, in memory or in a file of JSON lines.

## Alternatives considered

- **History as an event log (ADR 0001).** The lists hold the same history. The log adds one order across all the lists, and views rebuilt by applying the events in that order; nothing outside storage reads either. It would also tie a hosted backend to a log.
- **History kept as long as the proposal (ADR 0001).** Possibly forever, and so a second record of what ran next to SciCat.
- **Each record dropped by age, keeping the older records that a kept record reads.** Bounds the history of every proposal, and a kept record's provenance stays complete. But the trigger loop would reduce again every dataset whose records were dropped, so it would need a memory of its own: a set of handled datasets per rule, or the inputs that the derived datasets in SciCat list. The second needs automatic reduction to publish every result, which the requirements leave open.
- **A proposal's history dropped a retention period after its last record.** The same unit with a simpler clock. But a trigger loop that sees no new dataset for that long loses its rule's records and then reduces every dataset again, and an open client would lose the records it still reads.
- **The record of a state stored with every row of its request.** A driver that reads after every push makes records of 1, 2, ..., n rows, so history grows with the square of the number of pushes: 45,000 rows for 300 pushes. The pushes already hold the rows once.
- **No stored history in the in-process backend until a hosted backend exists.** This removes the log file, the restart, and their tests. But the in-process backend is the only place where storing and restarting are tried, and what it shows informs a hosted backend.

## Consequences

- Dropping a proposal's history leaves no dangling reference: no other proposal reads its records (system story G5), and an idle proposal has no open client and no record that waits. The provenance of every kept record is complete.
- A running trigger loop keeps a client of its proposal open, so the proposal is not idle and the loop never reduces a handled dataset again. A loop started for a proposal whose history was dropped reduces its datasets again.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps its history that long.
- A client whose process ended without closing it keeps its proposal from being idle until the backend ends it. How the service notices such a client is open (scipp/essapps#34).
- A backend that restarts does not know when its earlier clients ended, so it counts idle time from its start.
- The in-process backend keeps its log and never drops history. system.md describes the lists first, and the log as how this backend stores them.
- What the in-process backend shows for a hosted one:
  - A file that is only appended to stores lists that only grow, with one write per change. A submission is one line, so it is stored whole or not at all, and a line cut short by a crash is dropped when the file is read.
  - A log must keep old event formats readable for as long as it is kept, while the views may change between versions. Database tables would be migrated instead.
  - A backend that rebuilds its views from the log at start holds every record of its proposals in memory.
  - H2 needs persisted values ([ADR 0005](0005-nothing-is-written-unless-persisted.md)): a record pending at a restart runs only if every value it reads is written, will be written, or is a dataset.

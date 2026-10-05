# ADR 0004: History is three append-only lists; an event log is one way to store them

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05
- Supersedes: [ADR 0001](0001-history-as-an-event-log.md)

## Context

[ADR 0001](0001-history-as-an-event-log.md) made four decisions at once:

1. A snapshot of an accumulator is stored as the accumulator and the number of elements it covers, and each element is stored once, at its push.
2. Output values are not history.
3. History is kept as long as the proposal (its amendment).
4. History is an append-only log of events, and records, labels, and the elements of accumulators are views built by applying the events in order.

Story D7 became linear through the first decision, since a snapshot no longer lists its elements.
The fourth framed the whole system: system.md described every part in terms of events and views.

A reading of the in-process backend found what depends on the log:

- Every query reads the views: status, records under a label, provenance, the checks of references, and the count a snapshot takes.
- Only a backend that starts on an existing log reads the events, to rebuild its views and run the records left pending. `local()` keeps its log in memory, so this never happens there; only the tests start a backend on a log file.
- No query asks for the state at an earlier time. Provenance follows records and elements as they are.
- A restart cannot finish a pending record whose input had already completed, since values are not history. System story H2 needs saved values for that, not only history.

What the stories need from history:

- Records outlive the backend process: B6, H2, H3, and the trigger loop, which knows which datasets a rule has handled only from the records under the rule's label.
- A submission is stored whole or not at all.
- The order of the records under a label, and of the pushes into an accumulator, is kept.

Records never change, a record's status is set once, and pushes are only added.
So history only grows, with or without a log.

## Decision

History is three lists, each only appended to:

| List | One item per | Holds |
|---|---|---|
| records | record | ID, time, proposal, submitter, request or snapshot, output names, label, member |
| finishes | finished record | record ID, status, failure message |
| pushes | push into an accumulator | accumulator ID, element |

- A record never changes. Its status is its finish; a record without one is pending.
- A snapshot covers the first pushes into its accumulator, and its record names the accumulator and how many.
- Output values are not history. [ADR 0002](0002-the-client-is-the-lifetime.md) says what keeps them.
- A proposal's history is kept as long as the proposal and dropped with it as a whole.
- How a backend stores the lists is its choice. The in-process backend stores them as one event log, in memory or in a file of JSON lines.

## Alternatives considered

- **History as an event log (ADR 0001).** The lists hold the same history. The log adds one order across all three lists, and views rebuilt by applying the events in that order; nothing outside storage reads either. It would also tie a hosted backend to a log.
- **No stored history in the in-process backend until a hosted backend exists.** This removes the log file, the restart, and their tests. But the in-process backend is the only place where storing and restarting are tried, and what it shows informs a hosted backend.

## Consequences

- The in-process backend keeps its log. system.md describes the lists first, and the log as how this backend stores them.
- What the in-process backend shows for a hosted one:
  - A file that is only appended to stores lists that only grow, with one write per change. A submission is one line, so it is stored whole or not at all, and a line cut short by a crash is dropped when the file is read.
  - A log must keep old event formats readable for as long as the proposal, while the views may change between versions. Database tables would be migrated instead.
  - A backend that rebuilds its views from the log at start holds every record of its proposals in memory.
  - H2 needs a store of saved values: without one, a record pending at a restart fails if one of its inputs had already completed.

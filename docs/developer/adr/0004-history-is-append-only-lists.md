# ADR 0004: History is append-only lists, dropped per proposal once it is idle

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-09

## Context

A record names a spec, every parameter value, its inputs by reference, and its outputs.
A record never changes, only its status, which is set once.
Pushes, persist requests, and writes are only added.
So history only grows until it is dropped.

The stories need four things from history:

- Records outlive the backend process (B6, H2, H3). The trigger loop knows which datasets a rule has handled only from the records under the rule's label.
- A submission is stored whole or not at all.
- The order of the records under a label, and of the pushes into an accumulator, is kept.
- Persisted values are found again.

Two forces bound its size.

**Pins multiply rows.** Story D7 pins an accumulator after each of its 300 pushes.
If each record of a state listed its rows, history would hold about 45,000 rows, and every layer that handles records would handle them all.
In the in-process backend, D7 with 1000 pushes took 5.6 s with every row listed (501,500 stored references), and 0.86 s with a count of pushes.

**A proposal has no defined end.** Users need their records and outputs for days to weeks ([tensions](../../requirements/tensions.md), "Finding results again versus a second catalogue").

## Decision

History is six lists, each only appended to:

| List | One item per | Holds |
|---|---|---|
| records | record | ID, time, proposal, submitter, request, output names, label, member |
| accumulators | opened accumulator | ID, proposal, template, and the template's values typed with defaults filled in |
| finishes | finished record | record ID, status, failure message, the names of optional outputs that the workflow did not return |
| pushes | push into an accumulator | accumulator ID, one row per table |
| persist requests | request to persist outputs of a record, made after its submission | record ID, output names |
| writes | write of a persist request that ended | record ID, the outputs written, or why the write failed |

- A record never changes. Its status is its finish, and a record without one is pending.
- The record of a state ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)) is stored as the accumulator and its number of pushes. Its rows are those of the first n pushes, taken from the pushes list when needed. So history grows by a constant amount per push and per pin. The record that `freeze` returns is stored the same way.
- A persist request made at submission is part of the record. The outcome of that write is the record's finish, so the writes list holds no entry for it ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).
- Output values are not history ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)). History names no file.
- Once a proposal has been idle (README, [How long records and values are kept](../README.md#how-long-records-and-values-are-kept)) for the retention period, days to weeks as the deployment sets it, its history is dropped as a whole. An output needed for longer is published (README, [Provenance and publication](../README.md#provenance-and-publication)).
- How a backend stores the lists is its choice. The in-process backend appends them to one event log, in memory or in a file of JSON lines.

## Alternatives considered

- **History defined as one event log, with records and labels as views**, indexes rebuilt from the log. The lists hold the same history. The log adds one order across all lists, and nothing outside storage reads that order. It would tie a hosted backend to a log.
- **History kept as long as the proposal.** That may be forever, a second record of what ran next to SciCat.
- **Each record dropped by age, keeping the older records that a kept record reads.** The trigger loop would reduce again every dataset whose records were dropped. It would need a memory of its own, such as a set of handled datasets per rule.
- **A proposal's history dropped a fixed time after its last record.** A trigger loop that sees no new dataset for that long loses its rule's records and then reduces every dataset again. An open client would lose the records it still uses.
- **The record of a state stored with every row, in storage that shares repeated rows.** Sharing saves bytes, but every layer above the store still handles every row, and that grows with the square of the number of pins.
- **Output values kept with their records.** This stores one output per state, and makes a catalogue of outputs next to SciCat, which is a non-goal.

## Consequences

- Dropping a proposal's history leaves no dangling reference. No other proposal reads its records (system story G5), and an idle proposal has no open client and no pending record.
- A running trigger loop keeps a client of its proposal open, so the proposal is not idle. A loop started for a proposal whose history was dropped reduces its datasets again.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps all its history.
- A client whose process ended without closing it keeps its proposal from being idle until the backend closes it (scipp/essapps#34).
- A backend that restarts does not know when its earlier clients ended, so it counts idle time from its start.
- The in-process backend never drops history.
- Its log appends one line per change, so a submission is stored whole or not at all. A line cut short by a crash is dropped when the file is read.
- Old event formats must stay readable as long as the log is kept.
- A backend that rebuilds its indexes of records from the log at start holds every record of its proposals in memory.
- A restart keeps history, not values, so pending work goes on only if what it reads was persisted ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).

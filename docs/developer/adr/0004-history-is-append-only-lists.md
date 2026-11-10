# ADR 0004: History is append-only lists, dropped per proposal once it is idle

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-11-10

## Context

A record names a spec, every parameter value, each written inline, a file, or a reference to an output, and its outputs.
A record never changes, only its status, which is set once.
Pushes are only added.
So history only grows until it is dropped.

The stories need four things from history:

- Records outlive the process of the workspace (B6, H2, H3). The trigger loop knows which files a rule has handled only from the records under the rule's label.
- A submission is stored whole or not at all.
- The order of the records under a label, and of the pushes into an accumulator, is kept.
- Persisted values are found again.

Two forces bound its size.

**Records of states repeat rows.** The plain request of state n holds the rows of the first n pushes, and the record of a freeze is the record of that request ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).
Story D7 pushes 300 runs and freezes once, so a record that listed its rows would hold the 300 rows a second time.
A read of a state makes no record.

**A proposal has no defined end.** Users need their records and outputs for days to weeks ([tensions](../../requirements/tensions.md), "Finding results again versus a second catalogue").

## Decision

History is four lists, each only appended to:

| List | One item per | Holds |
|---|---|---|
| records | record | ID, time, proposal, submitter, request, output names, label, member |
| accumulators | opened accumulator | ID, proposal, template, and the template's values typed with defaults filled in |
| finishes | finished record | record ID, status, failure message, the names of optional outputs that the workflow did not return |
| pushes | push into an accumulator | accumulator ID, one row per table |

- A record never changes. Its status is its finish, and a record without one is pending.
- The record of a freeze ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)) is stored as the accumulator and its number of pushes. Its rows are those of the first n pushes, taken from the pushes list when needed. So history grows by a constant amount per push and per freeze, and reads of a state add nothing.
- A persist request is part of the record. The outcome of the write is the record's finish ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).
- Output values are not history ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)). History names no file.
- Once a proposal has been idle ([`ess.apps.protocol`](../../../packages/essapps/src/ess/apps/protocol.py)) for the retention period, days to weeks as the deployment sets it, its history is dropped as a whole. An output needed for longer is published (README, [Provenance and publication](../README.md#provenance-and-publication)).
- How a deployment stores the lists is its choice. `ess.apps.Workspace` appends them to one event log, in memory or in a file of JSON lines.
- The changes to a proposal's records have one order: the *change feed*, the ID of a record at each of its items in the records and finishes. A client reads it from a position (`client.changes`), so that it reads only the records that changed. It holds no events, and neither accumulators nor pushes, so a workspace keeps this order however it stores the lists.

## Alternatives considered

- **History defined as one event log, with records and labels as views**, indexes rebuilt from the log. The lists hold the same history. The log adds one order across all lists, and outside storage only the change feed needs an order, of the records alone. It would tie a hosted workspace to a log.
- **History kept as long as the proposal.** That may be forever, a second record of what ran next to SciCat.
- **Each record dropped by age, keeping the older records that a kept record reads.** The trigger loop would reduce again every file whose records were dropped. It would need a memory of its own, such as a set of handled files per rule.
- **A proposal's history dropped a fixed time after its last record.** A trigger loop that sees no new file for that long loses its rule's records and then reduces every file again. A client still in use would lose the records it reads.
- **The record of a freeze stored with every row, in storage that shares repeated rows.** Sharing saves bytes, but every layer above the store still handles each row twice, once in its push and once in the record.
- **Output values kept with their records.** This stores one output per state, and makes a catalogue of outputs next to SciCat, which is a non-goal.

## Consequences

- Dropping a proposal's history leaves no dangling reference. No other proposal reads its records (system story G5), and an idle proposal gets no call and has no pending record.
- A running trigger loop calls its client while it waits for changes, so its proposal is not idle. A loop started for a proposal whose history was dropped reduces its files again.
- A proposal that is never idle, such as one whose automatic reduction runs all year, keeps all its history.
- A client whose process ended makes no call, so it does not keep its proposal from being idle.
- A workspace that restarts does not know when its proposal was last called, so it counts idle time from its start.
- `ess.apps.Workspace` never drops history.
- Its log appends one line per change, so a submission is stored whole or not at all. A line cut short by a crash is dropped when the file is read.
- Old event formats must stay readable as long as the log is kept.
- A workspace that rebuilds its indexes of records from the log at start holds every record of its proposal in memory.
- A restart keeps history, not values, so pending work goes on only if what it reads was persisted ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).

# ADR 0004: History is append-only lists, and a history no person holds is dropped once its proposal is idle

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-11-10

## Context

A record holds the ID of its spec, every parameter value, and the names of its outputs.
A record never changes, only its status, which is set once.
Pushes are only added.
So history only grows until it is dropped.

The stories need four things from history:

- Records outlive the process of the workspace (B6, H2, H3). The automatic reduction knows which rows it has reduced only from the records under the batch's label.
- A submission is stored whole or not at all.
- The order of the records under a label, and of the pushes into an accumulator, is preserved.
- Persisted values are found again.

Two forces bound its size.
The plain request of state n holds the rows of the first n pushes ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), so the record of a freeze after 300 pushes (story D7) would hold the 300 rows a second time.
And a proposal has no defined end, while users need their records for days to weeks ([tensions](../../requirements/tensions.md), "Finding results again versus a second catalogue").

## Decision

History is five lists, each only appended to: records, opened accumulators, starts, finishes, and pushes.
A record never changes. Its finish sets its status, and a record without one is pending.
The record of a freeze is stored as the accumulator and its number of pushes, so it takes a constant amount of history.
Output values are not history, and history refers to no file of the store.
The changes to a proposal's records have one order, which a client reads from a position (`client.changes`).
A history that a process holds without a person, such as the automatic reduction's, is dropped as a whole once its proposal has been idle for the retention period, days to weeks.
The framework deletes nothing in a folder that a person opened.
How a deployment stores the lists is its choice. `ess.apps.Workspace` appends them to one log of events.

## Alternatives considered

- **History defined as one event log, with records and labels as views.** Only the change feed needs an order, and it would tie a hosted workspace to a log.
- **A history held without a person stored as long as the proposal.** That may be forever, a second record of what ran next to SciCat.
- **Each record dropped by age, keeping the older records that a remaining record reads.** The automatic reduction would reduce again every row whose records were dropped.
- **A proposal's history dropped a fixed time after its last record.** An automatic reduction that sees no new file for that long would reduce every row again, and a client still in use would lose its records.
- **The record of a freeze stored with every row, in storage that shares repeated rows.** Every layer above the store still handles each row twice.
- **Output values stored with their records.** This stores one output per state, and makes a catalogue of outputs next to SciCat, which is a non-goal.

## Consequences

- Dropping a proposal's history leaves no dangling reference, since no other proposal reads its records (system story G5).
- A running automatic reduction calls its client at each step, so its proposal is not idle. A loop started for a proposal whose history was dropped reduces its rows again.
- A proposal that is never idle, such as one whose automatic reduction runs all year, retains all its history.
- A client whose process ended makes no call, so it does not keep its proposal from being idle.
- A workspace that restarts does not know when its proposal was last called, so it counts idle time from its start.
- An output needed after the history is dropped is published.

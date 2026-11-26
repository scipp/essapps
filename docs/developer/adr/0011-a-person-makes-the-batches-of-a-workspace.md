# ADR 0011: A person makes the batches of a workspace

- Status: accepted
- Deciders: Simon
- Date: 2026-11-18

## Context

The terms are those of [batch.md, "Terms"](../../../packages/essbatch/docs/batch.md#terms).

With one batch per workspace, a person cannot:

1. **End a series.** After the settings change at lunch, every row of the morning has status `settings changed`, and "Reduce them again" offers to reduce it with the new settings. Nothing marks those rows as finished.
2. **Work on two sets of runs.** A person wants to reduce yesterday's runs again while today's runs are reduced as they arrive, and to look at one without stopping the other.
3. **Give each row the output of another workflow**, such as a beam centre per sample. A cell can refer to an output of another record ([batch.md, "Chains"](../../../packages/essbatch/docs/batch.md#chains)), but code must fill that column.

How users will organise an experiment is not known.
So this decision restricts a person as little as it can.

## Decision

**A person makes a batch, gives it a title, and removes it. A manager, `BatchManager`, lists the batches of a workspace. A person ends a series by making the next batch with its own settings, and the batch before keeps its settings.**
[ADR 0012](0012-a-batch-is-a-table.md) decides what a batch holds, and [ADR 0013](0013-follow-adds-the-rows-not-considered.md) how rows come into it from a row source.

- **The manager holds the one `Batch` of each label.** Two writers of one batch's definitions would lose each other's changes.
- **The spec of a batch is fixed when the batch is made.** A person can change its title and description.
- **A label is used once.** The manager rejects a label that has definitions or records, so the records of a removed batch never appear as results of another batch. Removing a batch removes its definitions, and its records stay, as all history does ([ADR 0004](0004-history-is-append-only-lists.md)).
- **Batches are independent.** Two batches may hold the same row, and each reduces it under its own label. Nothing checks that the batches cover every row or do not overlap.
- **Corrections of runs belong to the workspace.** Every batch reads the files through one file source, and a person corrects a run on the page of any batch, or of none.
- **At most one batch follows.** Making a batch that follows, or `intake.follow(label)`, first stops the batch that followed before.
- **The server owns the timer of Follow.** Whoever serves the pages calls the step, and without a page, `essbatch follow` does. `Intake` has no thread, timer or lock.
- **A page shows one batch, named in its URL.** The server holds no current batch. Reductions are records of the workspace, and Follow runs in the server, so neither depends on what a browser shows.

## Alternatives considered

- **One batch per workspace, with a range of rows shown** ([#223](https://github.com/scipp/essapps/issues/223)). It bounds the cost of the page and solves neither problem 1 nor 2: the batch still has one set of settings.
- **Each row holds all its settings.** A page would have to show and edit settings per row. A batch per series does the same with the settings a batch already has.
- **Batches that split the rows between them.** It is a rule to enforce, and it forbids reducing one series with two sets of settings to compare them.
- **The manager owns the timer of Follow.** The manager would get a thread, a timer and a lock next to the definitions it writes.
- **Follow as a flag of each batch.** A run would be reduced once per batch that follows. A single value can become a set later, while a flag per batch is hard to take back.

## Consequences

- The rows of the batch before keep their status when the next series gets other settings.
- All batches share the workspace's queue, so a long reduction in one batch delays the batch that follows.
- Problem 3 stays open ([#222](https://github.com/scipp/essapps/issues/222)). Chains remain for batches made in code. A batch that a person makes on the page has none.
- Only the batch that follows lists its source at each step, so the cost of a step does not grow with the number of batches.

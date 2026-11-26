# ADR 0011: A person makes the batches of a workspace

- Status: accepted
- Deciders: Simon
- Date: 2026-11-18

## Context

*Spec*, *record* and *binding* are the words of [README.md](../README.md); *row*, *key*, *result*, *template* and *outdated* are those of [batch.md, "Terms"](../../../packages/essbatch/docs/batch.md#terms).
These are used here:

- A *workspace* is a `Client` (the specs it binds, the files, the records) and its definitions; for a folder workspace, one folder.
- A *batch* is one spec reduced for the rows of a table, with settings shared by all rows. ADR 0012 says what it holds.
- The *label* of a batch names it and its results: a row's result is the newest record under the label with the row's key.
- The *header* of a batch is its title and its description.
- The *definitions* are what a workspace keeps besides files and records: per label, the header, the settings and the table of a batch, and the `source.json` of a batch made from a row source.
- A *row source* (`RowSource`), the *source* for short, is code of the application that makes rows from the files.
- *Follow* reduces a batch as its runs arrive, by a *step* every few seconds. ADR 0013 says what a step does.

One batch per workspace leaves three things a person cannot do:

1. **End a series.** If the settings change after lunch, every row of the morning reads `settings changed`, reduced with earlier settings, and "Reduce them again" offers to reduce it with the new settings. Nothing says that those rows are finished.
2. **Work on two sets of runs.** A person wants to reduce yesterday's runs again while the runs measured now are reduced as they arrive, and to look at one without stopping the other.
3. **Give each row the output of another workflow.** A beam centre per sample needs two batches and a source written for the pair. A cell can name an output of another record ([batch.md, "Chains"](../../../packages/essbatch/docs/batch.md#chains)), but the column is filled in code.

So this decision restricts a person as little as it can.

## Decision

A person makes a batch, names it, and removes it.
A *manager* lists the batches of a workspace.
A series ends where a person makes the next batch, with settings of its own; the batch before keeps its settings.

### The manager

- The manager holds the one `Batch` of each label in its process, since two writers of one definition lose each other's changes.
- A person changes the title and the description. The spec is fixed when the batch is made; ADR 0012 says how the settings and the table change.
- Removing a batch removes its definitions. Its records stay in the workspace, as all history does ([ADR 0004](0004-history-is-append-only-lists.md)).

### A label is made by the manager and used once

The manager refuses a label that has definitions or records.
So the records of a removed batch never show as results of another batch.

### What the workspace keeps

The corrections of runs belong to the workspace and to no batch: every batch reads the files through one file source, and a person corrects a run on the page of any batch, or of none.

Batches are independent.
Two may hold the same row, with the same spec or another, and each reduces it under its own label.
Nothing checks that the batches cover every row or that they do not overlap.

### One batch follows

- Making a batch that follows, or `intake.follow(label)`, stops the batch that followed before, first, so at most one batch follows.
- Whoever serves the pages owns the timer that calls `step`; without a page, the command `essbatch follow` does. `Intake` has no thread, no timer and no lock.

### A page shows one batch, named in its URL

The server holds no "current batch".
Reductions are records of the workspace and Follow is a thread of the server, so neither depends on what a browser shows.
A person starts a long reduction in one batch, looks at another, and comes back.

## Alternatives considered

- **One batch per workspace, with a range of rows shown** ([#223](https://github.com/scipp/essapps/issues/223)). It bounds the cost of the page and solves neither problem 1 nor 2: the batch still has one set of settings.
- **Each row keeps all its settings.** A page would have to show and edit settings per row. A batch per series gives the same with the settings a batch already has.
- **Batches that split the rows between them.** It is a rule to enforce, and it forbids reducing one series with two sets of settings to compare them.
- **The manager owns the timer of Follow.** The manager would get a thread, a timer and a lock next to the definitions it keeps.
- **Follow as a flag of each batch.** Each batch that follows adds and reduces every new run, so a run is reduced once per such batch. One value can become a set later; a flag per batch is hard to take back.

## Consequences

- A person ends a series by making the next batch. The settings of the batch before stay as they are, so its rows do not read `settings changed` when the next series gets other settings.
- A person looks at one batch while another runs.
- Problem 3 stays as it is for batches made in code, and is not available for batches that a person makes.
- Only the batch that follows lists its source at each step, so the cost of a step does not grow with the number of batches. A cache of the metadata of each file belongs in the file source, below the batches.

# ADR 0013: Follow adds the rows that a batch has not seen

- Status: accepted
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *template*, *blank*, *row*, *key*, `set_table` are the words of [ADR 0012](0012-a-batch-is-a-table.md): a batch is a template and a table, which only `set_table` changes and which is not computed again from the source at each read.
*Source*, *Follow* and *step* are those of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md): a row source, the source for short, is code of the application that makes rows from the files, and a batch that follows is reduced as its files arrive, by a step every few seconds.

ADR 0012 leaves open how a batch is amended from the files, with two conditions: what amends a batch uses the table as a person does, and what it needs to know is its own state next to the batch.

A source does three things to a batch today after it is made:

| A scan | Example |
|---|---|
| adds rows as files arrive | Follow |
| fills a cell that was empty | a sample run measured before its transmission run |
| changes a cell that has a value | a transmission run measured again: the source gives the rows of its sample the last one |

To do the third, a batch has to know who wrote each cell. To do the first without a memory, a row that a person removed comes back at the next step.

## Decision

**A batch made from a source keeps the name of the source and the keys it has seen. A step of Follow adds the rows of the source whose keys are not seen, once they are complete, and never changes a row that the table has.**

### A row that a person removes stays away

Its key is seen, so no step adds it again. The table keeps no mark for it (ADR 0012).

### A step never changes a row that the table has

It sets no cell and removes no row. A transmission run measured again changes no earlier row; a person sets the cell.

A source whose row grows, such as the runs of a sample so far, gives it a new key, so the grown list is a new row.

What removes a bad run from the later rows is a correction of the run, which the source reads.

### A row waits until it is complete

A row is added once it has a value in each of `batch.source_columns`. Until then it is *waiting*: the source gives it again at every step.

### Making a batch from a source

No scope is kept: "from run 60339 on" is the choice of rows at making, and what arrives later is what Follow adds.

### What a step reduces

The rows that are ready and have no record under the label of the batch. A row with a result is never reduced by a step, also not one that is outdated; a person reduces it.

### The next batch is a batch made from the source

No row moves from one batch to another. A person who wants the runs from 60400 on in a batch of their own makes it with Follow.

The earlier batch keeps its rows, also those from 60400 on, until a person removes them.

### A batch follows from the time it is made or told to follow, until it is stopped

- A batch that follows again adds at its next step every row of its source that is complete and not seen: those that arrived while it did not follow, also those that another batch took meanwhile. A row left out when it was made, or removed by a person, stays out, since its key is seen.
- A batch that follows has no last row. With one, the rows complete between the last row chosen and the making would be seen and never added, and the rows that arrive later would be.
- On the page, the next batch is one form, "New batch" with Follow. Follow is a state with one action, "Stop following", not a switch: the page offers no action to let a batch follow again.

## Alternatives considered

- **Marks in the table**: a removed row kept with a mark, and who wrote each cell. The first version of ADR 0012. The table then carries what only a step needs.
- **One mark, the highest run number seen.** It misses a file that is listed late, and it cannot let one row wait while later rows are added.
- **A mark over files.** A source makes a row from several files: a transmission run is a new file and no new row, and it completes a row whose own file is old. The step compares rows either way, so the keys of rows are what it remembers.
- **Add a row at once and fill its empty cells later.** It needs no memory of what is incomplete. But it fills a cell again that a person emptied, and an empty cell of a parameter that takes `None` is a value (ADR 0012), so "not listed yet" and "none" cannot be told apart.
- **A step changes cells that have a value**, as today, so that a transmission run measured again reaches the earlier rows and they are reduced again. It needs to know who wrote each cell, and results change with nobody asking. A person can do it for the rows they choose.
- **A scope kept in the header** (ADR 0011): a slice of an index that a source gives each row. With `seen_keys`, the start of the slice is the choice of rows at making, and it has no end: the next batch is made from the source and follows in its place. The index, its type, and the conversion of the ends go.
- **Follow as a switch on the page of each batch**, in place of Follow in "New batch". The next batch would be two actions: make it, then turn Follow on. And a person who turns it on for an earlier batch may not expect that it takes the rows that another batch took since.
- **A row is added once it can be reduced**, in place of once it is complete. A column that the source does not fill would hold the row back for ever, and a parameter that takes `None` would let a row through before its file is listed.

## Consequences

- An incomplete row is not in the table. The page says how many rows are incomplete, and a person can add them.
- A transmission run measured again reaches no earlier row, and a correction of a run changes no row that the table has.
- A stop by mistake is undone with `intake.follow(label)` or `essbatch follow -l LABEL`, not on the page. On the page, a person adds the later rows with "Add rows from the runs", or makes a batch from the source, with Follow, that starts at the first row the stopped batch lacks.
- A person who wants to try settings on a few rows first makes a batch without Follow, then the batch that follows with "Start with the settings of" the first. The rows tried are reduced again in the second batch.
- A batch made with Follow is stepped within seconds: the rows taken at making are reduced with the settings it was made with.
- `seen_keys` grows by one key for each row that the source ever gave.
- A source whose keys change, by a change of its code, gives every row a key that is not seen, and a step adds them all.

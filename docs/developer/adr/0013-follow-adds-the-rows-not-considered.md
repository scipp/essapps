# ADR 0013: Follow adds the rows that a batch has not seen

- Status: accepted
- Deciders: Simon
- Date: 2026-11-19

## Context

The terms are those of [batch.md, "Terms"](../../../packages/essbatch/docs/batch.md#terms).
[ADR 0012](0012-a-batch-is-a-table.md) makes a batch a template and a stored table, which only `set_table` changes.
It leaves open how Follow amends a batch from the files.

Before ADR 0012, a scan of the source did three things to a batch after it was made:

| A scan | Example |
|---|---|
| adds rows as files arrive | Follow |
| fills a cell that was empty | a sample run measured before its transmission run |
| changes a cell that has a value | a transmission run measured again: the rows of its sample get the new one |

To change a cell, a batch has to know who wrote each cell.
To add rows without a memory, a step brings back a row that a person removed.

## Decision

**A batch made from a source stores the name of the source and the keys it has seen. A step of Follow adds the rows of the source whose keys are not seen, once they are complete, and never changes a row that the table has.**

- **A removed row stays away.** Its key is seen, so no step adds it again, and the table holds no mark for it. "Add rows from the runs" brings it back.
- **A row is added once it is complete**, with a value in each of `batch.source_columns`. A sample run measured before its transmission run is added when the transmission run is listed.
- **A step sets no cell and removes no row.** A transmission run measured again changes no earlier row, and a person sets the cell. A correction of a run, which the source reads, removes a bad run from later rows. A source whose row grows, such as the runs of a sample so far, gives the grown list a new key.
- **The choice of rows at making replaces a scope.** "From run 60339 on" is the choice of rows when the batch is made, and Follow adds the rows that arrive later.
- **A step reduces the rows that are ready and have no record** under the batch's label. A person reduces a row that has a result, also an outdated one.
- **The next batch is made from the source.** No row moves between batches. The earlier batch keeps its rows until a person removes them.
- **A batch that follows has no last row.** With one, the rows complete between the last row and the making would be seen and never added, while later rows would be added.
- **On the page, Follow is chosen in "New batch" and has one action, "Stop following".** The page has no action to let a batch follow again.

## Alternatives considered

- **Marks in the table**: a removed row kept with a mark, and who wrote each cell. The table would hold what only a step needs.
- **One mark, the highest run number seen.** It misses a file listed late, and it cannot hold one row back while later rows are added.
- **A mark over files.** A transmission run is a new file and no new row, and it completes a row whose own file is old. The step compares rows either way, so it remembers the keys of rows.
- **Add a row at once and fill its empty cells later.** It would fill a cell again that a person emptied. For a parameter that accepts `None`, an empty cell is a value (ADR 0012), so "not listed yet" and "none" would look the same.
- **A step changes cells that have a value**, so that a transmission run measured again reaches the earlier rows. It needs to know who wrote each cell, and results change without anyone asking. A person can do it for the rows they choose.
- **A scope stored in the header**: a slice of an index that the source assigns to each row. The choice of rows at making sets its start. It needs no end, since the next batch is made from the source and follows instead.
- **Follow as a switch on the page of each batch.** The next batch would take two actions. A person who turns it on for an earlier batch may not expect it to add the rows that another batch added since.
- **A row is added once it can be reduced**, instead of once it is complete. A column that the source does not fill would hold the row back forever, and a parameter that accepts `None` would let a row in before its file is listed.

## Consequences

- An incomplete row of the source is not in the batch table. The page shows how many there are, and a person can add them.
- A batch that follows again adds at its next step every complete row it has not seen, also rows that another batch added meanwhile.
- A stop by mistake is undone with `intake.follow(label)` or `essbatch follow -l LABEL`. On the page, a person adds the later rows with "Add rows from the runs", or makes a new batch with Follow.
- To try settings on a few rows first, a person makes a batch without Follow, then the batch that follows with "Start with the settings of" the first. The rows tried are reduced again in the second batch.
- A batch made with Follow is stepped within seconds, so the rows chosen at making are reduced with the settings it was made with.
- `seen_keys` grows by one key for each row that the source ever made.
- A source whose keys change, by a change of its code, assigns every row a key that is not seen, and a step adds them all.

# ADR 0013: Follow adds the rows that a batch has not considered

- Status: accepted (#236)
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *template*, *blank*, *row*, *key*, `put` are the words of [ADR 0012](0012-a-batch-is-a-table.md): a batch is a template and a table, and only a person changes the table.
*Source*, *Follow* and *step* are those of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md): a source is code of the application that makes rows from the files, and a batch that follows is reduced as its files arrive, by a step every few seconds.

ADR 0012 leaves open how a batch is amended from the files, with two conditions: what amends a batch uses the table as a person does, and what it needs to know is its own state next to the batch.

A source does three things to a batch today after it is made:

| A scan | Example |
|---|---|
| adds rows as files arrive | Follow |
| fills a cell that was empty | a sample run measured before its transmission run |
| changes a cell that has a value | a transmission run measured again: the source gives the rows of its sample the last one |

To do the third, a batch has to know who wrote each cell. To do the first without a memory, a row that a person removed comes back at the next step.

## Decision

**A batch made from a source keeps the name of the source and the keys it has considered. A step of Follow adds the rows of the source whose keys are not considered, once the source has filled them, and never changes a row that the table has.**

```python
def step(batch, source, considered):
    given = rows_of(batch.client, source)                  # every row the source makes from the files now
    table = batch.frame()
    filled = [c for c in source.fills if c in table.columns]
    is_new = ~given.index.isin(considered) & ~given.index.isin(table.index)
    is_ready = given[filled].notna().all(axis=1)
    new = given[is_new & is_ready].reindex(columns=table.columns)
    batch.put(pd.concat([table, new]))                     # ADR 0012: the table is changed with `put`
    considered |= set(new.index) | (set(given.index) & set(table.index))   # kept, after the table is
    waiting = given.index[is_new & ~is_ready]              # shown on the page, not kept
    batch.reduce(ready_without_result(batch.state()))
```

### What is kept

```
definitions/labels/20261118-130204/source.json     {"source": "sample-runs", "considered": ["60337", "60338", "60339"]}
definitions/following.json                         the label of the batch that follows, or none (ADR 0011)
```

- A key is *considered* by a batch once a row of that key was put into the batch from the source, or was filled and left out when the batch was made.
- A key of the source that the table has is considered, whoever added the row, and each step keeps it so. So a step that stops between writing the table and writing `source.json` adds no row twice, and a row that a person added by hand with a key that the source gives later stays away once removed.
- A batch made without a source, such as one from a table file, has no `source.json` and cannot follow.

### A row that a person removes stays away

Its key is considered, so no step adds it again. The table keeps no mark for it (ADR 0012).

### A step never changes a row that the table has

It sets no cell and removes no row. A transmission run measured again changes no earlier row; a person sets the cell.

### A row waits until the source has filled it

A source says which columns it fills (`Source.fills`, such as `sample` and `sample_transmission`). A row is added once it has a value in each of these that is a column of the batch. Until then it is *waiting*: the source gives it again at every step, and the page says how many rows wait.

- A sample run measured before its transmission run is added when the transmission run is listed.
- A row is added whatever its other columns hold. A column that the source does not fill is empty in a new row, and the row is incomplete if the template needs it.
- A row that never gets its cell, such as a sample without a transmission run, waits until a person adds it (below).
- The step takes only the columns that the table has. A column of the source that the batch lacks is left out, so a step never changes the blanks of the template.

### Making a batch from a source

`create` lists the source once, and chooses from the rows that are filled:

```python
manager.create('loki-iofq', title='Afternoon', source='sample-runs',
               choose=lambda given: given[given.run >= 60339])

# in create:
given = rows_of(client, source)                  # one listing
arrived = given[filled]                          # the rows that have a value in each of source.fills
table = arrived if choose is None else choose(arrived)
considered = arrived.index                       # the rows taken and the rows left out
```

- A row that waits when the batch is made is neither taken nor considered. It keeps waiting, and Follow adds it once it is filled.
- `choose` gives rows of `arrived`, with its columns or fewer; `create` refuses anything else. Without `choose`, every filled row is taken.
- A batch made from a source takes no `rows`: its rows come from the one listing, so no file listed in between is considered without being seen.

No scope is kept: "from run 60339 on" is the choice of rows at making, and what arrives later is what Follow adds.

### A person adds rows of the source

"Add rows from the runs" shows the rows that the source gives and the table lacks: the rows that wait, the rows left out at making, the rows a person removed, and the rows filled while the batch did not follow. A person sees what they take, so the rows taken may be incomplete or waiting. The person picks rows, and they are put into the table as the source gives them now. This is also how a removed row is brought back.

### What a step reduces

The rows that are ready and have no record under the label of the batch. A row with a result is never reduced by a step, also not one that is outdated; a person reduces it. A row whose record failed or was cancelled, also by a restart, has a record and is left to a person, as today.

The rule is on the state of the rows, as in ADR 0011. So in a batch that follows, a row that a person adds is reduced as soon as it is complete.

### The next batch is a batch made from the source

No row moves from one batch to another. A person who wants the runs from 60400 on in a batch of their own makes it, and turns Follow on for it:

```python
label = manager.create('loki-iofq', title='Evening', source='sample-runs',
                       choose=lambda given: given[given.run >= 60400],
                       settings=manager.batch('afternoon').settings)
manager.follow(label)                                # the batch 'afternoon' follows no longer
```

The earlier batch keeps its rows, also those from 60400 on, until a person removes them. It has considered the keys it has seen, and takes the rows that arrive later only if it follows again.

### At most one batch follows

As in ADR 0011: the manager keeps the label, a restart resumes, and removing that batch ends Follow. Since `considered` is kept, the first step after a restart, or after Follow was off for a while, adds the rows that arrived in between.

## Example

A batch made from `sample-runs` with the rows from run 60339 on, which follows.

| | What happens | Table | `considered` |
|---|---|---|---|
| 1 | the batch is made; runs 60337 to 60340 are listed | 60339, 60340 | 60337, 60338, 60339, 60340 |
| 2 | sample run 60341 is listed; its transmission run is not | unchanged; 1 row waits | unchanged |
| 3 | transmission run 60342 is listed | 60341 is added and reduced | + 60341 |
| 4 | a person removes row 60340 | 60339, 60341 | unchanged, so 60340 is not added again |
| 5 | the application is restarted; sample run 60343 arrived meanwhile, with its transmission run | 60343 is added at the first step | + 60343 |
| 6 | the transmission of the sample of 60339 is measured again | unchanged: row 60339 keeps its cell | unchanged |

## Alternatives considered

- **Marks in the table**: a removed row kept with a mark, and who wrote each cell. The first version of ADR 0012. The table then carries what only a step needs.
- **One mark, the highest run number considered.** It misses a file that is listed late, and it cannot let one row wait while later rows are added.
- **A mark over files.** A source makes a row from several files: a transmission run is a new file and no new row, and it completes a row whose own file is old. The step compares rows either way, so the keys of rows are what it remembers.
- **Add a row at once and fill its empty cells later.** It needs no memory of what waits. But it fills a cell again that a person emptied, and an empty cell of a parameter that takes `None` is a value (ADR 0012), so "not listed yet" and "none" cannot be told apart.
- **A step changes cells that have a value**, as today, so that a transmission run measured again reaches the earlier rows and they are reduced again. It needs to know who wrote each cell, and results change with nobody asking. A person can do it for the rows they choose.
- **A scope kept in the header** (ADR 0011): a slice of an index that a source gives each row. With `considered`, the start of the slice is the choice of rows at making, and it has no end: the next batch is made from the source and follows in its place. The index, its type, and the conversion of the ends go.
- **A row is added once it can be reduced**, in place of once the source has filled it. A column that the source does not fill would hold the row back for ever, and a parameter that takes `None` would let a row through before its file is listed.

## Consequences

- `Scope`, `Row.index`, `Source.index_type`, `Source.index_title`, `Header.scope`, `Header.source` and `manager.change(scope=...)` go. `Source.fills` stays, with a second use: it says when a row is filled.
- The rule of a step is "ready and without a record". `RowState.changed_by_hand`, the columns `at` of `Automatic`, and the exception for cells set by hand go.
- A row that only waits is not in the table. The page says how many rows wait, and a person can add them.
- A transmission run measured again reaches no earlier row, and a correction of a run changes no row that the table has.
- A batch that is followed again after days takes every row that arrived since. That is what `considered` says; a person who wants a fresh start makes a batch.
- `considered` grows by one key for each row that the source ever gave: some tens of bytes per row.
- A source whose keys change, by a change of its code, gives every row a key that is not considered, and a step adds them all.
- A step still asks the source for its rows of all files. Its cost is as today ([#223](https://github.com/scipp/essapps/issues/223)).
- What ADR 0011 says of the scope, of the source in the header, and of what a step reduces is replaced by this. "Start the next batch here" goes: no rule moves rows between batches.

## Open

- **The command.** `essapps-batch reduce --source NAME` makes a batch from every row of a source. Whether a later run of the command does one step is not decided.
- **A row that waits for ever.** The page could say what each waiting row lacks. Not needed for the decision.
- **Several workflows on one batch** (#222): a workflow that fills cells is the second thing besides a person that would write to the table. This ADR does not cover it.

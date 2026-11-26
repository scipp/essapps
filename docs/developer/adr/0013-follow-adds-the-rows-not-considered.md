# ADR 0013: Follow adds the rows that a batch has not seen

- Status: accepted (#236)
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

```python
def step(batch, source, seen_keys):
    given = rows_of(batch.client, source)                  # every row the source makes from the files now
    table = batch.table()
    columns = list(batch.source_columns)                   # source.source_columns as at making; each is a column of the table
    is_new = ~given.index.isin(seen_keys) & ~given.index.isin(table.index)
    is_ready = given[columns].notna().all(axis=1)
    new = given[is_new & is_ready].reindex(columns=table.columns)
    batch.set_table(pd.concat([table, new]))               # ADR 0012: the table is changed with `set_table`
    seen_keys |= set(new.index) | (set(given.index) & set(table.index))   # kept, after the table is
    incomplete = given.index[is_new & ~is_ready]           # shown on the page, not kept
    batch.reduce(not_reduced(batch.state()))              # ready, and without a result
```

### What is kept

```
definitions/labels/20261118-130204/source.json     {"source": "sample-runs", "seen_keys": ["60337", "60338", "60339"], "follows": true}
```

- A key is *seen* by a batch once a row of that key was put into the batch from the source, or was complete and left out when the batch was made.
- A key of the source that the table has is seen, whoever added the row, and each step keeps it so. So a step that stops between writing the table and writing `source.json` adds no row twice, and a row that a person added by hand with a key that the source gives later stays away once removed.
- A batch made without a source, such as one from a table file, has no `source.json` and cannot follow.

### A row that a person removes stays away

Its key is seen, so no step adds it again. The table keeps no mark for it (ADR 0012).

### A step never changes a row that the table has

It sets no cell and removes no row. A transmission run measured again changes no earlier row; a person sets the cell.

A source whose row grows, such as the runs of a sample so far, gives it a new key, so the grown list is a new row:

```
after run 2:  si@2      runs=[2]
after run 4:  si@2,4    runs=[2, 4]      # si@2 is no longer given; the table keeps it
after run 3:  si@2,3,4  runs=[2, 3, 4]   # listed after run 4, still a new key
```

The key of a row that grows must name its list, since a step adds a row only under a key that the batch has not seen; for a long series an application would use a digest of the list.

What removes a bad run from the later rows is a correction of the run, which the source reads.

### A row waits until it is complete

A source says which columns it fills (`RowSource.source_columns`, such as `sample` and `sample_transmission`), and a batch made from it keeps them as `batch.source_columns`. A row is added once it has a value in each of `batch.source_columns`. Until then it is *waiting*: the source gives it again at every step, and the page says how many rows wait.

- A sample run measured before its transmission run is added when the transmission run is listed.
- A row is added whatever its other columns hold. A column that the source does not fill is empty in a new row, and the row is incomplete if the template needs it.
- A row that never gets its cell, such as a sample without a transmission run, waits until a person adds it (below).
- The step takes only the columns that the table has. A column of the source that the batch lacks is left out, so a step never changes the blanks of the template.

### Making a batch from a source

`create` lists the source once, and a person chooses the table of the batch from the rows that are complete:

```python
intake.create('loki-iofq', title='Afternoon', source='sample-runs',
              choose=Range(first='60339'), parameter_columns=['thickness'],
              follow=True)

# in create:
given = rows_of(client, source)                  # one listing
arrived = given[complete]                        # the rows that have a value in each of source.source_columns
table = arrived if choose is None else choose(arrived)
seen_keys = arrived.index                       # the rows taken and the rows left out
```

- A row that is incomplete when the batch is made is neither taken nor seen. It stays incomplete, and Follow adds it once it is complete, if the batch follows.
- A person picks a range of `arrived`: `Range(first, last, later)`, a value that `create` calls as `choose` and reads, each end open if not given, or no row now with `later`. `parameter_columns` adds an empty column for each parameter named, such as the thickness.
- In code, `choose` may be any function that gives rows of `arrived`, with any columns, such as `lambda given: given[given.run >= 60339]`; `create` refuses a key that `arrived` lacks. Without `choose`, every complete row is taken as the source gives it.
- A batch made from a source takes no `rows`: its rows come from the one listing, so no file listed in between is seen without being seen.

No scope is kept: "from run 60339 on" is the choice of rows at making, and what arrives later is what Follow adds.

### A person adds rows of the source

"Add rows from the runs" shows the rows that the source gives and the table lacks: the incomplete rows, the rows left out at making, the rows a person removed, and, in a batch that does not follow, the rows complete after it was made or after it stopped. A person sees what they take, so the rows taken may be incomplete or waiting. The person picks rows, and they are put into the table as the source gives them now. This is also how a removed row is brought back.

### What a step reduces

The rows that are ready and have no record under the label of the batch. A row with a result is never reduced by a step, also not one that is outdated; a person reduces it. A row whose record failed or was cancelled, also by a restart, has a record and is left to a person, as today.

The rule is on the state of the rows, as in ADR 0011. So in a batch that follows, a row that a person adds is reduced as soon as it is complete.

### The next batch is a batch made from the source

No row moves from one batch to another. A person who wants the runs from 60400 on in a batch of their own makes it with Follow:

```python
intake.create('loki-iofq', title='Evening', source='sample-runs',
              choose=Range(first='60400'),
              settings=manager.batch('afternoon').settings,
              follow=True)          # the batch 'afternoon' stops following
```

The earlier batch keeps its rows, also those from 60400 on, until a person removes them. It has seen the keys it was given, and takes no row that arrives later while it does not follow.

A batch of only the rows that arrive later takes no row at making. Every row complete then is seen, so no row is reduced again:

```python
intake.create('loki-iofq', title='Night', source='sample-runs',
              choose=Range(later=True), follow=True)
```

### A batch follows from the time it is made or told to follow, until it is stopped

A batch made from a source follows from its making with Follow, or from `intake.follow(label)`. A batch that follows can be stopped.

```python
intake.create(..., source='sample-runs', follow=True)    # follows; the batch that followed before stops
intake.following                                         # its label
intake.stop()                                            # none follows
intake.follow('afternoon')                               # follows again; the batch that followed before stops
```

```sh
essbatch follow -l afternoon                        # the same from the command, then steps until Ctrl-C, or --steps N
essbatch stop                                       # none follows
```

- At most one batch follows, as in ADR 0011: `Intake` keeps whether a batch follows in its `source.json`, a restart resumes, and removing that batch ends Follow. Since `seen_keys` is kept, the first step after a restart adds the rows that arrived in between.
- A batch made without a source cannot follow: only `intake.create`, which takes a source, takes `follow=True`, and `intake.follow` refuses a batch without one.
- A batch that follows again adds at its next step every row of its source that is complete and not seen: those that arrived while it did not follow, also those that another batch took meanwhile. A row left out when it was made, or removed by a person, stays out, since its key is seen.
- A batch that follows has no last row. With one, the rows complete between the last row chosen and the making would be seen and never added, and the rows that arrive later would be. The range that a person picks is a value, `Range(first, last, later)`, which `Intake` reads: `create(follow=True)` with a `Range` that has a `last` is refused there. The page leaves the end of the range open while Follow is ticked, to say so before a person sends the form; `essbatch reduce --source` makes no batch that follows, and `essbatch follow` lets one follow later. A `choose` that is a function may pick any rows, also with Follow: the rows were chosen in code, and the complete rows that it leaves out are seen, so Follow adds only the rows that arrive later.
- Nothing is kept that says a batch has followed. A batch made without Follow and a batch that was stopped are in the same state, and `intake.follow` writes `"follows": true` to either.
- On the page, the next batch is one form, "New batch" with Follow. Follow is a state with one action, "Stop following", not a switch: the page offers no action to let a batch follow again.

## Example

A batch made from `sample-runs` with the rows from run 60339 on, with Follow.

| | What happens | Table | `seen_keys` |
|---|---|---|---|
| 1 | the batch is made; runs 60337 to 60340 are listed | 60339, 60340 | 60337, 60338, 60339, 60340 |
| 2 | sample run 60341 is listed; its transmission run is not | unchanged; 1 row incomplete | unchanged |
| 3 | transmission run 60342 is listed | 60341 is added and reduced | + 60341 |
| 4 | a person removes row 60340 | 60339, 60341 | unchanged, so 60340 is not added again |
| 5 | the application is restarted; sample run 60343 arrived meanwhile, with its transmission run | 60343 is added at the first step | + 60343 |
| 6 | the transmission of the sample of 60339 is measured again | unchanged: row 60339 keeps its cell | unchanged |
| 7 | a person makes the batch "Evening" with Follow; sample run 60344 arrives later | unchanged: 60344 goes to "Evening" | unchanged |

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

- `Scope`, `Row.index`, `Source.index_type`, `Source.index_title`, `Header.scope`, `Header.source` and `manager.change(scope=...)` go. `RowSource.source_columns` stays, with a second use: it says when a row is complete.
- The rule of a step is "ready and without a record". `RowState.changed_by_hand`, the columns `at` of `Automatic`, and the exception for cells set by hand go.
- An incomplete row is not in the table. The page says how many rows are incomplete, and a person can add them.
- A transmission run measured again reaches no earlier row, and a correction of a run changes no row that the table has.
- A stop by mistake is undone with `intake.follow(label)` or `essbatch follow -l LABEL`, not on the page. On the page, a person adds the later rows with "Add rows from the runs", or makes a batch from the source, with Follow, that starts at the first row the stopped batch lacks.
- A person who wants to try settings on a few rows first makes a batch without Follow, then the batch that follows with "Start with the settings of" the first. The rows tried are reduced again in the second batch.
- A batch made with Follow is stepped within seconds: the rows taken at making are reduced with the settings it was made with.
- `seen_keys` grows by one key for each row that the source ever gave: some tens of bytes per row.
- A source whose keys change, by a change of its code, gives every row a key that is not seen, and a step adds them all.
- A step still asks the source for its rows of all files. Its cost is as today ([#223](https://github.com/scipp/essapps/issues/223)).
- What ADR 0011 says of the scope, of the source in the header, and of what a step reduces is replaced by this. "Start the next batch here" goes: no rule moves rows between batches.

## Open

- **A row that waits for ever.** The page could say what each incomplete row lacks. Not needed for the decision.
- **Several workflows on one batch** (#222): a workflow that fills cells is the second thing besides a person that would write to the table. This ADR does not cover it.

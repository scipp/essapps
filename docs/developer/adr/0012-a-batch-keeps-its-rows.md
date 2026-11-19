# ADR 0012: A batch keeps its rows

- Status: proposed (#236)
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *label*, *header*, *scope*, *source*, *definitions* are the words of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md).
*Row*, *key*, *cell*, *scan*, *edit*, *result* are those of [batch.md, "Terms"](../../../packages/essapps/docs/batch.md#terms).

Today the rows of a batch are stored nowhere. They are computed at each scan, and only what a person set on top is kept:

```python
scan = source.rows(files with corrections)       # code of the application
rows = scan in scope, with edits on top          # edits.json: added, edited, cleared, removed, by key
```

So a batch is defined by how its rows are made, and the table a person sees is the output of that recipe now. Five problems follow.

1. **The table needs the files.** If the file mount is not available, the scan gives no rows, and the table is empty. The results are in the workspace, but no row shows them. If the listing raises, the page of the batch cannot be shown.
2. **Removing files removes rows.** A row whose file is gone disappears, also one that was reduced or edited. Its edits are kept and apply to nothing.
3. **A change of the source's code changes every batch**, also one that was finished last week.
4. **The model has no table to put a table in.** The command takes one (`essapps-batch reduce iofq.yaml runs.csv`), and the page loads and saves one. Both are translated into "cells set by hand on top of the scan", in four structures of different shape (`added`, `edited`, `cleared`, `removed`) and a marker for a cleared cell. The origin of a cell is worked out again in six places.

5. **A cell is held in two forms.** A cell is a stand-in, never a resolved value: a file is named by an identity, an output by the ids of its record and output. But the stand-in is a pydantic object if a source or the page made it (`FileRef(file='run:60339')`), and plain data if a table file did (`{'@ref': 'file', 'file': 'run:60339'}`). Four functions convert between the forms (`_values`, `with_named_files`, `write_cell`, `read_cell`) and do not agree: a table file loaded back unchanged marks a cell that holds a reference to an output as set by hand.

A person thinks of a batch as a table: they edit its cells, remove its rows, reduce its rows and read their results. All of that refers to rows, so the rows must last as long as what refers to them.

## Decision

**A batch keeps its rows, as plain data. The source and the scope are what fills the table, and only a person removes a row.**

### What is kept

One table per batch, `rows.json`, in place of `edits.json`. Per row:

```python
key       '60339'
index     60339                                   # what the scope compares
info      {'name': 'D2O buffer'}
source    {'sample': 'run:60339', 'transmission': 'run:60305'}   # what the source gave, at the last scan that gave the row
hand      {'thickness': 2.0}                      # what a person set
cleared   ['transmission']                        # the cells a person emptied
added     False                                   # True: made by a person, the source never gives it
removed   False                                   # True: removed by a person; kept so that a scan does not add it again
```

A cell holds two values, one per writer. The cell shown is the value set by hand if there is one, nothing if a person emptied it, else the source's:

```python
cells = {c: v for c, v in {**row.source, **row.hand}.items() if c not in row.cleared}
# in pandas words: source.mask(by_hand, hand)
```

Two values are needed, not one value and a mark: "revert" shows the source's value again, and the page shows it under a cell set by hand.

### A cell holds plain data

A cell holds the plain form of a value, as the parameters of a request are held before they are resolved: text, a number, a truth value, `None`, or a list or dict of these.

```python
'run:60339'                                                 # a file, in a column whose field takes a file
2.0                                                         # a thickness
None                                                        # "no direct beam": a value. An empty cell is a key left out.
['path:masks/beamstop.xml', 'path:masks/edges.xml']         # several files in one cell
{'start': 1.0, 'stop': 13.0, 'num_bins': 200, 'unit': 'Å'}  # a group
{'@ref': 'output', 'record': 'r17', 'output': 'centre'}     # an output of another record
```

A value is brought to this form once, where it enters the table: from a source, from an edit on the page, from a table file. The field of the column validates it when a request is made, as today. The four converters go. Two cells are equal if their plain data are equal, and `rows.json` is the table as it is.

Whether a file is the text `run:60339` or the tagged form `{'@ref': 'file', 'file': 'run:60339'}` is open (see "Open").

### A scan is a merge

```python
def scan(table, files):
    given = {row.key: row for row in source.rows(files)}     # the source sees every file
    for key, row in given.items():
        if key in table:
            table[key].update(index=row.index, info=row.info, source=row.values)   # never `hand`
        elif scope.holds(row):
            table.add(row)
    not_given = {key for key in table if key not in given and not table[key].added}
```

| The scan gives a row | The table has it | Then |
|---|---|---|
| yes, in the scope | no | the row is added |
| yes, outside the scope | no | nothing |
| yes | yes | `index`, `info` and `source` are replaced; `hand`, `removed` stay |
| no | yes | the row stays as it is, and is shown as *not in the files* |

A scan that changes nothing writes nothing. A scan that cannot list the files changes nothing, and the page says why.

*Not in the files* is not kept: it says what the last scan of this process found. A row not in the files keeps its cells, its result and its plot. A person can reduce it if its cells still resolve, and remove it. Follow leaves it alone.

### The scope says which rows the batch shows and which rows a scan adds

As in ADR 0011, the scope is a slice of the index, and a batch shows the rows inside it and the rows added by hand. New: the scan adds only rows inside the scope, so a batch stores its range of the rows, not every row of the source.

- Widening the scope shows nothing new at once; the next scan adds the rows.
- Narrowing it hides the rows outside. They stay in `rows.json` with what was set by hand, so widening it again shows them as they were.

### Only a person removes a row

A removed row is kept, marked `removed`, so that the next scan does not add it again, and so that "Bring back" works as today. Nothing else removes a row: not a scan, not a correction of a run, not a change of the source's code.

One action removes the rows that are not in the files, for the case below where a correction leaves a row behind.

### "Start the next batch here" copies rows

The next batch gets the rows from the index on, as they are: `source`, `hand`, `removed`. Then the scope of this batch ends there, which hides them here. Results stay under the label of this batch (ADR 0011).

### A table goes in and out without loss

```python
frame = batch.frame()      # a DataFrame: the key as index, a column per column, then status and what was set by hand
batch.put(frame)           # a cell that differs from the cell shown is set by hand; a key the batch lacks is added
batch.put(batch.frame())   # sets nothing: the contract, tested with every type of cell an application has
```

The CSV of the command and the table file of the page are this frame as text. The batch layer keeps rows of plain data, as JSON; `frames.py` stays the one place that converts to and from a `DataFrame` (see "pandas", below).

## Examples

A batch of sample runs with scope `[60300, None)`.

**The files are not reachable.**

```
before                                        mount gone, page opened
key    sample  transmission  status           key    sample  transmission  status
60302  60302   60303         completed        60302  60302   60303         completed       not in the files
60304  60304   60305         ready            60304  60304   60305         ready           not in the files
```

Results and plots show. "Reduce" on 60304 is refused with the reason that its file is not found, as for any cell that does not resolve.

**Two files are removed.** The two rows stay, shown as not in the files. Nothing else changes.

**A run is corrected.** Run 60306 was recorded as a sample and is a transmission run. Before the correction the source gave a row `60306`; after it, it does not, and gives row `60304` the transmission run 60306.

```
key    sample  transmission  status
60304  60304   60306         outdated         # source cell replaced by the scan
60306  60306   -             incomplete       not in the files      # stays until a person removes it
```

Today row `60306` disappears at the scan. Here it stays, marked, and "Remove the rows not in the files" removes it. This is the cost of the decision: a row that only wrong metadata made is not cleaned up for the person.

**A cell is set by hand, then the file changes.**

```python
batch.rows.edit('60304', {'transmission': 'run:60310'})
# a correction makes the source give transmission 60306 for this row
# shown: 60310 (hand); under it: 60306 (source). Revert shows 60306.
```

**The command.**

```
essapps-batch reduce iofq.yaml runs.csv -l water     # a batch without a source: the rows of runs.csv are its rows
essapps-batch reduce -s pkg:SOURCES -l 20261118-130204 -o done.csv
# edit done.csv in a spreadsheet, then:
essapps-batch reduce -s pkg:SOURCES -l 20261118-130204 done.csv   # the cells changed are set by hand; the others are not
```

## pandas

Developers of applications know pandas, so the table is given to them as a `DataFrame`, and the guide explains the model in its terms.

- **`batch.frame()` and `batch.put(frame)`.** With plain cells the frame holds the same data as the table: most columns are text or numbers, and `put(frame())` loses nothing.
- **The guide states each concept with its pandas line:**

| Concept | For a pandas user |
|---|---|
| a row's cells | `source.mask(by_hand, hand)` |
| a scan | an outer merge on the key that overwrites `source`, never `hand` |
| the scope | `rows[(rows.run >= start) & (rows.run < stop)]` |
| the results | `records.sort_values('created').drop_duplicates('member', keep='last')` |
| the state | `cells.join(results)`, a left join on the key |
| a rule, such as `left_to_reduce` | a boolean mask over the state |
| the settings | a column with one value for all rows |

Whether the batch itself holds its rows as dicts of plain data or as a `DataFrame` is not decided here: it is not seen from outside. A batch writes before it changes and has one writer, so it does not hand out a frame that it holds, and a person works on a copy either way. Both forms were built and run through one session with equal results and equal size (162 lines against 156). Two things need care in a `DataFrame`: `None` is a value (`direct_beam: DirectBeamFile | None`), which the pandas operations for missing data treat as an empty cell; and a few columns hold a list or a dict.

## Alternatives considered

- **Keep the rows derived, and cache the last scan for when the files are away.** Two models: what a person sees depends on whether the cache or the scan answered, and removing files still removes rows.
- **Keep the rows, and let a scan remove those it no longer gives.** That is today's behaviour with a cache. It fails the second problem.
- **Remove a row not in the files if it has no result and nothing set by hand.** It cleans up after a correction. Rejected: a row then disappears or stays by a rule a person has to know, and a mount that is away for a moment removes rows that come back at the next scan. One explicit action does the same when a person wants it.
- **The scope only says what a scan adds; narrowing it hides nothing.** Then the scope in the list of batches no longer says which rows a batch has, and "Start the next batch here" has to move rows out of one table and into the other.
- **Cells as typed objects** (`FileRef`, `OutputRef`), as a source makes them today. Two forms of one value, and converters between them; see the context.

## Consequences

- `rows.json` replaces `edits.json`; `Batch.rows` stays its one writer. No code reads `edits.json`: a batch made before keeps its header and settings, and what was set by hand on it is lost.
- `RowTable` holds rows with `source` and `hand` in place of the layers `scan`, `added`, `edits`, `removed`. The marker `EMPTY` for a cell emptied by hand goes; `cleared` names such cells. The six places that work out the origin of a cell read it from the row.
- A batch is shown without a scan. A source that raises, or that is no longer registered, is a message on the page of the batch, not a batch that cannot be shown. That leaves one such case: the workspace binds no spec of the batch's name.
- `manager.start_next` copies rows; the code that carries edits over by key goes.
- A batch stores its range of the rows. A change of scope that widens it needs a scan to show the new rows.
- Rows that only wrong metadata made stay until a person removes them (the example above).
- The size of `rows.json` is that of the table: a few hundred bytes per row, bounded by the scope. It is written when a scan or a person changes the table.
- The rules over the state (`ready`, `arrived`, `left_to_reduce`) are unchanged, except that `arrived` leaves a row not in the files.
- A source may give a cell as an object, such as a `FileRef`; the table holds its plain form. The four converters between the forms go, and a table file loaded back unchanged sets nothing by hand.

## Open

- **The plain form of a file in a column that takes files**: the text `run:60339`, or the tagged form that a request holds. The text gives a column of text, and the field of the column says that it names a file. The tagged form needs no field to be read.

- **Whether a removed row that is not in the files is dropped for good.** Kept here, for one rule less. The table then holds rows that nothing shows.
- **Rows shaped as a table.** A row has four kinds of fields (`key`, `index`, `values`, `info`), and five fields of `Source` say which is what. A table has columns with roles. This matters for several workflows on one batch (#222: a column has a third writer, a workflow), and is left for that decision. So is the name `index`, which means the key in pandas.
- **When the last scan was.** A page could say how old the table is when the files cannot be listed. Not needed for the decision.

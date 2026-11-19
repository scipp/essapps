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
'run:60339'                                                 # a file
'r17.centre'                                                # an output of another record
2.0                                                         # a thickness
None                                                        # "no direct beam": a value. An empty cell is a key left out.
['path:masks/beamstop.xml', 'path:masks/edges.xml']         # several files in one cell
{'start': 1.0, 'stop': 13.0, 'num_bins': 200, 'unit': 'Å'}  # a group
```

- **A file and an output are text.** The field of the column says that the text names a file or an output, as it does for a file named in a table today. The text of an output is what `str(OutputRef)` gives: `r17.centre`, `r18.slices[2]`.
- **Every column that the page offers is then text or a number.** Of the 17 parameters of the six specs of LoKI and the demo that can be a column on the page, 9 are files, 7 are outputs, and 1 is a number. A group or a list can be a column only of a batch made in code or with the command.
- **A value has one plain form.** It is brought to it once, where it enters the table: from a source, from an edit on the page, from a table file. For a dict or a list, that form is what the field of the column dumps, so a group typed without its defaults and the same group with them are one cell. (A reference as a dict has two forms today, with and without `'key': None`, which compare unequal.)

The field of the column validates a cell when a request is made, as today. The four converters go. Two cells are equal if their plain data are equal, and `rows.json` is the table as it is.

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

- **`batch.frame()` and `batch.put(frame)`.** The frame holds the same data as the table: text and numbers. An empty cell is `NaN`; `None`, a value, is the text `null`, as in a table file. `put(frame())` sets nothing.
- **The guide states each concept with its pandas line**, which is literally true of the plain data:

| Concept | For a pandas user |
|---|---|
| a row's cells | `source.mask(by_hand, hand)` |
| a scan | an outer merge on the key that overwrites `source`, never `hand` |
| the scope | `rows[(rows.run >= start) & (rows.run < stop)]` |
| the results | `records.sort_values('created').drop_duplicates('member', keep='last')` |
| the state | `cells.join(results)`, a left join on the key |
| a rule, such as `left_to_reduce` | a boolean mask over the state |
| the settings | a column with one value for all rows |

### What the batch holds inside: rows of plain data

Inside, the table is rows of plain data, each a frozen value with `source`, `hand` and `cleared`; a table is replaced, never changed in place. A `DataFrame` inside was built too (four frames on one index: the rows, `source`, `hand`, `cleared`) and run through the same session with equal tables after each step. With plain cells nothing that can be counted decides between them:

| | rows of plain data | four frames |
|---|---|---|
| lines, as written | 152 | 138 |
| lines, formatted at 88 columns | 209 | 241 |
| the state, the rules, `frame()`, `put()` | 70 lines | 38 lines |
| plain data out and in (a request, a record's params, `rows.json`) | 4 lines | 16 lines |
| a scan, the state, saving, at 3000 rows | 15, 10, 24 ms | 15, 14, 44 ms |
| a frame handed out cannot change the one held | a copy is made | yes, by copy-on-write (72 cases; one exception, `combine_first` on `object` columns) |

The choice is made on three facts that cannot be counted, and by a narrow margin:

1. **Three partners of a batch take plain data and one takes a frame.** A request, the params of a record, and `rows.json` are plain data, at every reduction, state and save. A person takes a frame, on demand. Rows hold the form that the three use.
2. **A frame inside needs rules that nothing checks.** Four traps were hit in 138 lines, two of them silent: `combine_first` shares memory with its argument, so a write changed the table held; `.at` at a key that is no row adds a row; `from_dict` turns 200 into 200.0; `ne` says that two empty cells differ. `None` next to an empty cell needs the text `null` in the cells. A type checker reads a row; it reads nothing of a `pd.DataFrame`.
3. **`ess.apps.batch` imports without pandas today.**

For the frame: the state and the rules read as the formulas of the table above. It is a sound second choice, and the one to take if `None` stops being a cell value of its own (it is one at 3 of the 17 columns), or if several workflows on one batch (#222) make the column the unit of work.

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

- **Whether `None` stays a cell value of its own.** `direct_beam` takes a file or `None`, "no direct beam", and an empty cell there makes the row incomplete. A named choice in its place would make every cell text or a number with nothing else to tell apart.
- **`put` of a frame that holds a row removed before** brings the row back. `frame()` leaves removed rows out, so this happens only if a person adds the key.

- **Whether a removed row that is not in the files is dropped for good.** Kept here, for one rule less. The table then holds rows that nothing shows.
- **Rows shaped as a table.** A row has four kinds of fields (`key`, `index`, `values`, `info`), and five fields of `Source` say which is what. A table has columns with roles. This matters for several workflows on one batch (#222: a column has a third writer, a workflow), and is left for that decision. So is the name `index`, which means the key in pandas.
- **When the last scan was.** A page could say how old the table is when the files cannot be listed. Not needed for the decision.

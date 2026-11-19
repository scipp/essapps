# ADR 0012: A batch is a template and a table

- Status: proposed (#236)
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *label*, *header*, *scope*, *source*, *definitions* are the words of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md).
*Template*, *blank* and *fill* are those of `ess.apps.Template`: a spec, some values, and blanks that each use fills.
*Row*, *key*, *cell*, *scan*, *result* are those of [batch.md, "Terms"](../../../packages/essapps/docs/batch.md#terms).

Today the rows of a batch are stored nowhere. They are computed at each scan, and only what a person set on top is kept:

```python
scan = source.rows(files with corrections)       # code of the application
rows = scan in scope, with edits on top          # edits.json: added, edited, cleared, removed, by key
```

So a batch is defined by how its rows are made, and the table a person sees is the output of that recipe now. Six problems follow.

1. **The table needs the files.** If the file mount is not available, the scan gives no rows, and the table is empty. The results are in the workspace, but no row shows them.
2. **Removing files removes rows.** A row whose file is gone disappears, also one that was reduced or edited.
3. **A change of the source's code changes every batch**, also one that was finished last week.
4. **The model has no table to put a table in.** The command takes one (`essapps-batch reduce iofq.yaml runs.csv`), and the page loads and saves one. Both are translated into "cells set by hand on top of the scan", in four structures of different shape (`added`, `edited`, `cleared`, `removed`) and a marker for a cleared cell.
5. **A cell is held in two forms**: a pydantic object if a source or the page made it (`FileRef(file='run:60339')`), plain data if a table file did. Four functions convert between the forms and do not agree: a table file loaded back unchanged marks a cell that holds a reference to an output as set by hand.
6. **What a batch is depends on what amends it.** The source, the scope, and who wrote each cell are parts of every batch, because a scan must be able to run again at any time. A batch made from a table file has no source and carries the same parts.

A person thinks of a batch as a table: they edit its cells, remove its rows, reduce its rows and read their results.

## Decision

**A batch is a template and a table. The template holds the settings, and its blanks are the columns; each row of the table fills it once. The table has one value per cell, and only a person changes it. How the table was made is not part of the batch.**

```
batch '20261118-130204'
  header     title, description
  template   spec      loki-iofq
             params    {'wavelength': {...}, 'thickness': 1.0}    # the settings: one value per parameter, for all rows
             blanks    ('sample', 'sample_transmission')          # the parameters that each row gives
  rows       one DataFrame: a column per blank, and notes
```

```
rows
key     run    name         sample      sample_transmission
60339   60339  D2O buffer   run:60339   run:60305
60340   60340  sample A     run:60340   <NA>
water   <NA>   by hand      run:60012   run:60013
```

This decides what a batch is when it is made once and then changed by a person. How a batch is amended from the files, Follow included, is decided after it and on top of it (see "Not decided here").

### Keys, columns, cells

- **The key** of a row is text, unique in the batch, and the index of the frame. It joins a row to its results: the records under the label of the batch with that key, as today.
- **A column named as a parameter of the spec is a parameter column**, and the parameter columns are the blanks of the template. Any other column, such as `run` or `name`, is a note for the person. Nothing else tells them apart.
- **A cell holds one value, or is empty.** The value is the plain form of a parameter's value, as a request holds it before it is resolved. A file and an output are text: `run:60339`, and `r17.centre` for the output `centre` of record `r17`.
- **A column has the type of its parameter**, so a wrong value is refused where it enters the table:

| The parameter takes | dtype | A cell |
|---|---|---|
| a file, an output, text | `string` | `'run:60339'` |
| a number | `Float64`, `Int64` | `2.0` |
| a truth value | `boolean` | `True` |
| a list, a group, or more than one of the above | `object` | `['run:1', 'run:2']` |

### A row fills the template

```python
def request(key):
    cells = rows.loc[key, list(template.blanks)].dropna().to_dict()
    for parameter in template.blanks - cells.keys():        # the empty cells
        if has_default(parameter):
            cells[parameter] = default(parameter)
        elif takes_none(parameter):
            cells[parameter] = None
    return template.fill(params=cells)      # refuses "a blank, not filled": the row is incomplete
```

- **A parameter is a setting or a column, never both.** These are the rules of a template, unchanged: `fill` refuses a value for a parameter that the template fixes, and naming a parameter as a blank drops its value. So every value that the table contributes is in the table, and no cell depends on a setting.
- **The settings are one dict**, parameter to value, with no keys of rows: `template.params`.
- **An empty cell gives no value.** The parameter then takes its default, or `None` if it takes `None`, and the row is incomplete otherwise. A cell holds no word for `None`: in a table, empty is `None`.

| A column of | An empty cell gives |
|---|---|
| `thickness: float = 1.0` | `1.0` |
| `direct_beam: DirectBeamFile \| None` | `None`: no direct beam |
| `sample: SampleFile` | nothing: the row is incomplete |

### A person changes the table

A batch gives its table and takes one:

```python
frame = batch.frame()                                      # the rows, as above
frame.loc['60340', 'sample_transmission'] = 'run:60341'    # set a cell
frame.loc['60340', 'sample_transmission'] = pd.NA          # empty it
frame.loc['d2o', 'sample'] = 'run:7'                       # add a row
frame = frame.drop('60339')                                # remove a row
frame['thickness'] = 1.0                                   # add a column: `thickness` becomes a blank
frame = frame.drop(columns='thickness')                    # remove it: `thickness` is a setting again, with no value yet
batch.put(frame)                                           # replaces the table; the blanks follow its columns
batch.put(batch.frame())                                   # changes nothing and writes nothing
```

`put` refuses the whole table for a key that is empty or used twice, and for a cell that does not have the type of its column. It keeps the blanks of the template equal to the parameter columns of the table, so the columns are said in one place. Each action of the page is `put` with one such line before it. A table file of the page and the CSV of the command are this frame as text.

Removing a row drops it. Its records stay under the label, as every record does, and show again if a row with that key is added.

### Making a batch

The first rows are an argument of making the batch, as its first settings are; the parameter columns of the rows are its blanks. Where they come from is the business of whoever makes it:

```python
frame = table_of(source.rows(files))                     # rows from the files: code of the application
manager.create('loki-iofq', title='Afternoon', rows=frame[frame.run >= 60339])
manager.create('loki-iofq', title='Water', rows=pd.read_csv('runs.csv', index_col='key', dtype='string'))
manager.create('loki-iofq', title='By hand')             # no rows yet
```

On the page, "New batch" takes a spec, and either a source with a range of its rows, a table file, or nothing.

### The state is not stored

```python
newest = records.sort_values('created').drop_duplicates('member', keep='last').set_index('member')
state = rows.join(newest)                               # a left join on the key
outdated = [request(key) != newest.params[key] for key in rows.index]
```

A cell that names a file that is not found is marked when the table is shown, and a request with it is refused, as for any cell that does not resolve. Nothing about the files is stored in the table.

### What is kept

```
definitions/labels/20261118-130204/header.json     title, description
definitions/labels/20261118-130204/settings.json   the template, as a settings file that a person saves: spec, params, blanks
definitions/labels/20261118-130204/rows.json       the table: the plain data of frame.to_json(orient='table')
```

pandas writes and reads the table with the types of its columns. Checked on a table with `string`, `Float64`, `Int64` and `object` columns, empty cells, a list and a dict in a cell, and the key `'0342'`: the frame read back equals the frame written, with an empty cell of an `object` column read as `NaN`.

### "Start the next batch here" splits the table

```python
at = rows.index.get_loc('60400')
this, following = rows.iloc[:at], rows.iloc[at:]     # the next batch gets `following`, and the same template
```

Results stay under the label of this batch (ADR 0011), so a row that moves has no result in the next batch.

## Examples

**The files are not reachable.** The page shows the table, the results and the plots. Each file cell is marked as not found, and "Reduce" on such a row is refused with that reason.

**Two files are removed.** The two rows stay as they are, with their results. Their file cells are marked.

**A run is corrected.** Run 60306 was recorded as a sample and is a transmission run. The batch has a row `60306`, and row `60304` names another transmission run. Nothing changes in the batch. The person removes row `60306` and sets the cell of `60304`.

**A parameter becomes a column.**

```python
template.params['thickness']                # 1.0, for every row
frame['thickness'] = 1.0                    # a column with the value of the setting in every cell
batch.put(frame)                            # `thickness` is a blank; every request is as before, no row is outdated
frame.loc['60340', 'thickness'] = 2.0
batch.put(frame)                            # row 60340 is outdated; the others are not
```

The page does the first two lines for "Make this a column". The settings form no longer shows `thickness`.

**Some rows have a direct beam run.** `direct_beam` takes a file or `None`. As a column, the rows with a run name it, and the cells of the others are empty.

**The command.**

```
essapps-batch reduce iofq.yaml runs.csv -l water      # makes the batch `water`: iofq.yaml its template, runs.csv its rows
essapps-batch reduce -l water -o done.csv             # the table, with a status and a record per row
# edit done.csv in a spreadsheet, then:
essapps-batch reduce -l water done.csv                # done.csv replaces the table, and its rows are reduced
```

The columns of the state (`status`, `record`) are not columns of the table and are left out when a table file is read.

## pandas

The batch holds its rows as a `DataFrame`, so pandas becomes a dependency of `ess.apps.batch`; today only the command and the table files need it. Each concept is a line of pandas:

| Concept | In pandas |
|---|---|
| a row, a key | a row of the frame, its index |
| the parameter columns | `[c for c in rows.columns if c in parameters]`, equal to `template.blanks` |
| the request of a row | `template.fill(params=rows.loc[key, blanks].dropna().to_dict())` |
| set, empty, add, remove | `.loc[key, column] = value`, `= pd.NA`, `.loc[key, column] = value` at a new key, `.drop(key)` |
| the results | `records.sort_values('created').drop_duplicates('member', keep='last')` |
| the state | `rows.join(results)`, a left join on the key |
| a rule, such as `left_to_reduce` | a boolean mask over the state |
| rows of a range of runs | `frame[(frame.run >= start) & (frame.run < stop)]` |
| the next batch | `rows.iloc[at:]` |

## Not decided here: amending a batch from the files

A source does three things to a batch today after it is made:

| A scan | Example |
|---|---|
| adds rows as files arrive | Follow |
| fills a cell that was empty | a sample run measured before its transmission run |
| changes a cell that has a value | a transmission run measured again: the source gives the rows of its sample the last one |

Whether each is wanted, and how it is done, gets its own ADR. Two conditions for it follow from this one:

- **What amends a batch uses the table as a person does**: it adds a row or sets a cell with `put`.
- **What it needs to know is its own state, next to the batch**: which source, which range of rows, which rows a person removed. None of it is a column of `rows` or a field of every row.

Until then the parts of ADR 0011 on the source, the scope and Follow stand, and the code of this ADR waits for that decision, since Follow is in use. Several workflows on one batch (#222) are the same kind of question: something other than a person fills cells.

## Alternatives considered

- **Keep the rows derived, and cache the last scan for when the files are away.** Two models: what a person sees depends on whether the cache or the scan answered, and removing files still removes rows.
- **Two values per cell, one that the source gave and one that a person set, and a scan that merges.** It stores the inputs of a merge in place of the table. Each row then carries what only a scan needs (`source`, `hand`, `cleared`, `added`, `removed`), and the table is not one that pandas holds or stores. It also decides the three uses of a scan above with "yes" without asking.
- **A cell overrides a setting**: `settings | row.dropna()`, with an empty cell meaning "as in the settings". A second rule next to that of a template for the same two things, values for all uses and values of one use. An empty cell then depends on something outside the table.
- **The columns fixed when the batch is made** (today). Nothing needs it: a row is outdated by the comparison of its request with its record, whatever the columns were.
- **A list of the parameter columns in the header.** The template has it: its blanks.
- **A word for `None` in a cell**, such as the text `none`, so that an empty cell of `direct_beam` can mean "not said yet". A text column would hold a word that is not text of its parameter, and pandas reads empty as missing in every operation.
- **Cells as typed objects** (`FileRef`, `OutputRef`). Two forms of one value, and converters between them; see the context.
- **Rows of plain data inside the batch, and a `DataFrame` only at its interface.** With one value per cell the frame is the table, and a second form of it needs converters and a rule for which one is right.

## Consequences

- `rows.json` replaces `edits.json`. No code reads `edits.json`: a batch made before keeps its settings, and what was set by hand on it is lost.
- `RowTable`, the layers of `Row` (`values`, `info`, `index`), the marker `EMPTY`, "set by hand", and the four converters of cells go. `Header.spec` and `Header.columns` go: the template names the spec, and its blanks are the columns.
- A batch is shown without a scan and without its source. A batch cannot be shown in one case: the workspace binds no spec of its name.
- A wrong value in a cell is refused when it is set or read from a table file, not when the row is reduced.
- A spec cannot make a person choose between a value and `None` for each row: an empty cell of such a column is `None`. It still can for a setting.
- A row that only wrong metadata made stays until a person removes it, and a correction of a run changes no batch (the example above).
- The command and the page need no translation of a table: a table file is the table.
- `put` writes the whole table. At 3000 rows that is tens of milliseconds.
- What ADR 0011 says of the spec and the columns in the header and of `edits.json` is replaced by this. What it says of the source, the scope and Follow stands until the ADR on amending.

## Open

- **A note cannot have the name of a parameter.** A column `sample` is the parameter `sample` of the spec, whatever the maker of the table meant by it.
- **The order of the rows** is the order of the frame, and a new row goes to its end. Whether sorting on the page changes the stored order is not decided.

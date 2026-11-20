# ADR 0012: A batch is a template and a table

- Status: accepted (#236)
- Deciders: Simon
- Date: 2026-11-19

## Context

*Batch*, *label*, *header*, *source*, *definitions* are the words of [ADR 0011](0011-a-person-makes-the-batches-of-a-workspace.md).
*Template*, *blank* and *fill* are those of `ess.apps.Template`: a spec, some values, and blanks that each use fills.
*Row*, *key*, *cell*, *result* are those of [batch.md, "Terms"](../../../packages/essapps/docs/batch.md#terms).
Two words name what this decision replaces.
A *scope* was a slice of an index that a source gave each row, kept in the header of a batch.
A *scan* asked the source for every row at each read of a batch.

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

**A batch is a template and a table. The settings are one value per parameter, for the batch. The template holds the settings of the parameters that are no column, and its blanks are the columns; each row of the table fills it once. The value of a parameter for a row is its cell; if the cell is empty or the parameter is no column, the setting; if the batch has none, the default of the parameter. In a batch made from a source, a column that the source fills takes its value from the cell only. The table has one value per cell, and only a person changes it. How the table was made is not part of the batch.**

```
batch '20261118-130204'
  header     title, description
  settings   spec      loki-iofq
             params    {'wavelength': {...}, 'thickness': 1.0, 'sample_transmission': 'run:60305'}
                                                                  # one value per parameter, for the batch
  template   the settings but those at the blanks: {'wavelength': {...}, 'thickness': 1.0}
             blanks    ('sample', 'sample_transmission')          # the columns: parameters that a row may give itself
  rows       one DataFrame: a column per blank, and notes
```

```
rows
key     run    name         sample      sample_transmission
60339   60339  D2O buffer   run:60339   run:60305
60340   60340  sample A     run:60340   <NA>        # takes the setting: run:60305
water   <NA>   by hand      run:60012   run:60013
```

This decides what a batch is when it is made once and then changed by a person. How a batch is amended from the files, Follow included, is decided on top of it, in [ADR 0013](0013-follow-adds-the-rows-not-considered.md).

### Keys, columns, cells

- **The key** of a row is text, unique in the batch, and the index of the frame, named `key`; no column has that name. It joins a row to its results: the records under the label of the batch with that key, as today.
- **A column named as a parameter of the spec is a parameter column**, and the parameter columns are the blanks of the template. Any other column, such as `run` or `name`, is a note for the person. Nothing else tells them apart.
- **A cell holds one value, or is empty.** The value is plain data: a number, text, a truth value, a list, or a group. A file and an output are text: `run:60339`, and `r17.centre` for the output `centre` of record `r17`. A request takes neither as text, so the cells of a row are converted in one place before `fill` (`table.as_request`), and a value of a request back to a cell in one place (`table.cell_of`).
- **A column has the type of its parameter**, so a wrong value is refused where it enters the table:

| The parameter takes | dtype | A cell |
|---|---|---|
| a file, an output, a file or an output; text, a `Literal` or an enum of text, a time, a date | `string` | `'run:60339'` |
| a number | `Float64`, `Int64` | `2.0` |
| a truth value | `boolean` | `True` |
| a list, a group, a value or an output, anything else | `object` | `['run:1', 'run:2']` |

A cell must be of the kind of its column, and nothing converts it: `'2'` and `True` are not numbers. One exception: a column of whole numbers takes a float that is a whole number (`1.0` is kept as `1`), since pandas gives whole numbers with a gap as floats.

### A row fills the template

```python
template = Template(spec, params=settings, blanks=columns)  # drops the settings at the columns

def request(key):
    row = rows[list(template.blanks)].loc[[key]].to_dict('index')[key]
    cells = as_request(spec.params, {c: v for c, v in row.items() if v is not None})
    for parameter in template.blanks - cells.keys():        # the empty cells
        if parameter in settings:
            cells[parameter] = settings[parameter]
        elif has_default(parameter):
            cells[parameter] = default(parameter)
        elif takes_none(parameter):
            cells[parameter] = None
    return template.fill(params=cells)      # refuses "a blank, not filled": the row is incomplete
```

`to_dict('index')` gives each cell as a Python value of its column's type, and an empty cell as `None`. `rows.loc[key, blanks]` would not: the row is a `Series` of one dtype, so with only numbers among the blanks an `Int64` cell becomes a float, which a parameter of whole numbers refuses.

- **A cell overrides the setting for its row.** A column is a parameter that a row may give itself. The settings may hold a value for it, which every row with an empty cell takes, also a row added later.
- **The settings are one dict**, parameter to value, with no keys of rows: `batch.settings`. The rules of a template are unchanged: `fill` refuses a value for a parameter that the template fixes, and naming a parameter as a blank drops its value. So the template that the rows fill, and that a stage is opened from, holds the settings without those at the columns, and each row fills every column: with its cell, or with the setting.
- **An empty cell with no setting gives no value.** The parameter then takes its default, or `None` if it takes `None`, and the row is incomplete otherwise. A cell holds no word for `None`: in a table, empty is `None`.

| A column of | An empty cell gives, with no setting |
|---|---|
| `thickness: float = 1.0` | `1.0` |
| `direct_beam: DirectBeamFile \| None` | `None`: no direct beam |
| `sample: SampleFile` | nothing: the row is incomplete |

- **In a batch made from a source, a column that the source fills takes its value from the cell only.** An empty cell there means that the row waits for its file, not "take the value of the batch": the row is incomplete, whatever the settings hold and whatever the default of the parameter is. The rule lives where the values of a row are resolved (`batch.empty()`, which `batch.cells()` and so the state read), so a setting there is never read, and none has to be kept out at each way into the settings. An assignment of settings refuses one only to say so to a caller that tries, and `batch.settable(settings)` tidies settings taken from elsewhere.

### A person changes the table

A batch gives its table and takes one:

```python
frame = batch.frame()                                      # the rows, as above
frame.loc['60340', 'sample_transmission'] = 'run:60341'    # set a cell
frame.loc['60340', 'sample_transmission'] = pd.NA          # empty it
frame.loc['d2o', 'sample'] = 'run:7'                       # add a row
frame = frame.drop('60339')                                # remove a row
frame['thickness'] = pd.NA                                 # add a column: `thickness` becomes a blank; each row takes the setting
frame = frame.drop(columns='thickness')                    # remove it: every row takes the setting
batch.put(frame)                                           # replaces the table; the blanks follow its columns
batch.put(batch.frame())                                   # changes nothing and writes nothing
```

`put` refuses the whole table for a key that is empty or used twice, and for a cell that does not have the type of its column. It keeps the blanks of the template equal to the parameter columns of the table, so the columns are said in one place, and changes no setting. Each action of the page that changes rows is `put` with one such line before it. A table file of the page and the CSV of the command are this frame as text.

A parameter becomes a column, or a column a setting, with the settings in view:

```python
batch.make_column('thickness')             # an empty column; the setting stays, and every row takes it
batch.make_setting('thickness')            # drops the column; the setting becomes the value that every row takes
batch.make_setting('thickness', '60340')   # where the rows differ: the value that row 60340 takes
```

`make_setting` takes the value that the row takes: its cell, or, if the cell is empty, the setting, which then stays. So a column whose cells are all empty keeps its setting.

Removing a row drops it. Its records stay under the label, as every record does, and show again if a row with that key is added.

### Making a batch

The first rows are an argument of making the batch, as its first settings are; the parameter columns of the rows are its blanks. Where they come from is the business of whoever makes it:

```python
frame = rows_of(client, source)                          # rows from the files: code of the application
manager.create('loki-iofq', title='Afternoon', rows=frame[frame.run >= 60339])
manager.create('loki-iofq', title='Water', rows=pd.read_csv('runs.csv', index_col='key', dtype='string'))  # files and text only
manager.create('loki-iofq', title='By hand')             # no rows yet
```

On the page, "New batch" takes a spec, and either a source with a range of its rows, a table file, or nothing.

### The state is not stored

```python
newest = records.sort_values('created').drop_duplicates('member', keep='last').set_index('member')
state = rows.join(newest)                               # a left join on the key
changed = {key: differs(request(key), newest.params[key]) for key in rows.index}
own = {key: set(row.dropna().index) for key, row in rows[columns].iterrows()}  # the cells with a value
earlier = [key for key in rows.index if changed[key] and not changed[key] & own[key]]
outdated = [key for key in rows.index if changed[key] & own[key]]
```

A row whose request differs from its result's only at settings reads `earlier`, reduced with earlier settings: a change of settings applies to the rows not reduced yet. For one row, a parameter counts as a setting if it is no column or the row's cell is empty, since the row then follows the setting. A row whose own cell changed reads `outdated`. Which cells are empty is read from the table now, so nothing more is kept.

A cell that names a file that is not found is marked when the table is shown, and a request with it is refused, as for any cell that does not resolve. Nothing about the files is stored in the table.

### What is kept

```
definitions/labels/20261118-130204/header.json     title, description
definitions/labels/20261118-130204/settings.json   the settings, as a template file without blanks that a person saves: spec, params
definitions/labels/20261118-130204/rows.json       the table: {"schema": ..., "data": [{"key": "60339", "sample": "run:60339", ...}, ...]}
```

`rows.json` has the shape of `frame.to_json(orient='table')`, but pandas neither writes nor reads its records: `to_json` rounds floats (`0.1 + 0.2` reads back `0.3`), and an `Int64` above 2**53 loses its last digit. So:

```python
data = {'schema': build_table_schema(frame, version=False),                      # pandas: the dtype of each column
        'data': [{'key': key, **row} for key, row in frame.to_dict('index').items()]}
text = pydantic_core.to_json(data)                                                 # the shortest text of each float that reads back equal
column = pd.array([r.get(name) for r in data['data']], dtype=field['extDtype'])    # each column, read back
```

`table.data_of` and `table.frame_of` are the two halves. Checked on a table with `string`, `Float64`, `Int64`, `boolean` and `object` columns, empty cells, a list and a dict in a cell, `0.1 + 0.2`, `2**53 + 1`, a table without rows, and the key `'0342'`: the frame read back equals the frame written, dtypes included, with an empty cell of an `object` column read as `None`. It holds for every table, since a note too is text, a number, a truth value, or plain data.

## Examples

**The files are not reachable.** The page shows the table, the results and the plots. Each file cell is marked as not found, and "Reduce" on such a row is refused with that reason.

**Two files are removed.** The two rows stay as they are, with their results. Their file cells are marked.

**A run is corrected.** Run 60306 was recorded as a sample and is a transmission run. The batch has a row `60306`, and row `60304` names another transmission run. Nothing changes in the batch. The person removes row `60306` and sets the cell of `60304`.

**A parameter becomes a column.**

```python
batch.settings['thickness']                 # 1.0, for every row
batch.make_column('thickness')              # an empty column; every request is as before, no row is outdated
frame = batch.frame()
frame.loc['60340', 'thickness'] = 2.0
batch.put(frame)                            # row 60340 is outdated; the others are not
batch.settings = {**batch.settings, 'thickness': 1.5}
                                            # the rows with an empty cell read `earlier`; 60340 does not
```

A row that Follow adds later has an empty cell, and is reduced with 1.5. The settings form shows `thickness` with 1.5, marked as a column.

**Some rows have a direct beam run.** `direct_beam` takes a file or `None`. As a column, the rows with a run name it, and the cells of the others are empty.

**The command.**

```
essapps-batch reduce iofq.yaml runs.csv -l water      # makes the batch `water`: iofq.yaml its settings, runs.csv its rows
essapps-batch reduce -l water -o done.csv             # the table, with a status and a record per row
# edit done.csv in a spreadsheet, then:
essapps-batch reduce -l water done.csv                # done.csv replaces the table, and its rows are reduced
```

The columns of the state (`status`, `record`) are not columns of the table and are left out when a table file is read. A value of `iofq.yaml` at a column of `runs.csv` is the setting of the rows whose cell is empty.

## pandas

The batch holds its rows as a `DataFrame`, so pandas becomes a dependency of `ess.apps.batch`; today only the command and the table files need it. Each concept is a line of pandas:

| Concept | In pandas |
|---|---|
| a row, a key | a row of the frame, its index |
| the parameter columns | `[c for c in rows.columns if c in parameters]`, equal to `template.blanks` |
| the request of a row | `template.fill(params=as_request(spec.params, rows[blanks].loc[[key]].to_dict('index')[key]))`, its empty cells given the settings |
| set, empty, add, remove | `.loc[key, column] = value`, `= pd.NA`, `.loc[key, column] = value` at a new key, `.drop(key)` |
| the results | `records.sort_values('created').drop_duplicates('member', keep='last')` |
| the state | `rows.join(results)`, a left join on the key |
| a rule, such as `left_to_reduce` | a boolean mask over the state |
| rows of a range of runs | `frame[(frame.run >= start) & (frame.run < stop)]` |

## Not decided here: amending a batch from the files

A source does three things to a batch today after it is made:

| A scan | Example |
|---|---|
| adds rows as files arrive | Follow |
| fills a cell that was empty | a sample run measured before its transmission run |
| changes a cell that has a value | a transmission run measured again: the source gives the rows of its sample the last one |

Whether each is wanted, and how it is done, is decided in [ADR 0013](0013-follow-adds-the-rows-not-considered.md). Two conditions for it follow from this one:

- **What amends a batch uses the table as a person does**: it adds a row or sets a cell with `put`.
- **What it needs to know is its own state, next to the batch**: which source, and which rows it has considered. None of it is a column of `rows` or a field of every row.

The code of this ADR and of ADR 0013 is written together, since Follow is in use. Several workflows on one batch (#222) are the same kind of question: something other than a person fills cells.

## Alternatives considered

- **Keep the rows derived, and cache the last scan for when the files are away.** Two models: what a person sees depends on whether the cache or the scan answered, and removing files still removes rows.
- **Two values per cell, one that the source gave and one that a person set, and a scan that merges.** It stores the inputs of a merge in place of the table. Each row then carries what only a scan needs (`source`, `hand`, `cleared`, `added`, `removed`), and the table is not one that pandas holds or stores. It also decides the three uses of a scan above with "yes" without asking.
- **A parameter is a setting or a column, never both**, as in a template: every value that the table contributes would be in the table, and no cell would depend on a setting. Making a parameter a column would copy the setting into the cell of every row and drop the setting. A row added later, by Follow, by "Add rows from the runs" or by hand, would have an empty cell, so it would be reduced with the default, not with the value that the batch had, and nothing would tell the person.
- **Fill the cell of a row added with the setting.** Each code that adds a row (a step, `take`, the page, a table file) would need the settings, and a setting changed would not reach the rows added before.
- **A setting at a column that the source fills, as at any other column.** A row taken before its file arrives would take the setting, a file of another sample, and be reduced with it without a word.
- **The columns fixed when the batch is made** (today). Nothing needs it: a row is outdated by the comparison of its request with its record, whatever the columns were.
- **A list of the parameter columns in the header.** The template has it: its blanks.
- **A word for `None` in a cell**, such as the text `none`, so that an empty cell of `direct_beam` can mean "not said yet". A text column would hold a word that is not text of its parameter, and pandas reads empty as missing in every operation.
- **Cells as typed objects** (`FileRef`, `OutputRef`). Two forms of one value, and converters between them; see the context.
- **Rows of plain data inside the batch, and a `DataFrame` only at its interface.** With one value per cell the frame is the table, and a second form of it needs converters and a rule for which one is right.

## Consequences

- `rows.json` replaces `edits.json`. No code reads `edits.json`: a batch made before keeps its settings, and what was set by hand on it is lost.
- `RowTable`, the layers of `Row` (`values`, `info`, `index`), the marker `EMPTY`, "set by hand", and the four converters of cells go. `Header.spec` and `Header.columns` go: the template names the spec, and its blanks are the columns.
- A batch is shown without a scan and without its source. A batch cannot be shown in two cases: the workspace binds no spec of its name, or it binds a version of the spec that refuses the cells of the table, since it takes another kind of value at a column, or has a parameter of the name of a note.
- A wrong value in a cell is refused when it is set or read from a table file, not when the row is reduced.
- A spec cannot make a person choose between a value and `None` for each row: an empty cell of such a column is the setting, or `None` if there is none. It still can for a setting.
- An empty cell depends on the settings, outside the table. The page shows it in grey as the value that the row takes.
- A table file shows an empty cell where a row takes the setting, so it needs the settings file to say what a row was reduced with.
- A cell emptied after its row was reduced counts its value as a setting: the row reads `earlier`, not `outdated`.
- A batch made from a source keeps the names that its source fills with it, in `source.json`, as they were when it was made; the batch, the command without the sources, and Follow read them there. A step asks the batch which names a row waits for.
- The table of a batch made from a source keeps a column for each name that its source fills: `put`, a table file loaded, and "Make this a setting" refuse to drop one.
- `settings.json` holds every setting, those at the columns too, as a template without blanks. A file whose template has blanks, as one written before, reads the same: its blanks are not read, and it has no settings at the columns.
- A row that only wrong metadata made stays until a person removes it, and a correction of a run changes no batch (the example above).
- The command and the page need no translation of a table: a table file is the table.
- `put` writes the whole table. At 3000 rows that is tens of milliseconds.
- What ADR 0011 says of the spec and the columns in the header and of `edits.json` is replaced by this. What it says of the source, the scope and Follow is replaced by ADR 0013.

## Open

- **A note cannot have the name of a parameter.** A column `sample` is the parameter `sample` of the spec, whatever the maker of the table meant by it.
- **The order of the rows** is the order of the frame, and a new row goes to its end. Whether sorting on the page changes the stored order is not decided.

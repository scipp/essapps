# Batch toolbox for application developers

`ess.apps.batch` reduces one spec for many samples: a table of rows, settings shared by all rows, and the result of each row.
It is built on the client of `ess.apps`; [the client guide](../../../docs/developer/roles/client-authors.ipynb) explains specs, records, labels and stages.
It needs `essapps[files]` (pandas).
The decisions behind it are [ADR 0012](../../../docs/developer/adr/0012-a-batch-is-a-table.md) (a batch is a template and a table) and [ADR 0013](../../../docs/developer/adr/0013-follow-adds-the-rows-not-considered.md) (Follow adds the rows not considered).

## The system on one page

### Picture

```
  person, browser            person, shell           notebook, test
       │                          │                        │
  web: Pages, Page           cli, frames                   │
  (one lock, a timer)             │                        │
───────┼──────────────────────────┼────────────────────────┼──────────
       ▼                          ▼                        │
     BatchManager   keeps header, source, following        │
       │  makes the one Batch of each label                │
       │  step(): one step of Follow                       │
       ▼                                                   ▼
     Automatic ──────► Batch ◄──────────────────────── Batch made in code
     (add rows, reduce)  │ template + table                (keeps nothing)
       │                 │ frame(), put(frame)
       ▼                 │ state(), reduce(keys), cancel()
     Source         ┌────┼───────────┐
     (code: files   ▼    ▼           ▼
      → frame)    table  Client      DefinitionStore
                  dtypes specs,      header, settings, rows,
                  cells  files,      source, following,
                         records     corrections
```

An arrow points from the user of a thing to the thing.
Nothing below the line imports anything above it.

### Terms

| Term | Definition |
|---|---|
| workspace | a `Client` (specs bound, files, records) and a `DefinitionStore`; one folder |
| label | the name of a batch, and the label of its records |
| batch | a label with a header (title, description), a template and a table |
| template | an `ess.apps.Template`: a spec, the settings, and the blanks |
| setting | the value of a parameter for all rows: an entry of `template.params` |
| table | one `pandas.DataFrame`: the key as index, a parameter column per blank, and notes |
| key | the text that names a row, unique in the batch; the member of the row's records |
| parameter column | a column named as a parameter of the spec; the parameter columns are the blanks |
| note | any other column, such as `run` or `name`; shown, never part of a request |
| cell | one value of a column, or empty; plain data such as `'run:60339'`, `2.0`, `['run:1', 'run:2']` |
| request of a row | the template filled with the row's cells |
| result of a row | the newest record under the label with the row's key as member |
| state of a row | its result, the status of the result, its problems, and what changed since the result |
| `Batch` | the object that holds the template and the table of one label and reduces rows |
| manager | the `BatchManager`: makes, lists, changes and removes the batches of a workspace |
| source | code of the application that makes a table from the files (a `Source`) |
| considered | the keys of the source that a batch made from it has taken or left out |
| waiting | a row of the source, not considered, whose cells of `Source.fills` are not all filled |
| Follow | a step every few seconds on one batch: add the rows of its source that are new and filled, then reduce the rows that are ready and have no result |
| definitions | what a workspace keeps besides files and records (table below) |

A parameter is a setting or a column, never both.
A setting and a column are the two places of one parameter: one value for all rows, or one value per row.

### Rules that always hold

1. **Only `put` changes a table.** A person on the page, the command, `take` and Follow each build a frame and call `batch.put(frame)`. A `put` of an equal frame writes nothing.
2. **The parameter columns are the blanks.** `put` makes the parameter columns of the frame the blanks of the template. Nothing else says which parameters are columns.
3. **A cell is plain data, converted in one place.** `table.as_request` turns cells into request values; `table.cell_of` turns a request value into a cell.
4. **A column has the dtype of its parameter.** A cell of another kind is refused where it enters the table, with a `ValueError` that names the key and the column.
5. **The state is computed, never kept.** It is read from the records each time. Each rule (`ready`, `not_reduced`, `left_to_reduce`, `earlier_settings`) is a function from the states to keys.
6. **A step of Follow never changes a row that the table has.** It adds rows, and reduces rows that have no result.
7. **Each kept file has one writer** (table below).
8. **A batch is shown without its source and without the files.** A file that is not found is a problem of its row.
9. **The rules are in the batch layer.** A refusal is a `ValueError`. The page and the command show it and copy no rule. The batch layer has no lock and no timer.

### What is kept on disk

| File | Content | Writer |
|---|---|---|
| `definitions/labels/<label>/header.json` | `{"title": ..., "description": ...}` | manager |
| `definitions/labels/<label>/settings.json` | the template as a template file: spec, params, blanks | manager at `create`; then the `Batch` |
| `definitions/labels/<label>/rows.json` | `{"schema": ..., "data": [{"key": "60339", "sample": "run:60339", ...}]}` | manager at `create`; then `Batch.put` |
| `definitions/labels/<label>/source.json` | `{"source": "sample-runs", "considered": ["60337", ...]}`; only for a batch made from a source | manager |
| `definitions/following.json` | `{"label": ...}` or `{"label": null}` | manager |
| `definitions/corrections.json` | metadata of files set by hand | `CorrectedFiles` |
| `store/`, the log | the records | the workspace |

A label is a batch if and only if `header.json` exists.
`create` writes settings, rows and source, then the header; `remove` deletes the header first.
The blanks in `settings.json` are not read: the parameter columns of `rows.json` are the blanks.

## One example followed through

An afternoon at LoKI.
The spec `loki-iofq` has, among others, the parameters `sample` and `sample_transmission` (files) and `thickness: float = 1.0`.
The source `sample-runs` makes one row per sample run and fills `sample` and `sample_transmission`.

**1. A batch is made from the source.** Runs 60337 to 60340 are listed.

```python
given = manager.rows_of('sample-runs')
#        run    name      sample     sample_transmission
# key
# 60337  60337  water     run:60337  run:60305
# ...
# 60340  60340  sample A  run:60340  run:60306
label = manager.create('loki-iofq', title='Afternoon', source='sample-runs',
                       rows=given[given.run >= 60339], label='afternoon')
manager.follow(label)
```

On disk: `header.json`, `settings.json`, `rows.json` with rows 60339 and 60340, `source.json` with `considered` = 60337 to 60340, and `following.json` with `afternoon`.
Rows 60337 and 60338 are considered but not in the table: they were left out at making, so Follow never adds them.

**2. A person edits a cell and reduces.**

```python
batch = manager.batch(label)
frame = batch.frame()
frame.loc['60340', 'sample_transmission'] = 'run:60307'
batch.put(frame)
batch.reduce(['60339', '60340'])
```

`rows.json` is written again. The records are in the store under the label, with members `60339` and `60340`.

**3. A setting becomes a column.**

```python
frame = batch.frame()
frame['thickness'] = batch.settings.get('thickness', 1.0)   # the same value in every cell
batch.put(frame)                                            # no row is outdated
frame.loc['60340', 'thickness'] = 2.0
batch.put(frame)                                            # row 60340 is outdated
```

`rows.json` and `settings.json` are written: `thickness` is a blank, and its setting is dropped.
`batch.put(frame.drop(columns='thickness'))` makes it a setting again, with no value.
For a list or a group, build the column as `pd.Series([value] * len(frame), index=frame.index, dtype=object)`: pandas spreads a list over the rows.

**4. Follow adds a row once its transmission run is listed.** The page calls `manager.step()` every few seconds.

| | What happens | Table | `considered` |
|---|---|---|---|
| a | sample run 60341 is listed; its transmission run is not | unchanged; `loop.waiting == ('60341',)` | unchanged |
| b | transmission run 60342 is listed | 60341 is added and reduced | + 60341 |
| c | a person removes row 60340 | 60339, 60341 | unchanged, so 60340 is not added again |
| d | the transmission of sample 60339 is measured again | unchanged: row 60339 keeps its cell | unchanged |

```python
loop = manager.step()        # None if no batch follows
loop.waiting                 # the keys of the rows that wait
loop.refused                 # why rows of this step were refused, by key
```

In step b, `rows.json` is written first, then `source.json`.

**5. A restart.** The process stops; sample run 60343 and its transmission run arrive meanwhile.

```python
manager = BatchManager(client, sources={'sample-runs': SAMPLE_RUNS}, definitions=definitions)
manager.following            # 'afternoon'
manager.step()               # adds 60343 and reduces it
```

The manager reads the headers and `following.json`.
`manager.batch(label)` reads `settings.json` and `rows.json` and makes the `Batch`.
The results are read from the records, so no row is reduced twice.

## Writing a source

A source is a function from files to a frame, with the columns it fills:

```python
from ess.apps.batch import FileInfo, Source

def pair_by_sample(files: Sequence[FileInfo]) -> pd.DataFrame:
    samples = [f for f in files if f.metadata.get('role') == 'sample']
    records = [
        {'key': str(f.metadata['run']), 'run': f.metadata['run'],
         'name': f.metadata.get('name'), 'sample': str(f.ref),
         'sample_transmission': partner(f, files)}   # None if none is listed
        for f in samples
    ]
    columns = ['key', 'run', 'name', 'sample', 'sample_transmission']
    return pd.DataFrame.from_records(records, columns=columns).set_index('key')

SAMPLE_RUNS = Source(pair_by_sample, fills=('sample', 'sample_transmission'))
```

- The index is the key, as text. A number as key is refused: write `str(run)`.
- A file is its name, `str(f.ref)`, such as `'run:60339'`.
- `selector` (optional) chooses the files the source is given; raw files if not given.
- `rows_of(client, source)` calls the source on the files listed now. It adds an empty column for each of `fills` that the frame lacks, so a batch made on an empty folder still has its columns.
- The page names a row by its note `name` if the table has one, else by its key.
- A source knows no spec. `fits(spec, source)` of `ess.apps.batch.manager` checks that each of `fills` is a parameter of the spec.

A source is plain code on plain data: test it on a list of `FileInfo`.

## Cells and their dtypes

`table.dtypes(spec.params)` gives the dtype of each parameter column:

| The parameter takes | dtype | A cell |
|---|---|---|
| a file, an output, a file or an output; text, a `Literal` or an enum of text, a time, a date | `string` | `'run:60339'` |
| a number | `Float64` | `2.0` |
| a whole number | `Int64` | `3` |
| a truth value | `boolean` | `True` |
| a list, a group, a value or an output, anything else | `object` | `['run:1', 'run:2']` |

`| None` does not change the dtype.
A parameter that takes text or an output is refused, since a cell cannot tell the two apart.

Nothing converts a cell: `'2'` and `True` are not numbers.
One exception: an `Int64` column takes a float that is a whole number, since pandas gives whole numbers with a gap as floats.
A note gets the dtype of `convert_dtypes()`, which must be one of the five.
In the table an empty cell is `pd.NA` (`None` in an `object` column); in `rows.json` and in `to_dict('index')` it is `None`.

`as_request` converts a cell at a parameter that takes:

```python
# a file
'run:60339'      -> FileRef(file='run:60339')
60339            -> FileRef(file='run:60339')        # a whole number is a run
# an output
'r17.centre'     -> OutputRef(record='r17', output='centre')
'r18.slices[2]'  -> OutputRef(record='r18', output='slices', key='2')
# a file or an output: text with a kind ([a-z]+:) is a file, other text an output
# a list of these: each element
# anything else, a file inside a group too: the cell as it is
```

`cell_of` is the inverse: a `FileRef` or an `OutputRef` gives its text, anything else its JSON form.

## The request of a row

```python
row = rows[columns].loc[[key]].to_dict('index')[key]       # each cell of its type
cells = as_request(model, {c: v for c, v in row.items() if v is not None})
for c in columns - cells.keys():                           # the empty cells
    if has_default(c): cells[c] = default(c)
    elif takes_none(c): cells[c] = None
template.fill(params=cells)                                # a blank left: incomplete
```

| A column of | An empty cell gives |
|---|---|
| `thickness: float = 1.0` | `1.0` |
| `direct_beam: DirectBeamFile \| None` | `None`: no direct beam |
| `sample: SampleFile` | nothing: the row is incomplete (problem `empty`) |

A table has no word for `None`: empty is `None`.

## The state of a row and the rules over it

`batch.state(keys=None)` gives a `RowState` per row:

| Field | Meaning |
|---|---|
| `record`, `status` | the result and its status (`pending`, `completed`, `failed`, `cancelled`), or `None` |
| `problems` | why the row cannot be reduced: an empty cell without a value, a cell the workspace refuses, a referenced record that failed or was cancelled |
| `changed` | the parameters at which the request now differs from the request of the result |
| `spec_changed` | the result is of another spec than the one bound |
| `outdated` | `spec_changed`, or `changed` is not empty |
| `text` | `pending`, `incomplete`, `not reduced`, `completed`, `outdated`, `failed` or `cancelled` |

`batch.results(keys=None)` gives the same without resolving anything (`changed` is `None`). The page shows it while the settings are refused.
`batch.state` raises `SubmitError` if the settings are refused: then no row can run.

The rules, in `ess.apps.batch`:

| Rule | Keys |
|---|---|
| `ready(states)` | no problems, and not pending |
| `not_reduced(states)` | ready, and without a result: what Follow reduces |
| `earlier_settings(states, columns)` | ready, completed, and changed only at settings |
| `left_to_reduce(states, columns)` | ready, and without a result, failed, cancelled or outdated; not those of `earlier_settings` |

A change of settings applies to rows not reduced yet; a change of a cell applies to its row.
A person reduces `left_to_reduce`, or chooses the rows.

`batch.reduce(keys)` submits all rows or none, and raises `SubmitError` naming the row and the column.
`batch.settings = {...}` replaces all settings and reduces nothing; it refuses a name that is a column.

## The batches of a workspace

```python
manager = BatchManager(client, sources={'sample-runs': SAMPLE_RUNS},
                       definitions=FolderDefinitions(folder),
                       settings={'loki-iofq': {...}},   # the first settings of a new batch, by spec
                       staged=('loki-iofq',))           # the specs that run through a stage
label = manager.create('loki-iofq', title='Water', rows=frame)   # label: the time if not given
manager.headers()          # {label: Header(title, description)}
manager.batch(label)       # the one Batch of the label, made at the first call
manager.change(label, title='Water, second try')
manager.remove(label)      # the definitions go; the records stay
```

- `create` refuses a label that has definitions or records, a spec that the workspace does not bind, and rows that `put` refuses.
- With `source=`, the batch keeps `source.json`: `considered` is every key the source gives now and every key of `rows`.
- `manager.batch(label)` reads `settings.json` and `rows.json`, binds the spec by name, and passes the definitions, so the `Batch` writes each change.
- Ask for the batch each time: one handed out before `remove` must not be used after.
- A `Batch` made in code, `Batch(client, spec, label=..., settings=..., rows=...)`, is the same class without definitions: it keeps nothing.

## Follow

At most one batch follows.

```python
manager.follow(label)       # ValueError if the batch has no source.json, or its source is not registered
manager.follow(None)        # none follows
loop = manager.step()       # an Automatic, or None
manager.offered(label)      # the rows of the source that the table lacks, with a boolean column `waits`
manager.take(label, keys)   # put these rows as the source gives them now, then consider them
```

A step (`Automatic.step`), as code:

```python
given = rows_of(batch.client, source)
new, waiting = added(batch.frame(), given, source.fills, considered)
batch.put(pd.concat([batch.frame(), new]))   # only the columns of the table
considered |= set(new.index)                 # source.json, written after the table
batch.reduce(not_reduced(batch.state(not_reduced(batch.results()))))
```

- A row is new if its key is neither considered nor in the table. A key in the table counts as considered, so a step cut off before `source.json` is written adds no row twice.
- A new row is added once each of `fills` that is a column of the table has a value. Until then it waits.
- A row that a person removes stays away: its key is considered. `take` brings it back.
- A row with a result is never reduced by a step, also when it is outdated, failed or cancelled.
- A row whose request is refused does not hold back the others; `loop.refused` says why, and the next step tries again.
- Removing the batch that follows ends Follow.

`follow`, `step`, `offered` and `take` raise `ValueError` "<label> was not made from a source of rows" for a batch without `source.json`.
`Automatic(batch, source, considered=...)` runs the same step on a `Batch` made in code, with `considered` in memory.

## Table files and the command

A table file (`.csv`, `.tsv`, `.xlsx`) is the frame as text, with the key as column `key`:

```
key,sample,thickness,binning,name
60339,run:60339,1.5,"{""bins"":4}",water
60340,run:60340,,,NA
```

- `table.write_table(frame, path)` and `table.read_table(path, spec.params)` are the only writer and reader.
- A list or a group is JSON text. Empty is the only missing marker, so the note `NA` is text.
- `read_table` reads parameter columns with their dtypes, and drops `status`, `record` and `failure`.
- A header that is not exactly a parameter name, such as `Sample`, is read as a note, and `sample` becomes a setting with no value.

The command:

```
essapps-batch rows package.module:SOURCE -o runs.csv     # the table a source makes now
essapps-batch reduce iofq.yaml runs.csv -l water          # makes `water`: iofq.yaml its template, runs.csv its table
essapps-batch reduce -l water -o done.csv                 # the table, with status, record and failure per row
essapps-batch reduce -l water done.csv                    # done.csv replaces the table (put); its rows are reduced
essapps-batch reduce iofq.yaml -l water2 -s package.module:SOURCES --source sample-runs
                                                          # a new batch from every row of a source
```

For a label that exists, the template becomes the settings and the table replaces the table.
`--remaining` reduces only `left_to_reduce`; `--earlier-settings` adds `earlier_settings` to it.
A new label with neither a table nor `--source` gets one row, keyed by the label: every parameter is a setting.
The command does no step of Follow.

## Chains

A cell can name an output of another record: `'r17.centre'` at a parameter that takes an output (typed `Referenced[...]` in the spec).

```python
centres = find.reduce(left_to_reduce(find.state(), find.columns))   # a batch of the spec that finds the centre
frame = iofq.frame()
for key, record in centres.items():
    frame.loc[key, 'beam_centre'] = str(record.ref('centre'))
iofq.put(frame)
```

A cell may name a record that still runs. If that record failed or was cancelled, the row has the problem "the record it reads is failed".

## Where the definitions are kept

`DefinitionStore` is a protocol: `load(key, read)`, `save(key, data)`, `delete(key)`, `labels()`.
A key is a tuple, such as `('labels', label, 'rows')` or `('following',)`.
`FolderDefinitions(folder)` keeps each table as a JSON file under `definitions/`, written whole through a temporary file.
`rows.json` holds `table.data_of(frame)`, and `table.frame_of(data)` reads it back equal, dtypes and floats included.
A refused write raises `NotKept`.

## The page

`ess.apps.batch.web` serves the manager as a page.
`Pages` holds one lock over the manager, and a timer that calls `manager.step()`.
`Page` is the presenter of one batch: each action that changes rows builds a frame and calls `put`.
The actions are described in [`batch/web/README.md`](../src/ess/apps/batch/web/README.md).

## Testing an application

- Test the source on a list of `FileInfo`, without a workspace.
- Test the batch on `ess.apps.local()` or a folder workspace with fake workflows (`ess.apps.testing`). They start in milliseconds, and refuse and fail as real ones do.
- Use `FolderDefinitions(tmp_path)`, and make the manager again to test a restart.
- `packages/essapps/tests/batch/toys.py` and `conftest.py` show the pattern.

## What it does not do

- A step never changes a cell or removes a row. A transmission run measured again reaches no earlier row; a person sets the cell.
- A correction of a run changes no batch.
- No range of runs is kept: "from run 60339 on" is the choice of rows when the batch is made.
- One batch follows at a time. The command does no step.
- The batch layer has no lock and no timer: whoever serves owns both.

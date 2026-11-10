# Batch toolbox for application developers

`ess.apps.batch` is for applications that reduce many samples with one workflow: a table of rows, settings shared by all rows, and the result of each row.
It is built on the client of `ess.apps`. A *spec* declares a workflow's parameters, and a *binding* connects a spec to code. [The client guide](../../../docs/developer/roles/client-authors.ipynb) explains specs, records, labels, and stages.
Your application supplies what depends on the technique: which files make which rows, and optionally how to draw a result.

## An application

```python
from ess.apps import local
from ess.apps.batch import Batch, FileInfo, Row
from ess.apps.batch.panel import BatchView
from ess.apps.panel import App

def pair_by_sample(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, with the transmission run of the same sample."""
    partner = {f.metadata.get('sample'): f.ref for f in files
               if f.metadata.get('role') == 'transmission'}
    rows = []
    for f in files:
        if f.metadata.get('role') == 'sample':
            name = f.metadata.get('sample')
            values = {'sample': f.ref}
            if name in partner:
                values['sample_transmission'] = partner[name]
            rows.append(Row(str(f.metadata['run']), values, info={'name': str(name)}))
    return rows

client = local(proposal='p1', files=files, bind=BINDINGS)
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'),
              label='iofq', source=pair_by_sample, staged=True)
view = BatchView(batch, info=('name',), draw=draw_iofq)
view.scan()
App(client, plots=PLOTS, tabs={'I(Q)': view}).panel().servable()
```

`python -m ess.apps.batch.panel.demo [DIRECTORY]` serves a complete application on fake files and fake workflows, with its workspace in a folder (see [The app and the command on one folder](#the-app-and-the-command-on-one-folder)).
`ess.instruments.loki.batch` holds a source and a `Batch` for the real LoKI workflows, without a view.

The same logic works without widgets, in a notebook or a test:

```python
from ess.apps.batch import ready, remaining

batch.scan()
state = batch.state(settings)   # where each row stands
records = batch.reduce(settings, ready(state))
batch.reduce(settings, remaining(batch.state(settings)))
batch.cancel()
```

`settings` is a dict of the parameters that are not columns, as `Form.params` gives it.

## Terms

- A **column** is a parameter of the spec that differs per row, such as `sample`. The columns are the blanks of a `Template`.
- The **settings** are all other parameters, shared by every row. They are the template's values.
- A **row** has a **key**, a value per column, and read-only **info** to show with it. A cell can hold anything a request can: a file, a reference to an output, a number, a list of runs.
- A **source** is your function from files to rows. A **scan** calls it.
- An **edit** is a cell set by hand, by a person or by your code.
- A row's **result** is the newest record of the client's submitter, with the batch's spec and label, and the row's key as its member. So two `Batch` objects with the same spec and label are one batch: give each batch its own label.

Choose a key that does not change from one scan to the next, such as the run number. Results and edits are found by key. Put the name of the sample in `info`, under a name that is not a column.

## Rules

**A scan never overrides what was set by hand.** A table holds two layers, what the last scan gave and what was set by hand; a row's cells are the first with the second on top.

```python
t = RowTable().rescan([Row('a', {'x': 1, 'y': 2}), Row('b', {'x': 3})])
t = t.edit('a', {'x': 10}).remove('b')
t = t.rescan([Row('a', {'x': 99, 'y': 98}), Row('b', {'x': 3}), Row('c', {'x': 5})])
# a: x is 10, set by hand, and y is 98, from the scan
# b: stays removed
# c: new
```

A row added by hand stays. An edited row that a scan no longer gives is not shown, and comes back with its edits if a later scan gives it. `reset(key)` forgets a row's edits and that it was removed; `reset(key, column)` forgets the edit of one cell. `EMPTY` clears a cell whatever the scan gives. None of this is stored: a new session starts from a new scan.

**A row is ready if it is complete and not running.** Each column with no default has a value, no cell reads a record that failed or was cancelled, and its result is not running. An empty cell of a column with a default takes the default. Whether a value is valid, such as a thickness below zero, is checked at submission.

| Function | Gives |
|---|---|
| `ready(states)` | every row that can run now |
| `remaining(states)` | ready rows with no result, a failed or cancelled one, or a stale one |
| `summary(states)` | the number of rows in each state |

**A result is stale** if the row's cells or the settings differ from those it was submitted with. `Batch` remembers that for the rows it submitted, and works it out only when `state` is given the settings. Requests are compared as given, so writing a default out, or naming a file another way, shows a row as stale: at worst a row is offered for a rerun.

**`reduce` is strict and all-or-nothing.** The rows given are submitted in one call. If any is refused, or still running, none is submitted, and the `SubmitError` names the rows refused. `FieldError.entry` is the row's key and `loc[0]` the column. A reason about the settings appears once, with no key. So `reduce(settings, ready(state))` skips incomplete and running rows, while one invalid value refuses the whole call and names the row. The workspace checks values first and references second, so correcting the rows named can bring up more.

**With `staged=True`** the rows go through a stage, and what they share is computed once. It needs a binding that makes stages. A stage is reused while the settings are the same. Whether a column can vary through a stage is the binding's to decide, and a refusal says so; a column that everything depends on saves nothing.

**One row with other settings.** `tweak(key, params)` submits one request with all of its parameters, which becomes the row's result. It is stale only if the row's cells change after that. If a parameter differs for many rows, make it a column.

## Writing a source

A source takes `FileInfo(ref, metadata)` for each file, and returns `Row`s. It calls nothing, so test it on plain data:

```python
def test_a_sample_run_pairs_with_its_transmission_run():
    rows = pair_by_sample([info(1, role='sample', sample='w'),
                           info(2, role='transmission', sample='w')])
    assert rows[0].values['sample_transmission'] == ref(2)
```

Group runs by giving a cell a list of files. Leave a cell out if a partner is missing, so the row shows as incomplete and the rest of the batch can run. A key given twice is refused with the key named, and nothing changes.

Pairing is yours to write. The framework knows no technique. If a source raises, the view shows the error in its message.

## When the metadata is wrong or missing

Wrap the file source, and give the wrapper to the workspace. Forms, rules, and sources then all see a file as corrected:

```python
files = CorrectedFiles(FolderFiles('/data', table))
client = local(proposal='p1', files=files, bind=BINDINGS)
files.correct(run, role='sample')       # or exclude=True, if your source reads it
```

`RunSheet` is a table of the files in which a person does this. You choose the fields with a pydantic model:

```python
class Marks(BaseModel):
    role: Literal['sample', 'transmission'] | None = None

sheet = RunSheet(files, client, Marks, then=view.scan)
App(client, tabs={'I(Q)': view, 'Runs': sheet})
```

## Chains

A cell may read another record, also one still running, so a row can use a value computed for that row:

```python
centres = find.reduce(settings, remaining(find.state()))
for key, record in centres.items():
    iofq.rows = iofq.rows.edit(key, {'beam_centre': record.ref('centre')})
```

After a centre failed, this reruns only what is left, and points those rows at the new centre. The field must be typed `Referenced[...]` in the spec.

## From files, and from the command line

A batch can be given as two files: a template, and a table with a row for each sample.
The command `essapps-batch` reads them (extra `[files]`):

```yaml
# iofq.yaml: a Template
spec: {name: loki-iofq, version: 1}
blanks: [sample, sample_transmission]
params:
  can: 60393                               # a run
  can_transmission: 60392
  empty_beam: 60392
  direct_beam: /data/direct-beam.h5        # a path; null for none
  masks: {'@latest': masks, output: masks}  # the newest record labelled masks
  beam_centre: {x: -0.029, y: -0.018, z: 4.116}
  wavelength: {start: 1.0, stop: 13.0, num_bins: 200}
  q: {start: 0.01, stop: 0.3, num_bins: 100}
```

```
# runs.csv
sample,sample_transmission,name
60339,60394,water
60250,60394,sds
```

```sh
B="essapps-batch --bind ess.instruments.loki.bindings:BINDINGS"
$B reduce iofq.yaml runs.csv --label water-series -o done.csv
$B export done.csv reduced -o exported.csv
```

```
# exported.csv
sample,sample_transmission,name,status,record,failure,iofq,transmission
60339,60394,water,completed,2e35ce83...,,reduced/60339/iofq.h5,reduced/60339/transmission.h5
60250,60394,sds,failed,bd9864f1...,OSError: ...,,
```

Each step is a command, and a function for a notebook that takes and returns a pandas `DataFrame`:

| Step | Command | Function |
|---|---|---|
| list the files | `files package.module:read --with runs` | `frames.described(pd.DataFrame(scan_folder(folder, read)), runs)` |
| make the table | `rows package.module:source`, or a spreadsheet | `frames.table_of(source(batch.file_infos(client)))` |
| reduce it | `reduce template table --label ...` | `frames.reduce_table(client, template, table, label=...)` |
| write the outputs to files | `export table folder` | `export.export_table(client, table, folder)` |

A table is a `.csv`, `.tsv`, or `.xlsx` file.

In a notebook, the table is a `DataFrame` and the template a `Template`, and no file is needed. [loki-batch.ipynb](../../essinstruments/docs/loki-batch.ipynb) runs every step on LoKI's data:

```python
done = frames.reduce_table(client, template, table, label='water-series')
exported = export_table(client, done, Path('reduced'))
```

A cell that is not text is its value, and `None` or `NaN` is an empty cell, so only a table read from a file is all text. To write `None` itself in a cell, as opposed to leaving it empty, write the text `null`.

**The table.** The first column holds the key of each row; it may also be a blank, as `sample` above. A column named as a blank holds its cells. A column of any other name is info, which comes back as it is; a column named `key`, as `frames.table_of` writes the key, is not info. Two mistakes are refused: a blank that is no column, such as a misspelt header, and a column named as a parameter that is not a blank, whose cells no row would use. An empty cell takes the parameter's default.

**A cell** is read as JSON if it is JSON, so `1.5`, `null`, and `{"x": 0, "y": 1}` are values, and anything else is text. Text that is JSON is written in quotes, such as `"12"` for a sample name at a string parameter; a `.csv` file doubles them, `"""12"""`. At a parameter that is a file field, or a list of them, a whole number names a run, `pid:...` a file by its kind, and other text a path, relative to the working directory. A number with a leading zero, such as `0342`, is not JSON, so it is read as a path: write `342`. The same holds for the values of the template's file. A file field inside a group takes the JSON form, `{"@ref": "file", "file": "run:60339"}`. Every file is one that the workspace's file source knows.

**`reduce`** returns the table with three columns, which it replaces if the table has them: `status`, the ID of the `record`, and `failure`, the reason. So its result can be its next table. With `--remaining` it leaves out the rows whose newest record under the label completed. It compares neither cells nor settings: after changing either, reduce without `--remaining`, or under another label. The `status` of a row is `completed`, `failed`, or `cancelled`.

Ctrl-C cancels every row that has not completed, those running among them, and the command ends with a `KeyboardInterrupt` once the workflows that were running return. It writes no table; reduce again with `--remaining`.

**`export`** writes the outputs of each completed row to `<folder>/<key>/`, and returns the table with a column for each output that holds the path of its file. It finds the outputs by the `record` column, not by the label. A key must be the name of a folder, so one with `/` is refused.

```
reduced/60339/iofq.h5          what scipp.io.load_hdf5 reads: a DataGroup of the output's fields
reduced/60339/_record.json     the record: every file and value the outputs came from
```

These files are a placeholder (`ess.apps.hdf5` states the format). A format of a technique, such as NXcanSAS, is not written. If the folder is inside the workspace, a later `files` lists what was exported.

**Exit status.** 0: done, and for `reduce`, every row completed. 2: the command or a row was refused, and nothing ran. 3: a row did not complete. 1: any other error. Every command but `files` opens the workspace, and needs `--bind`.

**The workspace** of the command is a folder, `--workspace`, the working directory by default (`ess.apps.folder`). It holds one input, which you write, and what the commands keep:

```
files.csv      input: the raw files, masks, and calibrations that a request may name
log.jsonl      history
store/         every output of every row, <record>/<output>.h5
```

`files.csv` is the table of `FolderFiles`, a stand-in for a catalogue: a row per file with its path, relative to the folder, and its metadata as text. A run number in a template or a table is looked up here.

```
file,run,kind,role
raw/60339.nxs,60339,raw,sample
masks/mask.xml,,mask,
```

`files` writes it from the files in the folder and, with `--with`, a table that says what each run is:

```sh
essapps-batch files ess.instruments.loki.files:read_metadata --with runs.xlsx -o files.csv
```

```
# runs.xlsx: a row per run, kept by a person
run,role,sample
60339,sample,water
60394,transmission,water
,direct_beam,          <- with a column file: a file that is no run
```

`read_metadata` is a function of the instrument package, from the path of a file to its metadata, or `None` for a file that is not the instrument's (`ess.apps.folder.scan_folder`). The runs come first, by their number, since the order of the rows is the order of measurement. A file that cannot be read is left out, with a warning. Two files of one run are refused.

A file says its kind, its run, and perhaps its sample, but not what a run is for. A row source that pairs runs reads the `role`, and a field such as the direct beam requires it. The run table gives it (`frames.described`): a row describes the file of its `run`, or the file at its `file` if it has no run. A run of the table that has no file in the folder is ignored, so the table may cover more than the folder holds. A file that no row describes keeps what it says of itself, and where both have a column, a cell of the run table that is not empty wins. Two rows for one run are refused. Without `--with`, the roles are typed into `files.csv`, so `files` then refuses to write over a table that exists.

The workspace outlives the command, so a template references a record of an earlier command. A template without blanks is one request, and `reduce` without a table submits it:

```sh
$B reduce detector.yaml --label detector      # prints key,status,record,failure
$B reduce masks.yaml --label masks            # detector: {'@latest': detector, output: detector}
$B reduce iofq.yaml runs.csv --label water-series
```

`{'@latest': label, output: name}` is read when the command starts, as the output of the newest record under the label, of `member` if given, whoever submitted it. It is for a template, not for a cell of a table. The record of each row holds the ID that it was read as. A reference by ID is `{'@ref': output, record: ..., output: ...}`.

One command holds the folder at a time. If a command was killed, the next one first finishes the rows that were running, so reduce again with `--remaining`. A workspace reads a persisted output by the type that its spec declares, so only one that runs the spec reads it. Nothing removes the files of the store.

## The UI

`BatchView` is a tab for `App`:

- the settings form, the table of rows with a filter by state, and a count of each state;
- buttons to scan, reduce all, reduce remaining, reduce selected, cancel, and show the settings of a selected result;
- details of a selected row, with the reason it failed;
- an editor: select rows, change cells, and **Update selected** writes only the changed cells, to all of them. A cell with something missing is not written and is named. To write a value that equals its default to several rows, choose the column in **Write**;
- **Bring back removed rows**;
- a form for a tweak of one row;
- a path, and buttons that load and save the files of the command: **Load rows** and **Save rows** (a table), **Load settings** and **Save settings** (a template), and **Export selected** (a folder).

## The app and the command on one folder

A `BatchView` reads and writes the files of `essapps-batch`, so a person and the command share one workspace folder (`open_folder`). One process holds the folder at a time, so stop the app before the command runs:

```sh
python -m ess.apps.batch.panel.demo ws     # Save rows: rows.csv, Save settings: settings.yaml
B="essapps-batch -w ws --bind ess.apps.batch.panel.demo:BINDINGS"
$B reduce settings.yaml rows.csv --label demo -o done.csv
python -m ess.apps.batch.panel.demo ws     # the rows show as completed
```

The command finds the results of the app, and the app those of the command, if both use the batch's label and the same submitter (`open_folder` gives `user`), and the outputs are persisted (`Batch(..., persist=True)`).

| Button | Same as |
|---|---|
| **Load rows** | `batch.rows = batch.rows.rescan(frames.rows_of(frames.read_table(path), spec.params, batch.columns))` |
| **Save rows** | `frames.with_results(client, states, frames.table_of(batch.rows, batch.columns))`, written with `frames.write_table` |
| **Load settings** | `text.load_template(path)`, refused if of another spec or with other blanks than the columns |
| **Save settings** | `text.write_template(batch.template(settings), path)` |
| **Export selected** | `export.export_table` on the completed rows selected |

`reduce` refuses a table with a row that cannot run, so remove the incomplete rows before saving. A table loaded takes the place of a scan, so the edits stay on top, and **Scan files** gives the rows of the files again. The `status` of a saved table is what the view shows, such as `stale` or `not reduced`. `reduce` replaces the result columns, and `export` reads only the `record` column, so both take such a table. A relative path is relative to the working directory of the app.

`draw(records)` gets the completed results of the rows selected, by key, and returns a Panel object.

Any object with `refresh(listing)` and `panel()` can be a tab (`ess.apps.panel.Tab`).

## Interfaces to the framework

| Between | Interface | Why |
|---|---|---|
| your pairing and the batch | a function over plain data | there is no behaviour to abstract |
| `Batch` and the framework | `Client`, the class | one implementation; a fake would have to redo the validation that a table shows |
| `Form` and the framework | `Listing`, a pydantic model of the params, and `Form.held()` | `Form` needs neither `Client` nor `WorkflowSpec` |
| a tab and `App` | the `Tab` protocol | applications write their own |
| the logic and the widgets | the methods of `Batch` and `RowTable` | the view only places widgets and connects events |

`ess.apps` and `ess.apps.panel` import nothing from `ess.apps.batch`, and `ess.apps.batch` imports no widget library. `ess.apps.batch.panel` holds the widgets. A test enforces this.

## Testing an application

| What | How |
|---|---|
| your source | plain data |
| rules of rows and states | plain values; see `packages/essapps/tests/batch/table_test.py` and `state_test.py` |
| submitting, stages, refusals, reruns | `local()` with `ess.apps.testing.FakeFiles` and fake workflows; a fixture starts in milliseconds |
| a view | call its methods and read its widgets, without a browser |

```python
fake = FakeFiles('p1', tmp_path)
fake.measure(1, 1.0, role='sample', sample='water')
binding = StagedFunction(IOFQ, fake_iofq)     # function_binding for no stages
with local(proposal='p1', files=fake, bind=[binding]) as client:
    ...
```

Raise in the fake to make a row fail; hold it back on a `threading.Event` to keep a row pending.

## What it does not do

- Keep the table across sessions, but in a file that a person saves and loads. A new `Batch` scans again. It finds results by key in its workspace, so `local()` per session starts with none, and a result that is not persisted is gone once its workspace closes.
- Show a stale result after a restart.
- Free the values of replaced results. They stay until the workspace closes or they are released, unless the batch persists them (`persist=`).
- Offer preparatory steps with a **Use** button. A `Referenced[...]` setting offers each record in the settings form.

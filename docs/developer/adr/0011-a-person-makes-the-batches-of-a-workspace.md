# ADR 0011: A person makes the batches of a workspace, each with a scope over the rows of a source

- Status: accepted. Several workflows on one batch are described below and not decided. Amended 2026-11-18: the header holds the spec and the columns. Amended 2026-11-19: the manager keeps which batch follows; "Start the next batch here" leaves Follow where it is. [ADR 0012](0012-a-batch-is-a-table.md), proposed, replaces what is said here of the spec and the columns in the header and of `edits.json`.
- Deciders: Simon
- Date: 2026-11-18

## Context

*Spec*, *record* and *binding* are the words of [README.md](../README.md).
*Column*, *settings*, *row*, *key*, *source*, *scan*, *edit*, *result* and *outdated* are those of [batch.md, "Terms"](../../../packages/essapps/docs/batch.md#terms).
Four more are used here:

- The *label* of a batch names its results: a row's result is the newest record under the label with the row's key.
- A *template* is a spec with values for some of its parameters; the parameters left out are its *blanks*.
- The *definitions* are where a workspace keeps what a person made of a batch: per label, the edits, and the settings as a template whose blanks are the columns.
- *Follow* scans the batch that follows at an interval and reduces a row once its files are listed, and again when the files change it at one of the columns `at` (`Automatic`).

A batch is one workflow over every row that the files of a workspace give:

| Part of a batch | Decided by | Kept in the definitions |
|---|---|---|
| spec and columns | code, such as `iofq_batch` | in the settings, once a person applies some |
| source | code | no |
| which files the source gets | `selector=` in code | no |
| label | code, or `--label` of the command | as the name of a folder |
| settings | a person | yes |
| edits | a person | yes |
| which batches a workspace has | nobody | no |

`create_app(batch)` serves one batch.
A person cannot do three things:

1. **End a series.** If the settings change after lunch, every row of the morning reads `completed, earlier settings` from then on, and "Reduce them again" offers to reduce it with the new settings. Nothing says that those rows are finished.
2. **Work on two sets of runs.** A person wants to reduce yesterday's runs again while the runs measured now are reduced as they arrive, and to look at one without stopping the other.
3. **Give each row the output of another workflow.** A beam centre per sample needs two batches and a source written for the pair (`with_centre` in batch.md, "Chains").

A batch also grows with its proposal: at 30 000 rows a state of the page takes 1.6 s and 18 MB ([#223](https://github.com/scipp/essapps/issues/223)).

How visiting users will organise an experiment is not known (see Open).
So this decision restricts a person as little as it can.
What makes the common case short, such as "Start the next batch here", is a button of a page and no rule of the model.

## Decision

A person makes a batch, names it, bounds it, and removes it.
A *manager* lists the batches of a workspace.
This solves problem 1, and problem 2 but for the order in which requests run (see Open).
Problem 3 is not decided (see "Not decided").

### The header of a batch

The definitions keep one more part per label, the *header*:

```json
{"spec": "loki-iofq",
 "columns": ["sample", "sample_transmission", "can"],
 "source": "sample-runs",
 "title": "Afternoon, 2 mm cells",
 "description": "New settings: the collimation changed at 13:00.",
 "scope": {"start": 60339, "stop": null}}
```

```
definitions/labels/20261118-130204/header.json     spec, columns, source, title, description, scope
definitions/labels/20261118-130204/edits.json      cells set, rows added or removed
definitions/labels/20261118-130204/settings.json   a template: the settings, with a copy of the spec and the columns
definitions/following.json                         the label of the batch that follows, or none
```

Making a batch writes its header and its first settings; the header keeps the spec and the columns.
A batch has one spec, and a column is a parameter of it.
What code passes to `Batch` and `create_app` comes from these places for a batch that a person makes:

| Argument | Comes from |
|---|---|
| spec | chosen from the specs the workspace binds (`client.specs()`). The name is kept; the version is the one bound, the highest if several are ([ADR 0009](0009-a-record-of-another-spec-stays-readable.md)) |
| `source`, `selector`, `words`, `row_name` | chosen as one from the sources that the application registers by name, next to its bindings. A registered source is the function, the columns it fills, the index of a row (below), and these arguments |
| `columns` | those the source fills, and the parameters that the person makes columns |
| `label` | generated from the time of making; the manager refuses a label that has definitions or records |
| first `settings`, `staged`, `draw` | registered by the application for the spec |
| `persist` | always, so the manager needs a workspace that persists, such as a folder |
| `at` of Follow | the columns that the source fills |
| `files`, `marks`, `interval`, `title` | of the application, given once for all batches |

A source is written for the parameter names of a spec.
So a batch can be made only with a source whose columns are parameters of its spec: `sample-runs` fills `sample` and `sample_transmission`, and fits `loki-iofq`.

A person changes the title, the description and the scope.
The names of the spec and the source, and the columns, are fixed when the batch is made.

### The scope picks rows, never files

A source gives each row an *index*: a value that orders the rows, such as the run number or the start time.

```python
{'start': 60339, 'stop': None}     # index values; start included, stop excluded, None is open
```

```python
scan = source(files with corrections)       # every file
rows = scan in scope, with edits on top
```

- The source sees every file, so a partner measured before the scope, such as the transmission run of a sample, is still found ([automatic-reduction.md](../automatic-reduction.md#why-the-history-is-not-cut-off)).
- A slice compares values. It needs no row at its ends, and a file listed late lands in or out by its value.
- `stop` is excluded, as in a Python slice, so a batch that starts where another stops leaves no gap and shares no row with it.
- A slice holds every row whose index is inside it, whenever its file is listed. With an open `stop` that is every new row.
- A row added by hand is in its batch whatever the scope. A batch without a source has only such rows.
- An edit of a row that the scope leaves out is kept and not used.
- "Load table…" refuses a table with a row that the source makes and the scope leaves out. Added by hand, such a row would no longer take its cells from the scan.

The cells of a batch with a `stop` are still those that the scan gives now.
If a later file changes a cell of one of its rows, the row reads `outdated`.
Follow then reduces it again if this batch is the one that follows; otherwise only a person does.
With LoKI's source, which gives a sample the last transmission run of its name, a standard whose transmission is measured each day makes yesterday's row of that standard `outdated`.

Batches are independent.
Two may hold the same row, with the same spec or another, and each reduces it under its own label.
Nothing checks that the batches cover every row or that they do not overlap.

### The manager

```python
manager = BatchManager(client, sources=SOURCES, definitions=definitions)
label = manager.create('loki-iofq', title='Afternoon', source='sample-runs',
                       columns=('sample', 'sample_transmission', 'can'),
                       scope=Scope(start=60339))
manager.headers()              # the header of each batch, by label
manager.batch(label)           # the Batch: state(), reduce(), cancel(), settings
manager.change(label, scope=Scope(start=60339, stop=60421))
manager.remove(label)
```

- The manager holds the one `Batch` of each label in its process, since two writers of one definition lose each other's changes.
- The counts of the page (rows per status of each batch, rows of each source in no batch) are how a person sees a row that became `outdated` or arrived in a batch that does not follow, and the rows that no batch holds. They take a scan and a state of every batch, so they are computed when asked for, not at each poll of a page.
- Removing a batch removes its header, edits and settings. Its records stay in the workspace, as all history does ([ADR 0004](0004-history-is-append-only-lists.md)).
- The command makes and reads its batch with the manager, so a batch made with `essapps-batch reduce --label` shows in the list. A `Batch` made in code without the manager has no header and is not listed.

### One batch follows

The manager keeps the label of the batch that follows, or none:

```python
manager.follow(label)   # this batch follows, and no other
manager.follow(None)    # none follows
manager.following       # what the switch of a page shows
```

- One value means that at most one batch follows, with no rule to enforce. Turning Follow on for one batch turns it off for the other.
- The manager keeps the value in the definitions, apart from any batch, so a restart resumes. Removing the batch that follows ends Follow.
- A step of Follow is one step of `Automatic` on `manager.batch(manager.following)`.
- Whoever serves the pages owns the timer that steps it, and the lock. The manager has no thread, no timer and no lock. It reads and writes definitions and hands out `Batch` objects.

A batch has no state "closed".
It receives no new rows once its scope has a `stop` at or below the newest index.
"Start the next batch here" sets the `stop` of one batch there, makes the next with the same spec, source and settings from that index on, and leaves Follow where it is.
The rows that move have no result under the next batch, so a step would reduce them all again, with settings that a person has not changed yet.

### A page shows one batch, named in its URL

```
/batches/20261118-130204/
```

The server holds no "current batch".
Reductions are records of the workspace and Follow is a thread of the server, so neither depends on what a browser shows.
A person starts a long reduction in one batch, looks at another, and comes back.

## Not decided: several workflows on one batch

Problem 3 needs a row table that several workflows share.
Chains written in code remain, for batches made in code.
A batch that a person makes has none: a source registered by name cannot read the state of a batch made later.

The direction is that a batch holds a list of workflows.
Each has its own settings and one result per row, says which column each of its parameters takes, and adds a column per output:

```json
{"workflows": [
  {"id": "detector", "spec": "loki-calibrated-detector", "takes": {"run": "sample"}},
  {"id": "centre", "spec": "loki-beam-centre", "takes": {"detector": "detector.detector"}},
  {"id": "iofq", "spec": "loki-iofq",
   "takes": {"sample": "sample", "sample_transmission": "sample_transmission",
             "beam_centre": "centre.centre"}}]}
```

`Batch` and `Automatic` as they are can run this, with one `Batch` per workflow: a cell may reference a record that is still running, so one pass of Follow submits every workflow for a new row.
`test_a_row_reads_the_record_of_its_row_in_a_batch_stepped_before` shows it for two.

It needs these decisions, and the first three change what a person sees:

1. **When a row is ready.** After an edit of `sample`, the detector reads `outdated`, the centre still reads `completed`, and "Reduce" at I(Q) would use the new file with the centre of the old one. A row's status at one workflow has to depend on the workflows before it.
2. **What "Reduce" means.** Whether it reduces a row at one workflow or at all of them in order, and whether a result that a person reduced again counts as arrived for the workflows after it. Follow reduces nothing that a person changed and did not ask to reduce (batch.md, "Automatic reduction").
3. **Acceptance.** A person inspects a value before a long reduction starts ([#162](https://github.com/scipp/essapps/issues/162)). A column that reads the newest result removes that check. A cell set by hand to one record would be the acceptance.
4. **Names.** A column is a parameter today. LoKI's detector takes `run` and the table calls that column `sample`, so parameters and columns get separate names. Edits, `at`, templates, the headers of a table file and the positions of errors then need a translation.
5. **A value shared by some rows**, such as the transmission of one transmission run used by several sample runs. It is neither one result per row nor a setting of all rows.
6. **Removing a workflow** whose output another one takes.

Whether to build it depends on how users work (see Open).
If it is built, the header, the labels and the URL change once more: a label per workflow, such as `20261118-130204/iofq`.

## Alternatives considered

- **One batch per workspace, with a range of rows shown** ([#223](https://github.com/scipp/essapps/issues/223)). It bounds the cost of the page and solves neither problem 1 nor 2: the batch still has one set of settings.
- **Each row keeps settings of its own.** A page would have to show and edit settings per row. A batch per series gives the same with the settings a batch already has.
- **The scope cuts the files given to the source.** A partner outside the scope is not found, and its rows wait for ever.
- **A slice by position.** A file listed late shifts every row after it, so a batch would hold other rows than the day before.
- **A slice with both ends included**, as `.loc` of pandas. With times as the index, a batch that starts where another stops would share a row with it or leave a gap.
- **Batches that split the rows between them.** It is a rule to enforce, and it forbids reducing one series with two sets of settings to compare them.
- **Follow run by the manager.** The manager would get a thread, a timer and a lock next to the definitions it keeps. What was built: the manager keeps which batch follows, as it keeps the headers, and the application that serves owns the timer and the lock.
- **Follow as a flag of each batch.** Every open-ended batch that follows reduces every new run. A bounded one reduces its rows again when a later file changes a cell, which for the standard measured each day is not wanted. One value can become a set later; a flag per batch is hard to take back.

## Consequences

- A person ends a series by giving its scope a `stop`. The settings of that batch stay as they are, so its rows no longer read `completed, earlier settings` when the next series gets other settings. A row can still read `outdated` if the files change its cells.
- A person looks at one batch while another runs. All batches share the workspace's queue, so a long reduction still delays the batch that follows (see Open).
- Problem 3 stays as it is for batches made in code, and is not available for batches that a person makes.
- `Batch` changes in two places: a row gets an index, and a batch has a scope that its scan applies and that can be set. The page of a batch stays, but for Follow.
- Follow moves from the page of one batch to the application that serves them all. The application owns the timer, and steps the batch that follows under the lock of the pages. The manager keeps which batch follows.
- The definitions get two operations they lack: listing the labels, and deleting a part. The layout of `definitions/` stays, with the header added.
- `essapps-batch reduce --label` makes and reads the batch of its label with the manager, and takes the registered sources as it takes the bindings. It looks the kept spec up by name and version today, and has to take the bound version as the manager does.
- Two rules change. Whether a batch follows is kept; today it belongs to the process, and a restart starts with `--follow` or without. `at` gets a default, the columns of the source; today the application chooses it.
- A batch that a person bounds has tens to hundreds of rows, so the cost per row does not arise ([#223](https://github.com/scipp/essapps/issues/223)). The cost per file stays whatever the scope: at 60 000 files the options of the file cells are 3.1 MB of each state, a new listing takes 1.4 s, and the Runs view 1.8 s.
- A batch without a `stop` remains, and is what an automatic reduction with no person has. Its page can show only its newest rows.
- Only the batch that follows scans at each step of Follow, so the cost of a step does not grow with the number of batches. A cache of the metadata of each file belongs in the file source, below the batches.
- The preparatory reductions of LoKI (detector, masks) fit as batches without a source, with one row. They are not part of the first work.
- Order of work, each part usable alone:
  1. The header and the manager, for batches made in code; a page per batch by URL, with the list; one loop of Follow for all of them.
  2. The index of a row, and the scope.
  3. Making, changing and removing a batch from the page, with the registered sources.

## Open

- **How users organise an experiment.** To be asked of instrument scientists: Do they think in series with a start and an end? Do they change settings for the runs that follow, or reduce everything again? How often does each row need a value that another workflow computes from it? The answers decide what the page offers, and whether several workflows on one batch are built.
- **Whether a `stop` also cuts the files.** The source of a batch could get only the files measured before its `stop`, so that no later file changes its rows. That needs the index on files as well as rows.
- **Which request runs first.** A workspace runs its requests on one pool of threads, one thread for a folder unless told otherwise, in the order submitted. A priority at submission would be a change of the workspace. To be measured with real runs first.
- **When the manager lets go of a batch.** Each `Batch` it holds keeps its stage and its page's state until closed. What hundreds of them cost is not measured.
- **A row without an index**, such as a file without a run number. A scope is a slice, so such a row is held only by a batch without a start and a stop. Naming rows by key is not built.
- **A label with a `/`** in the path of a URL. Generated labels have none, and the manager refuses one; code may give one to a `Batch` made without the manager.
- **Results of removed batches on disk.** Their records stay, and so do their persisted outputs. Nothing releases them.
- **Many batches.** The list has no order but that of making, and no archive.

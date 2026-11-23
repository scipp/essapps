# ADR 0011: A person makes the batches of a workspace

- Status: accepted. [ADR 0012](0012-a-batch-is-a-table.md) decides what a batch holds, and [ADR 0013](0013-follow-adds-the-rows-not-considered.md) how rows come into it.
- Deciders: Simon
- Date: 2026-11-18

## Context

*Spec*, *record* and *binding* are the words of [README.md](../README.md); *row*, *key*, *result*, *template* and *outdated* are those of [batch.md, "Terms"](../../../packages/essapps/docs/batch.md#terms).
These are used here:

- A *workspace* is a `Client` (the specs it binds, the files, the records) and its definitions; for a folder workspace, one folder.
- A *batch* is one spec reduced for the rows of a table, with settings shared by all rows. ADR 0012 says what it holds.
- The *label* of a batch names it and its results: a row's result is the newest record under the label with the row's key.
- The *header* of a batch is its title and its description.
- The *definitions* are what a workspace keeps besides files and records: per label, the header, the settings and the table of a batch.
- A *source* is code of the application that makes rows from the files.
- *Follow* reduces a batch as its runs arrive, by a *step* every few seconds. ADR 0013 says what a step does.

One batch per workspace leaves three things a person cannot do:

1. **End a series.** If the settings change after lunch, every row of the morning reads `earlier`, reduced with earlier settings, and "Reduce them again" offers to reduce it with the new settings. Nothing says that those rows are finished.
2. **Work on two sets of runs.** A person wants to reduce yesterday's runs again while the runs measured now are reduced as they arrive, and to look at one without stopping the other.
3. **Give each row the output of another workflow.** A beam centre per sample needs two batches and a source written for the pair. A cell can name an output of another record ([batch.md, "Chains"](../../../packages/essapps/docs/batch.md#chains)), but the column is filled in code.

One batch also grows with its proposal: at 30 000 rows a state of the page takes 1.6 s and 18 MB ([#223](https://github.com/scipp/essapps/issues/223)).

How visiting users will organise an experiment is not known (see Open).
So this decision restricts a person as little as it can.

## Decision

A person makes a batch, names it, and removes it.
A *manager* lists the batches of a workspace.
A series ends where a person makes the next batch, with settings of its own; the batch before keeps its settings.
This solves problem 1, and problem 2 but for the order in which requests run (see Open).
Problem 3 is not decided (see "Not decided").

### The manager

```python
manager = BatchManager(client, definitions=FolderDefinitions(folder))
intake = Intake(manager, sources={'sample-runs': SAMPLE_RUNS})
label = intake.create('loki-iofq', title='Afternoon', source='sample-runs').label
manager.headers()              # {label: Header(title, description)}, sorted by label
manager.batch(label)           # the Batch: frame(), put(), state(), reduce(), cancel(), settings
manager.change(label, title='Afternoon, 2 mm cells')
manager.remove(label)
```

- The manager holds the one `Batch` of each label in its process, since two writers of one definition lose each other's changes.
- A person changes the title and the description. The spec is fixed when the batch is made; ADR 0012 says how the settings and the table change.
- Removing a batch removes its definitions. Its records stay in the workspace, as all history does ([ADR 0004](0004-history-is-append-only-lists.md)).
- The command `essapps-batch reduce --label` makes and reads its batch with the manager, so it shows in the list. A `Batch` made in code without the manager has no header and is not listed.

What the application gives once, and what a person chooses for each batch:

| Part | Given by |
|---|---|
| spec | a person, from the specs that the workspace binds (`client.specs()`); the name is kept, the version is the one bound, the highest if several are ([ADR 0009](0009-a-record-of-another-spec-stays-readable.md)) |
| source | a person, from the sources that the application registers by name (`Intake(sources=...)`), or none |
| title, description | a person |
| label | the manager |
| first settings, `staged` | the application, for each spec (`BatchManager(settings=..., staged=...)`) |
| `draw`, `files`, `marks`, `interval`, `title` | the application, for all batches (`Pages`) |

A source is written for the parameter names of a spec.
So a batch is made with a source only if each column that the source fills is a parameter of the spec (`fits`): `sample-runs` fills `sample` and `sample_transmission`, and fits `loki-iofq`.

### A label is made by the manager and used once

A label is the time of making, `YYYYMMDD-HHMMSS`, with `-2`, `-3`, ... added if that is taken.
Code may give one; the manager refuses it unless it is letters, digits, `_`, `.` and `-`, starting with a letter, a digit or `_`, so that a label is one segment of the path of a URL.
The manager refuses a label that has definitions or records.
So the records of a removed batch never show as results of another batch.

### What the workspace keeps

```
definitions/labels/20261118-130204/header.json   {"title": "Afternoon, 2 mm cells", "description": "..."}
definitions/labels/20261118-130204/...           the settings and the table (ADR 0012), the source (ADR 0013)
definitions/following.json                       {"label": "20261118-130204"}, or {"label": null}
definitions/corrections.json                     metadata of runs set by hand
```

A label is a batch if and only if its header is kept.
The corrections of runs belong to the workspace and to no batch: every batch reads the files through one file source, and a person corrects a run on the page of any batch, or of none.

Batches are independent.
Two may hold the same row, with the same spec or another, and each reduces it under its own label.
Nothing checks that the batches cover every row or that they do not overlap.

### One batch follows

The intake keeps the label of the batch that follows, or none:

```python
intake.create(..., source='sample-runs', follow=True)    # this batch follows, and no other
intake.following                                         # its label, or None
intake.stop()                                            # none follows
intake.step()                                            # one step of Follow on it (ADR 0013)
```

- One value means that at most one batch follows, with no rule to enforce. Making a batch that follows stops the batch that followed before.
- The intake keeps the value in the definitions, apart from any batch, so a restart resumes. Removing the batch that follows ends Follow.
- Whoever serves the pages owns the timer that calls `step`. The intake has no thread, no timer and no lock.
- The workspace has one lock: every request of every page and each step take it, so the batches, the manager and the corrections change in one request at a time.

### A page shows one batch, named in its URL

```
/batches/20261118-130204/
/                            the page of the batch that follows, else of the only batch, else the list of batches
```

The server holds no "current batch".
Reductions are records of the workspace and Follow is a thread of the server, so neither depends on what a browser shows.
A person starts a long reduction in one batch, looks at another, and comes back.

## Not decided: several workflows on one batch

Problem 3 needs a table that several workflows share ([#222](https://github.com/scipp/essapps/issues/222)).
Chains written in code remain, for batches made in code.
A batch that a person makes on the page has none: a registered source cannot read the state of a batch made later.

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

`Batch` and `auto.step` can run this, with one `Batch` per workflow: a cell may reference a record that is still running, so one pass of Follow submits every workflow for a new row.
`test_a_row_reads_the_record_of_its_row_in_a_batch_stepped_before` shows it for two.

It needs these decisions, and the first three change what a person sees:

1. **When a row is ready.** After an edit of `sample`, the detector reads `outdated`, the centre still reads `done`, and "Reduce" at I(Q) would use the new file with the centre of the old one. A row's status at one workflow has to depend on the workflows before it.
2. **What "Reduce" means.** Whether it reduces a row at one workflow or at all of them in order, and whether a result that a person reduced again counts as arrived for the workflows after it.
3. **Acceptance.** A person inspects a value before a long reduction starts ([#162](https://github.com/scipp/essapps/issues/162)). A column that reads the newest result removes that check. A cell set by hand to one record would be the acceptance.
4. **Names.** A column is named as its parameter. LoKI's detector takes `run` and the table calls that column `sample`, so parameters and columns get separate names. The blanks of a template, the headers of a table file and the positions of errors then need a translation.
5. **A value shared by some rows**, such as the transmission of one transmission run used by several sample runs. It is neither one result per row nor a setting of all rows.
6. **Removing a workflow** whose output another one takes.

Whether to build it depends on how users work (see Open).
If it is built, the labels and the URL change: a label per workflow, such as `20261118-130204/iofq`.

## Alternatives considered

- **One batch per workspace, with a range of rows shown** ([#223](https://github.com/scipp/essapps/issues/223)). It bounds the cost of the page and solves neither problem 1 nor 2: the batch still has one set of settings.
- **Each row keeps all its settings.** A page would have to show and edit settings per row. A batch per series gives the same with the settings a batch already has.
- **Batches that split the rows between them.** It is a rule to enforce, and it forbids reducing one series with two sets of settings to compare them.
- **The manager owns the timer of Follow.** The manager would get a thread, a timer and a lock next to the definitions it keeps.
- **Follow as a flag of each batch.** Each batch that follows adds and reduces every new run, so a run is reduced once per such batch. One value can become a set later; a flag per batch is hard to take back.

## Consequences

- A person ends a series by making the next batch. The settings of the batch before stay as they are, so its rows do not read `earlier` when the next series gets other settings.
- A person looks at one batch while another runs. All batches share the workspace's queue, so a long reduction still delays the batch that follows (see Open).
- Problem 3 stays as it is for batches made in code, and is not available for batches that a person makes.
- Follow belongs to the application that serves all batches. The application owns the timer, and steps the batch that follows under the lock of the pages.
- Only the batch that follows lists its source at each step, so the cost of a step does not grow with the number of batches. A cache of the metadata of each file belongs in the file source, below the batches.
- A batch that holds one series has tens to hundreds of rows, so the cost per row of [#223](https://github.com/scipp/essapps/issues/223) does not arise. The cost per file stays.
- The preparatory reductions of LoKI (detector, masks) are batches without a source, with one row.

## Open

- **How users organise an experiment.** To be asked of instrument scientists: Do they think in series with a start and an end? Do they change settings for the runs that follow, or reduce everything again? How often does each row need a value that another workflow computes from it? The answers decide what the page offers, and whether several workflows on one batch are built.
- **Which request runs first.** A workspace runs its requests on one pool of threads, one thread for a folder unless told otherwise, in the order submitted. A priority at submission would be a change of the workspace. To be measured with real runs first.
- **When the manager lets go of a batch.** Each `Batch` it holds keeps its stage and its page's state until removed. What hundreds of them cost is not measured.
- **Results of removed batches on disk.** Their records stay, and so do their persisted outputs. Nothing releases them.
- **Many batches.** The list is sorted by label, which for generated labels is the order of making. It has no archive.

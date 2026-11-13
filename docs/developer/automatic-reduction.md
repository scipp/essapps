# Automatic reduction

This sub-design builds on the core of [README.md](README.md) and on the batch toolbox, `ess.apps.batch` ([its guide](../../packages/essapps/docs/batch.md)).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) places it.

## A batch reduced as its files arrive

The framework knows no technique: which runs make one reduction is the application's to say.
It says so once, as the *source* of a batch, a function from files to rows.
A person reducing a batch and the automatic reduction use the same source.

The batch is a chain of tables, of which only the files grow on their own:

```python
files   = client.files.list(selector)              # with their metadata
rows    = source(files)                            # Batch.scan: plain data in and out
results = records under the batch's label, by member      # the history, read as a table
state   = rows joined with results on key == member       # Batch.state
todo    = [key for key in state if runs(key)]      # the predicate of the next section
submit(todo)                                       # appends to the history, so results change
```

The automatic reduction is this chain stepped by a loop, `ess.apps.batch.auto.Automatic`:

```python
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'), label='auto/iofq',
              source=pair_by_sample, staged=True, persist=True)
auto = Automatic(batch, at='sample', settings=lambda: load_settings('iofq.yaml'))
run([auto])              # steps forever: scan, then reduce the rows with a new file at `sample`
```

`settings` is called at each step, so a setting changed during a beamtime applies to the rows not reduced yet.
The loop is a controller: it runs in a process of its own or in a notebook, never in the workspace.

## Which rows run

A person and the loop differ in one predicate, the `runs` above ([batch.md](../../packages/essapps/docs/batch.md#automatic-reduction) tables both).
For the loop, a row runs if its cell at the *trigger column* `at` holds a file that no record under the batch's label, with the row's key as its member, holds at `at`:

```python
def runs(row, held):      # held: the files at `at` of the records of the row's key
    return bool(files_at(row.values, at) - held) and ready(row)
```

`ready` is the batch's: the row is complete, and its result is not running.
A file at `at` is the cell's value, or an element of its list, as a series of angles gives it; a file deeper inside the value does not count.

| What happened to a row | Runs |
|---|---|
| no record holds its file at `at` | yes |
| its record failed or was cancelled | no |
| a setting changed, or a cell at another column | no |
| a new file joined its list at `at` | yes |

Each line answers a story or a failure mode:

- A failed record counts, so that a file that fails is not retried at every step. Running it again is a person's decision (D2).
- A changed setting does not reduce old rows again (E4). Their records keep the settings they ran with, and a person who wants them reduced again makes a batch with its own label.
- A later partner, such as a transmission run that the source pairs with rows made before, changes a cell at another column, and does not reduce those rows again.
- A series grows (E1): a new angle in a row's list reduces the row again with every angle so far, and an angle whose file arrives again keeps its identity and is not new.

The records are read whatever their spec version or submitter. So a new version of the spec, or a restart under another name, reduces no old row again.
A label is what a consumer group is to a stream: the records under it say which files are consumed, and nothing else does.
A new label starts from the first file, and a person's batch under its own label does not stop the loop from reducing a row; a record that another client submits under the loop's label, with a row's key as its member, does.

A row that is running waits: the loop submits it again, with its new file, once its record has finished.

## What the loop keeps

Which files the records of each key hold is read from the proposal's changes (`client.changes`), from the start when the loop starts.
So a restarted loop reduces no row again, and keeps nothing on disk of its own.
This holds while the proposal's history is kept. A running loop keeps it, since its calls keep the proposal from being idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

Besides, the loop keeps the rows and the settings of its last step, which spare it work only.
A step looks at the rows whose cells changed, those with a record that changed, and, if the settings changed, those that were refused:

| A row waits because | It is looked at again when |
|---|---|
| a cell is empty, such as a missing transmission run | its cells change |
| its result is running | its record changes |
| its request was refused, such as an invalid value | its cells or the settings change |

The batch works out the state of those rows only (`Batch.state(settings, keys)`).

## Refusals

`Batch.reduce` is all or none, as a person needs it: one invalid row refuses the call, and the error names it.
The loop instead submits the rows, drops those the `SubmitError` names (`FieldError.entry`), and submits the rest, so a refused row does not hold back the others.
A reason that names no row is about the settings, such as a spec the workspace no longer runs (E2), and refuses every row.
`Automatic.refused` holds the reason of each row that has a new file and did not run, by key, so that an application can show why. Why a row waits is also its `RowState.problems`.

## Chains

A row may reference the output of a row of another batch, as a sample reads the transmission computed from its transmission run.
The second batch's source reads the first batch's state, and the loop steps the first batch before the second:

```python
transmissions = Batch(client, TRANSMISSION, columns=('run',), label='auto/transmission', ...)

def samples(files):
    done = {transmissions.rows[k].info['sample']: s.record
            for k, s in transmissions.state().items() if s.record}
    return [Row(key, {'sample': run, 'transmission': done[name].ref('transmission')}) ...]

run([Automatic(transmissions, at='run', settings=...),
     Automatic(Batch(client, IOFQ, ..., source=samples), at='sample', settings=...)])
```

A sample that arrives before its transmission run waits with an empty cell.
In the step in which the transmission is submitted, the sample's row references the pending record, so both run in one step.
If the transmission's record has failed by the time the sample's row is made, the row waits: a cell reads a failed record.
If it fails after, the sample's record fails with it and holds the sample's file, so the row does not run again.
A source is recomputed at every step, so a chain survives a restart, which a reference set by hand in a cell (an edit) does not.

## Cost

A step lists every file, runs the source over all of them, and compares the rows with those of the last step.
That is linear in the number of files, about 4 µs per file and 3 µs more to list it in the fake file source.
The state and the submission cost only the rows looked at.
A loop that starts looks at every row once.

| Runs reduced | 3 000 | 30 000 | 100 000 |
|---|---|---|---|
| a step with one new file, best of ten | 12 ms | 0.19 s | 0.74 s |
| of which listing the files | 5 ms | 75 ms | 0.33 s |
| mean of ten | 25 ms | 0.39 s | 1.4 s |

Measured with a toy workflow, the fake file source, and a history in memory.
The mean is about twice the best because of Python's garbage collector: each step makes a row for each file, and the collector then walks every object of the process, the records among them.

## Open

- **New files since a position.** `FileSource.list` returns every file, so each step lists the whole proposal. A catalogue cannot be listed whole at each step; the file source needs a call that returns what is new since a position, as `client.changes` does for records.
- **A step below linear.** The source runs over all files because nothing says which rows a new file can touch. An optional group per file would say it: a step would then make the rows of the groups that got a file, from those files and the files shared by all groups.

  ```python
  Batch(..., source=pair_by_sample,
        group=lambda f: None if f.metadata['role'] == 'can' else f.metadata['sample'])  # None: shared
  ```

  A source that pairs by time, such as "the can measured before", has no such group and stays linear.
- **Starting a new label at a given run.** A new label consumes from the first file. Starting it at a run needs a range in `Selector`, which matches equal values only.
- **Old rows reduced again for a new spec version.** One more term of the predicate, from the spec of each row's result (ADR 0009): `runs(row, held) or result.spec != spec`.
- **A loop that pushes into an accumulator**, for a sum that grows with each file (D7). The accumulator would be the only thing the workspace keeps for the loop, which would freeze it with `persist=` once the sum is complete.
- **The application around the loop**: one process per instrument, a workspace per proposal, the configuration files, and a page that shows the batches. The page and the loop would use one `Batch` from two threads, and `Batch` has no lock.

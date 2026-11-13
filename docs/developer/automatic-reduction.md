# Automatic reduction

This sub-design builds on the core of [README.md](README.md) and on the batch toolbox, `ess.apps.batch` ([its guide](../../packages/essapps/docs/batch.md)).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) places it.

## A batch reduced as its files arrive

The framework knows no technique: which runs make one reduction is the application's to say.
It says so once, as the *source* of a batch, a function from files to rows.
A person reducing a batch and automatic reduction use the same source.

The batch is a chain of tables, of which only the files grow on their own:

```python
files   = client.files.list(selector)              # with their metadata
rows    = source(files)                            # Batch.scan: plain data in and out
results = records under the batch's label, by member      # the history, read as a table
state   = rows joined with results on key == member       # Batch.state
todo    = [key for key in state if runs(key)]      # the predicate of the next section
submit(todo)                                       # appends to the history, so results change
```

Automatic reduction steps this chain with a *loop*, an `ess.apps.batch.auto.Automatic`:

```python
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'), label='auto/iofq',
              source=pair_by_sample, staged=True, persist=True)
loop = Automatic(batch, at='sample', settings=lambda: load_settings('iofq.yaml'))
run([loop])              # steps forever: scan, then reduce the rows with a new file at `sample`
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

The records are read whatever their spec or submitter. So a new version of the spec, or a restart under another name, reduces no old row again.
A label is what a consumer group is to a stream: the records under it say which files are consumed, and nothing else does.
A loop on a new label starts from the first file. A person's batch under its own label does not stop the loop from reducing a row; a record that another client submits under the loop's label, with a row's key as its member, does.

A row that is running waits: the loop submits it again, with its new file, once its record has finished.

## What the loop keeps

Which files the records of each key hold, `held` above, is read from the proposal's changes (`client.changes`), from the start when the loop is made.
So a restarted loop reduces no row again, and keeps nothing on disk of its own.
This holds while the proposal's history is kept. A running loop keeps it, since its calls keep the proposal from being idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

Each step checks every row against `held`, so a step that raises, in the changes, the settings, the source, or a submission, loses nothing: the next step tries again.
Besides `held`, the loop keeps the settings of the last step and the rows it could not submit, each as it was then, so that it does not try them at every step:

| A row did not run because | It is tried again when |
|---|---|
| a cell is empty, such as a missing transmission run | its cells change |
| its result is running | its record changes |
| its request was refused, such as an invalid value | its cells, its records, or the settings change |

## Refusals

`Batch.reduce` is all or none, as a person needs it: one invalid row refuses the call, and the error names it.
The loop instead submits the rows, drops those the `SubmitError` names (`FieldError.entry`), and submits the rest, so a refused row does not hold back the others.
A reason that names no row is about the settings, such as a spec the workspace no longer runs (E2), and refuses every row.
`Automatic.refused` holds the reason of each row that has a new file and did not run, by key, so that an application can show why. Why a row waits is also its `RowState.problems`.

## Chains

A row may reference the output of a row of another batch, as a sample reads the transmission computed from its transmission run.
The second batch's source reads the first batch's state, and `run` steps the loop of the first batch before that of the second:

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
The source reads the state of every row of the first batch at every step.

## Cost

Some instruments in full operation are expected to write 10 000 to 100 000 files per proposal, and a file every few seconds.
The numbers below are measured against that scale.

A step lists every file, runs the source over all of them, and checks each row's cell at `at` against `held`.
That is linear in the number of files.
To submit the rows that run, the batch reads every record under the label to find the result of each of those rows, and asks for the status of those rows only.
A loop that is made reads every record of the proposal once.

| Files reduced | 3 000 | 30 000 | 100 000 |
|---|---|---|---|
| a step with one new file, best of ten | 15 ms | 0.21 s | 0.82 s |
| of which listing the files | 5 ms | 82 ms | 0.32 s |
| mean of ten | 30 ms | 0.40 s | 1.4 s |

Measured with a toy workflow, the fake file source, and a history in memory, on a machine shared with other work.
The mean is about twice the best because of Python's garbage collector: each step makes a row for each file, and the collector then walks every object of the process, the records among them.

A step costs the same for one new file or fifty, so the interval of `run` sets the load on one core, `step / (step + interval)`.
With a step of about 1 s at 100 000 files, that is 50 % at an interval of 1 s, 9 % at 10 s, and 3 % at 30 s.
The price is that a new file waits up to one interval.
An interval that follows the step time, such as `max(1 s, 10 × the last step)`, would keep the load near 10 % at any number of files; it is not built.

## Why the history is not cut off

A cut-off by age or by count, of the files given to the source, would bound the cost of a step, but it changes the results:

- A partner older than the cut-off, such as a can measured at the start, is gone, and its row waits for ever.
- A sample measured again after the cut-off gets a row with its later runs only, which is reduced with those, and nothing shows it.

A cut-off that a person sets on purpose, "only the files since Tuesday", is a selector on the files given to the source.

## Open

- **New files since a position.** `FileSource.list` returns every file, so each step lists the whole proposal. A catalogue cannot be listed whole at each step; the file source needs a call that returns what is new since a position, as `client.changes` does for records.
- **Reading the records of chosen members.** The state of the rows that run reads every record under the label (`client.records(label=...)`) and keeps those of the rows' keys. A remote client cannot read 100 000 records at each step.
- **A step below linear.** The source runs over all files because nothing says which rows a new file can touch. Two ways would say it; neither is prototyped, and a spike on LoKI's pairing and on a pairing by time would show which is better:
  - *A group per file*, declared by the author. A step makes the rows of the groups that got a file, from those files and the files shared by all groups. A source that pairs by time, such as "the can measured before", has no such group and stays linear.

    ```python
    Batch(..., source=pair_by_sample,
          group=lambda f: None if f.metadata['role'] == 'can' else f.metadata['sample'])  # None: shared
    ```

  - *A row from one file, by queries on an index of the files.* The framework records what each row asked for. It needs no declaration by the author, and changes the signature of every source.

    ```python
    def row_of(file: FileInfo, files: FileIndex) -> Row | None:
        if file.metadata.get('role') != 'sample':
            return None
        partners = files.list(Selector(role='transmission', sample=file.metadata['sample']))
        ...
    # the framework records that this row asked for (role=transmission, sample=water):
    # a new file with that metadata makes this row again, and no other
    ```

  A class with state that the author writes, `add(file) -> changed rows`, is not offered. It would have to give the same rows for any order of arrival, a role corrected later, a file that arrives again, and a restart, which a function over the files gives without effort.
- **A cell at `at` that names a file.** Files are compared by identity, as `FileInfo.ref` gives it. A source that writes `file(run=1)` at `at` makes a row that no record holds, since the record holds the identity, so the row runs at every step. Comparing them needs the file source's `resolve`, which a client cannot reach.
- **Starting a new label at a given run.** A loop on a new label consumes from the first file. Starting it at a run needs a range in `Selector`, which matches equal values only.
- **Old rows reduced again for a new spec version.** One more term of the predicate, from the spec of each row's result (ADR 0009): `runs(row, held) or result.spec != spec`.
- **The files held at `at`, by key, for a page.** A page reads `Batch.state`, which counts the results of the batch's spec only. After a new version of the spec, a page shows the old rows as not reduced, while the loop holds them.
- **A loop that pushes into an accumulator**, for a sum that grows with each file (D7). The accumulator would be the only thing the workspace keeps for the loop, which would freeze it with `persist=` once the sum is complete.
- **The application around the loop**: one process per instrument, a workspace per proposal, the configuration files, and a page that shows the batches. The page and the loop would use one `Batch` from two threads, and `Batch` has no lock.

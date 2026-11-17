# Automatic reduction

This sub-design builds on the core of [README.md](README.md) and on the batch toolbox, `ess.apps.batch` ([its guide](../../packages/essapps/docs/batch.md)).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) places it.

## A batch reduced as its files arrive

The framework knows no technique: which runs make one reduction is the application's to say.
It says so once, as the *source* of a batch, a function from files to rows.
A person reducing a batch and automatic reduction use the same source.

The batch is a chain of tables, of which only the files and the records grow on their own:

```python
files   = client.files.list(selector)                   # with their metadata
rows    = source(files)                                 # Batch.scan: plain data in and out
results = records under the batch's label, by member    # the history, read as a table
state   = rows with settings, joined with results on key == member   # Batch.state()
todo    = rule(state)                                   # remaining for a person, arrived for the loop
submit(todo)                                            # appends to the history, so results change
```

Automatic reduction steps this chain with a *loop*, an `ess.apps.batch.auto.Automatic`:

```python
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'), label='auto/iofq',
              source=pair_by_sample, staged=True, persist=True, settings=defaults)
loop = Automatic(batch, at={'sample'})
run([loop])              # steps until one raises

# one step
batch.scan()
state = batch.state()
batch.reduce(arrived(state, at))
```

A step reads the batch's settings, so a setting changed during a beamtime, `batch.settings = new`, applies to the rows reduced from then on.
The loop is a controller: it runs in a process of its own or in a notebook, never in the workspace.

## Which rows run

A person and the loop differ in one rule, `rule` above ([batch.md](../../packages/essapps/docs/batch.md#automatic-reduction) tables both):

```python
def remaining(state):      # a person's "reduce remaining"
    return [key for key in ready(state)
            if state[key].status in (None, FAILED, CANCELLED) or state[key].outdated]

def arrived(state, at):    # the loop
    return [key for key in ready(state)
            if state[key].record is None or state[key].changed & at]
```

`ready` is the batch's: the row is complete, and its result is not running.
`changed` names the parameters at which the row's request differs from its result's request, both resolved as a record holds them.
`at` is a set of columns that the application names, with no default: the columns whose change reduces a row again.

| What happened to a row | `arrived` | `remaining` |
|---|---|---|
| it has no result | yes | yes |
| its result failed or was cancelled | no | yes |
| a setting changed | no | yes, but for a tweak, which is `outdated` only where a column changed |
| a cell changed at a column of `at`, such as a run that joins its list | yes | yes |
| a cell changed at another column | no | yes |
| its result is of another spec | at the columns of `at` the other spec lacks | yes |

Each line answers a story or a failure mode:

- A failed record counts, so that a file that fails is not retried at every step. Running it again is a person's decision (D2), or follows a change at `at`.
- A changed setting does not reduce old rows again (E4). Their records keep the settings they ran with; the rows read `outdated`, and a person reduces them through the loop's batch if they want.
- A later partner, such as a transmission run that the source pairs with rows made before, changes a cell at a column outside `at`, and does not reduce those rows again.
- A series grows (E1): a new angle in a row's list at `at` reduces the row again with every angle so far, and an angle whose file arrives again keeps its identity and changes nothing.
- A cell that names a file by run, `file(run=1)`, resolves to the identity that the record holds, so it is not a change.

A result of another spec, such as an older version, stays the row's result.
`differs` counts a parameter that one of the two requests lacks as changed, so the row is reduced again if the other spec lacks a column of `at`, and is otherwise compared as any other.
A row whose result is running is not ready. Once the record has finished, the next step compares the row with it, and reduces it if a file arrived at `at` meanwhile.

A list is compared element by element, in order, so a source gives a list cell in one order at every scan, such as sorted by run: `FileSource.list` promises no order, and a list in another order, or shorter, reduces the row again.

## One label, one batch

Which rows are reduced follows from the records under the label and from the cells of the loop's rows.
The loop keeps the results under its label equal to its batch's rows at the columns of `at`.
So a label has one batch:

```python
page = Batch(client, IOFQ, ..., label='auto/iofq', source=pair_by_sample)   # a second batch on the loop's label
page.rows = page.rows.edit('60339', {'sample': file(run=60400)})
page.reduce(['60339'])             # the row's result now holds run 60400 at `sample`
loop.step()                        # differs from the loop's row at `at`: reduced again with the source's file
```

A record that another `Batch` submits under the label with other cells at `at` is replaced at the next step.
A person who wants another file for a row edits the loop's own batch, or corrects the file's metadata so that the source gives another file.
Two loops never share a label: if their rows differ at `at`, each replaces the other's records at every step.
A record under the label that equals the loop's row at `at`, from any submitter, counts as the row's result.
A loop on a new label starts from the first file. A person's batch under its own label does not touch the loop's rows.

## What the loop keeps

Nothing that decides which rows run.
A step reads the files, the batch's rows and settings, and the records under the label, and works out the rest.
`Automatic` keeps its batch, `at`, and `refused`, the reasons of the last step, which no step reads.
So a step that raises, in the settings, the source, or a submission, loses nothing: the next step tries again.
A restarted loop reduces no row again whose cells come from the source alone.
The batch's definitions keep its edits and settings ([`ess.apps.batch.definition`](../../packages/essapps/src/ess/apps/batch/definition.py)), so with them an edited row is not reduced again either.
Without one, the edits are gone at a restart, and a row that was edited at a column of `at` is reduced again with the source's cells.
A record that was running when the workspace ended is failed at the restart, and one that had not started is cancelled unless it persists outputs ([`Workspace._restart`](../../packages/essapps/src/ess/apps/workspace.py)); the loop does not run such a row again, and a person retries it.
This holds while the proposal's history is kept. A running loop keeps it, since its calls keep the proposal from being idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

The `Batch` keeps what it reads of the records, the newest of each member under the label, from the proposal's changes, and how it resolved each cell until the next scan.
Both are caches that the records and the file source rebuild.
It also keeps which results are its own tweaks, which changes `outdated` but not `changed`, so not what `arrived` gives.

## Problems and refusals

A *problem* is found when the state is read, a *refusal* when a row is submitted.
A row whose cell the workspace does not accept, such as a thickness below zero or a file named before it is measured, is `incomplete`: `Batch.state(settings)` resolves the cells it compares, and the reason is one of the row's `problems`.
So `arrived` does not give it, the page shows why, and the loop reduces the row once the cell is accepted.

`submit` checks more than `client.resolve`, which looks at values only ([`WorkspaceProtocol.resolve`](../../packages/essapps/src/ess/apps/protocol.py)).
What only `submit` refuses, and how a batch row meets it:

| `submit` refuses | A batch row meets it when |
|---|---|
| a reference to an output of a record that the workspace does not keep and that does not persist it | a chain's first batch does not persist, and the workspace restarted or released the record |
| a reference to a record that failed or was cancelled after the state was read | the record finished between `state` and `reduce`; the next step shows the row `incomplete` |
| a reference to a record that the workspace does not know | a cell set by hand, or a source, names a record of another proposal or one made up |
| a reference to an accumulator | a cell references an accumulator in place of the record of its freeze |
| a reference to an output of a record of another spec than the one bound | the first batch's records are of a spec version the workspace no longer binds |
| a reference whose data do not fit the field | a source references the wrong output |
| a row still running | another client submitted the row between `state` and `reduce` |
| `persist` names an output the spec lacks, or the workspace has no store | the batch is made with such a `persist`; the reason names no row |
| a stage that stopped, was released, or holds other values | a staged batch; `Batch` opens a new stage and submits once more |

`Batch.reduce` is all or none, as a person needs it: one invalid row refuses the call, and the error names it.
The loop instead drops the rows that the `SubmitError` names (`FieldError.entry`), and submits the rest, so a refused row does not hold back the others.
A reason that names no row refuses every row of the step.
`Automatic.refused` holds the reasons of the last step by key, for a page to show.
Nothing is kept to spare a retry, so a refused row is submitted again at every step, and refused again until what it references changes.

Settings that the workspace does not accept make `Batch.state()` raise a `SubmitError`, since no row can run with them.
The step then submits nothing, and `refused` holds the reason for every ready row, such as `{'1': 'unknown spec sans-iofq/v1'}` after an upgrade removed the spec (E2).

## Chains

A row may reference the output of a row of another batch, as a sample reads the transmission computed from its transmission run.
The second batch's source reads the first batch's state, and `run` steps the loop of the first batch before that of the second:

```python
transmissions = Batch(client, TRANSMISSION, columns=('run',), label='auto/transmission', ...)

def samples(files):
    done = {transmissions.rows[k].info['sample']: s.record
            for k, s in transmissions.state().items() if s.record}
    return [Row(key, {'sample': run, 'transmission': done[name].ref('transmission')}) ...]

run([Automatic(transmissions, at={'run'}),
     Automatic(Batch(client, IOFQ, ..., source=samples), at={'sample'})])
```

A sample that arrives before its transmission run waits with an empty cell.
In the step in which the transmission is submitted, the sample's row references the pending record, so both run in one step.
If the transmission's record has failed by the time the sample's row is made, the row waits: a cell reads a failed record.
If it fails after, the sample's record fails with it, and the row is not reduced again unless a cell at `at` changes.
A transmission reduced again gives a new record, so the sample's cell `transmission` changes: the row is reduced again if `transmission` is in `at`, and reads `outdated` otherwise.
A source is recomputed at every step, so a chain follows each new record of the first batch, which a reference set by hand in a cell (an edit) does not.
The source reads the state of every row of the first batch at every step.

## Cost

Some instruments in full operation are expected to write 10 000 to 100 000 files per proposal, and a file every few seconds.
The numbers below are measured against that scale.

A step lists every file, runs the source over all of them, and reads the state of every row:

```python
batch.scan()                    # every file listed, the source run over all
state = batch.state()           # per row: its problems, its cells compared with its result's
                                # request, the status of its result; the settings resolved once
```

A cell of a row with a result is compared as given first, and resolved only if it differs, such as a file named by run, whose record holds its identity.
`scan` drops what the batch kept resolved, since a file may have gained a catalogue identifier, so such a cell is resolved again at every step, at about 0.13 ms each, whether its column is in `at` or not.
This is the slow regime: 15 s per step at 100 000 rows whose transmission is named by run, against 2.8 s for the same rows with files by identity.
A row without a result has every cell resolved, which the first step pays.

| Rows (2 files each) | 10 000 | 30 000 | 100 000 |
|---|---|---|---|
| all reduced, nothing arrives | 0.19 s | 0.72 s | 2.8 s |
| all reduced, one file arrives | 0.20 s | 0.77 s | 4.2 s |
| all reduced, each row's transmission named by run, nothing arrives | 1.3 s | 4.4 s | 15 s |
| none reduced: the first step, which submits every row | 5.7 s | 18 s | 61 s |

Best of five steps, with a toy workflow, a file source that finds a name by run in one lookup, and a history in memory, on a machine shared with other work.
The mean is up to 1.5 times the best, because of Python's garbage collector, which walks every object of the process, the records among them.
Of a step at 30 000 rows with nothing named by run, listing the files is about 40 %, finding the problems of each row and the references in its cells about 30 %, comparing the cells with the results about 20 %, and the status of every result 5 %.

A step costs the same for one new file or fifty, so the interval of `run` sets the load on one core, `step / (step + interval)`.
With a step of about 3 s at 100 000 rows, that is 75 % at an interval of 1 s, 23 % at 10 s, and 9 % at 30 s; with every row naming a file by run, a step of 15 s keeps a core busy at any interval below a minute.
The price is that a new file waits up to one interval.
An interval that follows the step time, such as `max(1 s, 10 × the last step)`, would keep the load near 10 % at any number of files; it is not built.

## Why the history is not cut off

A cut-off by age or by count, of the files given to the source, would bound the cost of a step, but it changes the results:

- A partner older than the cut-off, such as a can measured at the start, is gone, and its row waits for ever.
- A sample measured again after the cut-off gets a row with its later runs only, which is reduced with those, and nothing shows it.

A cut-off that a person sets on purpose, "only the files since Tuesday", is a selector on the files given to the source.

## Open

- **New files since a position.** `FileSource.list` returns every file, so each step lists the whole proposal. A catalogue cannot be listed whole at each step; the file source needs a call that returns what is new since a position, as `client.changes` does for records.
- **Reading the records of chosen members.** A batch reads every record under its label once, at its first state. Each later state reads the records that the proposal's changes name since, under any label, and keeps those under its own. A remote client would read 100 000 records when a batch is made, and every record that another batch of the proposal submits or finishes. A step also asks for the status of every row's result, 100 000 at that scale.
- **A cell that names a file by run, resolved at every step.** `scan` drops every value a batch kept resolved, so a cell that names a file by run is resolved again at every step, about 15 s at 100 000 rows. Keeping a value until the file it names changes identity needs the file source to say which files did.
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
- **A step that raises ends `run`.** A later step would lose nothing, but `run` does not take it: which errors a service logs and steps past, such as a lost connection, is not decided.
- **Starting a new label at a given run.** A loop on a new label consumes from the first file. Starting it at a run needs a range in `Selector`, which matches equal values only.
- **Old rows reduced again for a new spec version.** One more term of `arrived`, from the state of each row (ADR 0009): `state[key].record is None or state[key].changed & at or state[key].spec_changed`.
- **A loop that pushes into an accumulator**, for a sum that grows with each file (D7). The accumulator would be the only thing the workspace keeps for the loop, which would freeze it with `persist=` once the sum is complete.
- **The application around the loop**: one process per instrument, a workspace per proposal, the configuration files, and a page that shows the batches. The batch page holds the loop of one batch: its switch "Follow" steps an `Automatic` on the page's `Batch` from a timer thread, under the lock that every request of the page takes (`ess.apps.batch.web.Server.step`). A step that raises there is shown on the page, and the next step runs.

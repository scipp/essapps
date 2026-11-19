# Automatic reduction

This sub-design builds on the core of [README.md](README.md) and on the batch toolbox, `ess.apps.batch` ([its guide](../../packages/essapps/docs/batch.md)).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) places it.

## A batch reduced as its files arrive

The framework knows no technique: which runs make one reduction is the application's to say.
It says so once, as the *source* of a batch: a `Source`, which holds a function from files to rows and the columns that its rows fill.
A person reducing a batch and automatic reduction use the same source.

[The model](../../packages/essapps/docs/batch.md#the-model-tables-and-one-cycle) shows a batch as stored tables, derived tables, and one cycle: a rule picks rows from the state, and `Batch.reduce` submits them.

Automatic reduction steps this cycle with a *loop*, an `ess.apps.batch.auto.Automatic`:

```python
SAMPLE_RUNS = Source(pair_by_sample, fills=('sample', 'sample_transmission'))
batch = Batch(client, IOFQ, columns=('sample', 'sample_transmission'), label='auto/iofq',
              source=SAMPLE_RUNS, staged=True, persist=True, settings=defaults)
loop = Automatic(batch)
run([loop])              # steps until one raises

# one step
batch.scan()
batch.reduce(not_reduced(batch.state()))
```

A step reads the batch's settings, so a setting changed during a beamtime, `batch.settings = new`, applies to the rows reduced from then on.
The loop is a controller: it runs in a process of its own or in a notebook, never in the workspace.

## Which rows run

A person and the loop differ in the rule of the cycle ([batch.md](../../packages/essapps/docs/batch.md#automatic-reduction) tables each). `left_to_reduce` stands for a person here:

```python
def left_to_reduce(state, columns):   # the page's Reduce, and essapps-batch reduce --remaining
    return [key for key in ready(state)
            if (state[key].status in (None, FAILED, CANCELLED) or state[key].outdated)
            and key not in earlier_settings(state, columns)]

def not_reduced(state):    # the loop
    return [key for key in ready(state) if state[key].record is None]
```

`ready` is the batch's: the row is complete, and its result is not running.
A row with a result is never reduced by a step: not one that is outdated, not one that failed or was cancelled. A person reduces it ([ADR 0013](adr/0013-follow-adds-the-rows-not-considered.md), "What a step reduces").

| What happened to a row | `not_reduced` | `left_to_reduce` |
|---|---|---|
| it has no result, whether the scan or a person put it into the batch | yes | yes |
| its result failed or was cancelled | no | yes |
| a setting changed | no | no: the row is one of `earlier_settings`, which a person reduces with "Reduce them again" (`--earlier-settings`) |
| a cell changed, by the scan or by a person | no | yes |
| its result is of another spec | no | yes |

Each line answers a story or a failure mode:

- A failed record counts, so that a file that fails is not retried at every step. Running it again is a person's decision (D2).
- A changed setting does not reduce old rows again (E4). Their records keep the settings they ran with; the rows read `outdated`, and a person reduces them through the loop's batch if they want.
- A later partner, such as a newer transmission run that the source pairs with rows made before, reduces no row again: the rows read `outdated`, and a person reduces the ones they choose.
- A series that grows (E1) is not followed: the row of a sample is reduced once, with its first angle. The story is not met.
- A row that a person adds or edits before it has a result is reduced by the next step once it is complete, as a row of the scan is. So a person who sets two cells of such a row while the batch follows may see it reduced between the two.

A result of another spec, such as an older version, stays the row's result, and the row reads `outdated`.
A row whose result is running is not ready, and once the record has finished, the row has a result.

## One label, one batch

Which rows are reduced follows from the records under the label: a step reduces the ready rows of its batch that have no record under the label.
A record under the label with the row's key as its member, from any submitter or any batch, is the row's result, so the step leaves the row to a person.
Two batches on one label share their results: what one reduces, the other does not.
A loop on a new label starts from the first file, or from the first row of the scope of its batch. A person's batch under its own label does not touch the loop's rows.

## What the loop keeps

Nothing that decides which rows run.
A step reads the files, the batch's rows and settings, and the records under the label, and works out the rest.
`Automatic` keeps its batch and `refused`, the reasons of the last step, which no step reads.
So a step that raises, in the settings, the source, or a submission, loses nothing: the next step tries again.
A restarted loop reduces no row again that has a result.
A record that was running when the workspace ended is failed at the restart, and one that had not started is cancelled unless it persists outputs ([`Workspace._restart`](../../packages/essapps/src/ess/apps/workspace.py)); the loop does not run such a row again, and a person retries it.
This holds while the proposal's history is kept. A running loop keeps it, since its calls keep the proposal from being idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

The `Batch` keeps what it reads of the records, the newest of each member under the label, from the proposal's changes, and how it resolved each cell until the next scan.
Both are caches that the records and the file source rebuild.

## Problems and refusals

A *problem* is found when the state is read, a *refusal* when a row is submitted.
A row whose cell the workspace does not accept, such as a thickness below zero or a file named before it is measured, is `incomplete`: `Batch.state()` resolves the cells it compares, and the reason is one of the row's `problems`.
So `not_reduced` does not give it, the page shows why, and the loop reduces the row once the cell is accepted.

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
The step then submits nothing, and `refused` holds the reason for every ready row without a result, such as `{'1': 'unknown spec sans-iofq/v1'}` after an upgrade removed the spec (E2).

## Chains

A row may reference the output of a row of another batch, as a sample reads the transmission computed from its transmission run.
The second batch's source reads the first batch's state, and `run` steps the loop of the first batch before that of the second:

```python
transmissions = Batch(client, TRANSMISSION, columns=('run',), label='auto/transmission', ...)

def samples(files):
    done = {transmissions.rows[k].info['sample']: s.record
            for k, s in transmissions.state().items() if s.record}
    return [Row(key, {'sample': run, 'transmission': done[name].ref('transmission')}) ...]

iofq = Batch(client, IOFQ, ..., source=Source(samples, fills=('sample', 'transmission')))
run([Automatic(transmissions), Automatic(iofq)])
```

A sample that arrives before its transmission run waits with an empty cell.
In the step in which the transmission is submitted, the sample's row references the pending record, so both run in one step.
If the transmission's record has failed by the time the sample's row is made, the row waits: a cell reads a failed record.
If it fails after, the sample's record fails with it, and a person reduces the row again.
A transmission reduced again gives a new record, so the sample's cell `transmission` changes, and the row reads `outdated`.
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
`scan` drops what the batch kept resolved, since a file may have gained a catalogue identifier, so such a cell is resolved again at every step, at about 0.13 ms each.
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

A cut-off that a person sets on purpose, "only the runs from 60339 on", is the scope of a batch: a slice of the index of its rows, such as the run number. It picks rows of the scan, and the source still sees every file ([batch.md, "Rules"](../../packages/essapps/docs/batch.md#rules)).

## Open

- **New files since a position.** `FileSource.list` returns every file, so each step lists the whole proposal. A catalogue cannot be listed whole at each step; the file source needs a call that returns what is new since a position, as `client.changes` does for records.
- **Reading the records of chosen members.** A batch reads every record under its label once, at its first state. Each later state reads the records that the proposal's changes name since, under any label, and keeps those under its own. A remote client would read 100 000 records when a batch is made, and every record that another batch of the proposal submits or finishes. A step also asks for the status of every row's result, 100 000 at that scale.
- **A cell that names a file by run, resolved at every step.** `scan` drops every value a batch kept resolved, so a cell that names a file by run is resolved again at every step, about 15 s at 100 000 rows. Keeping a value until the file it names changes identity needs the file source to say which files did.
- **A step below linear.** The source runs over all files because nothing says which rows a new file can touch. Two ways would say it; neither is prototyped, and a spike on LoKI's pairing and on a pairing by time would show which is better:
  - *A group per file*, declared by the author. A step makes the rows of the groups that got a file, from those files and the files shared by all groups. A source that pairs by time, such as "the can measured before", has no such group and stays linear.

    ```python
    Source(pair_by_sample, fills=...,
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
- **A loop that pushes into an accumulator**, for a sum that grows with each file (D7). The accumulator would be the only thing the workspace keeps for the loop, which would freeze it with `persist=` once the sum is complete.
- **The application around the loop**: one process per instrument, a workspace per proposal, the configuration files, and a page that shows the batches. The manager keeps which batch follows (`BatchManager.following`): the switch "Follow" of a page makes its batch the one that follows, and a timer thread of the app steps an `Automatic` on that batch, under the lock that every request of every page takes (`ess.apps.batch.web.Pages.tick`). It reduces with the batch's settings; a change in the page's settings form is in force after Apply. A step that raises is shown on the page of the batch, and the next step runs.

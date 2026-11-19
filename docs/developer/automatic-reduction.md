# Automatic reduction

Automatic reduction is Follow: a step every few seconds on one batch adds the rows of its source that are new and filled, and reduces the rows that are ready and have no result.
How it works is in [the batch guide, "Follow"](../../packages/essapps/docs/batch.md#follow); the decision is [ADR 0013](adr/0013-follow-adds-the-rows-not-considered.md).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) places it.
This page holds what neither says: how the records decide what a step reduces, what `submit` refuses, measured costs, and open questions.

## The records of a label are the results

A step reduces the ready rows of its batch that have no record under the label.
A record under the label with the row's key as its member, from any submitter or any batch, is the row's result, so the step leaves the row to a person.
Two batches on one label share their results: what one reduces, the other does not.

## What a step keeps

Nothing that decides which rows run but `considered` in `source.json`.
A step reads the files, the batch's table and settings, and the records under the label, and works out the rest.
So a step that raises, in the settings, the source, or a submission, loses nothing: the next step tries again.
A restarted application reduces no row again that has a result.
A record that was running when the workspace ended is failed at the restart, and one that had not started is cancelled unless it persists outputs ([`Workspace._restart`](../../packages/essapps/src/ess/apps/workspace.py)); a step does not run such a row again, and a person retries it.
This holds while the proposal's history is kept. A step keeps it, since its calls keep the proposal from being idle ([`ess.apps.protocol`](../../packages/essapps/src/ess/apps/protocol.py)).

The `Batch` keeps what it reads of the records, the newest of each member under the label, from the proposal's changes, and how it resolved each cell until the next `put`.
Both are caches that the records and the file source rebuild.

## Problems and refusals

A *problem* is found when the state is read, a *refusal* when a row is submitted.
A row whose cell the workspace does not accept, such as a thickness below zero or a file named before it is measured, is `incomplete`: `Batch.state()` resolves the cells it compares, and the reason is one of the row's `problems`.
So `not_reduced` does not give it, the page shows why, and a step reduces the row once the cell is accepted.

`submit` checks more than `client.resolve`, which looks at values only ([`WorkspaceProtocol.resolve`](../../packages/essapps/src/ess/apps/protocol.py)).
What only `submit` refuses, and how a batch row meets it:

| `submit` refuses | A batch row meets it when |
|---|---|
| a reference to an output of a record that the workspace does not keep and that does not persist it | a chain's first batch does not persist, and the workspace restarted or released the record |
| a reference to a record that failed or was cancelled after the state was read | the record finished between `state` and `reduce`; the next step shows the row `incomplete` |
| a reference to a record that the workspace does not know | a cell names a record of another proposal or one made up |
| a reference to an accumulator | a cell references an accumulator in place of the record of its freeze |
| a reference to an output of a record of another spec than the one bound | the first batch's records are of a spec version the workspace no longer binds |
| a reference whose data do not fit the field | a cell references the wrong output |
| a row still running | another client submitted the row between `state` and `reduce` |
| `persist` names an output the spec lacks, or the workspace has no store | the batch is made with such a `persist`; the reason names no row |
| a stage that stopped, was released, or holds other values | a staged batch; `Batch` opens a new stage and submits once more |

`Batch.reduce` is all or none, as a person needs it.
A step instead drops the rows that the `SubmitError` names (`FieldError.entry`), and submits the rest.
A reason that names no row refuses every row of the step.
Settings that the workspace does not accept make `Batch.state()` raise a `SubmitError`; the step submits nothing, and `refused` holds the reason for every row without a result that is ready as given, such as `{'1': 'unknown spec sans-iofq/v1'}` after an upgrade removed the spec (E2).

## Cost

Some instruments in full operation are expected to write 10 000 to 100 000 files per proposal, and a file every few seconds.
A step lists every file, runs the source over all of them (`rows_of`), reads the results of every row, and compares only the rows without a result:

```python
given = rows_of(batch.client, source)         # every file listed, the source run over all
candidates = not_reduced(batch.results())     # per row: its problems as given, the status of its result
state = batch.state(candidates)               # the settings resolved once; the cells of these rows resolved
```

The table below was measured with a step that made rows from the files and compared every row with its result (`batch.state()`), before the table was a `DataFrame`.
A step compares only the rows without a result, so the lines "all reduced" overstate its cost: a toy measurement at 3000 rows gives 0.05 s per step where a step that compares every row takes 0.07 s with files by identity, and 0.06 s where it takes 1.7 s with each row's sample named by run.
`put` of the whole table costs tens of milliseconds at 3000 rows (ADR 0012); it was not measured at the sizes below.

| Rows (2 files each) | 10 000 | 30 000 | 100 000 |
|---|---|---|---|
| all reduced, nothing arrives | 0.19 s | 0.72 s | 2.8 s |
| all reduced, one file arrives | 0.20 s | 0.77 s | 4.2 s |
| all reduced, each row's transmission named by run, nothing arrives | 1.3 s | 4.4 s | 15 s |
| none reduced: the first step, which submits every row | 5.7 s | 18 s | 61 s |

Best of five steps, with a toy workflow, a file source that finds a name by run in one lookup, and a history in memory, on a machine shared with other work.
The mean is up to 1.5 times the best, because of Python's garbage collector, which walks every object of the process, the records among them.
Of a step at 30 000 rows with nothing named by run, listing the files is about 40 %, finding the problems of each row and the references in its cells about 30 %, comparing the cells with the results about 20 % (which a step does not do), and the status of every result 5 %.

A step costs the same for one new file or fifty, so the interval of the timer sets the load on one core, `step / (step + interval)`.
With a step of about 3 s at 100 000 rows, that is 75 % at an interval of 1 s, 23 % at 10 s, and 9 % at 30 s.
The price is that a new file waits up to one interval.
An interval that follows the step time, such as `max(1 s, 10 × the last step)`, would keep the load near 10 % at any number of files; it is not built.

## Why the history is not cut off

A cut-off by age or by count, of the files given to the source, would bound the cost of a step, but it changes the results:

- A partner older than the cut-off, such as a can measured at the start, is gone, and its row waits for ever.
- A sample measured again after the cut-off gets a row with its later runs only, which is reduced with those, and nothing shows it.

A cut-off that a person sets on purpose, "only the runs from 60339 on", is the choice of rows when the batch is made: the rows before are considered and never added, and the source still sees every file.

## Open

- **New files since a position.** `FileSource.list` returns every file, so each step lists the whole proposal. A catalogue cannot be listed whole at each step; the file source needs a call that returns what is new since a position, as `client.changes` does for records.
- **Reading the records of chosen members.** A batch reads every record under its label once, at its first state. Each later state reads the records that the proposal's changes name since, under any label, and keeps those under its own. A remote client would read 100 000 records when a batch is made, and every record that another batch of the proposal submits or finishes. A step also asks for the status of every row's result, 100 000 at that scale.
- **A step below linear.** The source runs over all files because nothing says which rows a new file can touch. Two ways would say it; neither is prototyped:
  - *A group per file*, declared by the author. A step makes the rows of the groups that got a file, from those files and the files shared by all groups. A source that pairs by time, such as "the can measured before", has no such group and stays linear.
  - *A row from one file, by queries on an index of the files.* The framework records what each row asked for. It needs no declaration by the author, and changes the signature of every source.

  A class with state that the author writes, `add(file) -> changed rows`, is not offered. It would have to give the same rows for any order of arrival, a role corrected later, a file that arrives again, and a restart, which a function over the files gives without effort.
- **A series that grows** (E1): a step never changes a row, so the row of a sample is reduced once, with its first angle. The story is not met.
- **A step that pushes into an accumulator**, for a sum that grows with each file (D7).

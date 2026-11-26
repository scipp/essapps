# Automatic reduction

For a developer of the automatic reduction.
After reading you know how the records decide what a step reduces, what survives a failure, and what `submit` rejects that the state of a row does not show.

Automatic reduction is Follow: a step every few seconds on one batch adds the new rows of its row source, and reduces the ready rows that have no result.
How a step works is in [the batch guide, "Follow"](../../packages/essbatch/docs/batch.md#follow), and the decision is [ADR 0013](adr/0013-follow-adds-the-rows-not-considered.md).
Its stories are E1 to E4 in [user-stories.md](user-stories.md), and [deployments.md](deployments.md#automatic-reduction) shows the process that runs it.

## The records of a label are the results

A record under the batch's label, with a row's key as its member, is that row's result.
It may come from any submitter or any batch.
A step reduces only the ready rows without a result.
A row with a result, also a failed or cancelled one, is left to a person.
So two batches on one label share their results: what one reduces, the other does not.

## What survives a failure

A step stores only `seen_keys` on disk, in `source.json`.
It reads the rest at each step: the files, the batch table, the settings, and the records under the label.
So a step that raises loses nothing, and the next step tries again.
If a step raises, `Intake` still marks the keys of the batch table as seen, so a row that the step added and a person removes stays away.
A restarted application reduces no row again that has a result.
A record that was running when the workspace ended fails at the restart, so a person retries its row.
This holds while the proposal's history exists.
Each step calls the workspace, so a proposal with a running step is never idle.

## What `submit` rejects

A row whose cell the workspace does not accept, such as a thickness below zero, has status `incomplete`.
`Batch.state()` lists the reason in the row's `problems`, so a step does not submit the row, and the page shows why.

`submit` checks more than `client.resolve`, which looks at values only ([`WorkspaceProtocol.resolve`](../../packages/essapps/src/ess/apps/protocol.py)).
What only `submit` rejects, and how a batch row meets it:

| `submit` rejects a reference to | A batch row meets it when |
|---|---|
| an output neither in memory nor persisted | a chain's first batch does not persist, and the workspace restarted |
| a record that failed or was cancelled | the record finished between `state` and `reduce` |
| a record that the workspace does not know | a cell refers to a record of another proposal |
| an accumulator | a cell refers to an accumulator, not to its freeze |
| an output of a record of an unbound spec | the spec of the first batch's records is no longer bound |
| an output whose data do not fit the field | a cell refers to the wrong output |

`Batch.reduce` also rejects a row that is still running, which happens when another client submitted it between `state` and `reduce`.
`submit` rejects a `persist` that names an output the spec lacks, or a workspace without a store.
For a staged batch, it rejects a stage that stopped, was released, or holds other values.
`Batch` then opens a new stage and submits again.

`Batch.reduce` submits all rows or none, as a person needs it.
A step instead drops the rows that the `SubmitError` names (`FieldError.request_key`), and submits the rest.
An error that names no row rejects every row of the step.
If the workspace does not accept the settings, `Batch.state()` raises a `SubmitError`, and the step submits nothing.
Its `submit_errors` then holds the reason for every ready row without a result, such as `{'1': 'unknown spec sans-iofq/v1'}` after an upgrade removed the spec (E2).

## Why a step reads every file

A cut-off by age or by count, of the files given to the row source, would bound the cost of a step, but it changes the results:

- A partner older than the cut-off, such as a can measured at the start, is gone, and its row stays incomplete for ever.
- A sample measured again after the cut-off gets a row with its later runs only, which is reduced with those, and nothing shows it.

A person who wants "only the runs from 60339 on" chooses the rows when the batch is created.
The rows before are then seen and never added, and the row source still reads every file.

# Stages and accumulators

A plain request loads its runs again each time it is submitted.
Tuning `reduction.bins` over three values loads the sample three times, and adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators keep what such work computes in the workspace's memory between requests:

| | Made from | Each call gives | Keeps | Needs a binding that |
|---|---|---|---|---|
| stage | a template | values of the blanks | the part that depends only on the fixed values | makes stages, such as `PipelineBinding` |
| accumulator | a template whose blanks are tables | rows of one or more tables (a *push*) | the sum, or whatever its driver holds, over the rows so far | makes drivers, such as `DriverBinding` |

Both make records that are identical to the records of plain requests with the same values (see Symmetries).
The workspace keeps a stage or an accumulator until a client releases it or the workspace closes.
Every client that holds a stage or an accumulator may call through it, push into it, and read it.
No client call lists them or reopens one by its ID, and they are not in the changes.
So a second connection, such as a reloaded web page, reaches the stages and accumulators of the first only if it is handed them ([README, open question 9](README.md#open-questions)).
Making one over a binding that does not serve it is refused.
[ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md) and [ADR 0003](adr/0003-accumulators-add-in-place.md) give the reasons; [bindings.md](../../packages/essspec/docs/bindings.md) gives the binding's side of the contract.

**In short:**

- A call through a stage, and every state of an accumulator, has a plain request. The record of a stage call, and of a freeze, is the record of that request.
- A push returns once logged. The workspace adds it later, in the order logged.
- `client.output(acc, ...)` copies the state after the pushes logged so far, and makes no record.
- Push n+1 waits until everyone reading state n is done, so no reader ever sees a value change.
- `freeze` turns the last state into an ordinary record, without a copy. A request references that record, never the accumulator.
- A failure in a push stops the accumulator for good.

## Stage

```python
tune = client.stage(Template(IOFQ, params={'sample': file(run=60339), 'can': file(run=60338),
                                           'transmission': t.ref('transmission')},
                             blanks=('reduction',)))
for bins in (50, 100, 200):
    client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='iofq')
```

The first call has the binding make its stage from the fixed values, and compute what depends only on them.
Later calls compute only the rest.
Each call gives values for the blanks only, fills them, and makes the record of the plain request.
A blank is a whole parameter, never a field of a group.
A file name in the template is resolved when the stage is made, so the stage keeps the file that the name meant then.

## Accumulator

A *table* is a list parameter that the spec names in `tables=` ([params.md](../../packages/essspec/docs/params.md#tables-several-runs)), and its elements are *rows*.
An accumulator is a template whose blanks are tables:

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params={'reduction': Reduction(bins=100),
                                                       'transmission': t.ref('transmission')},
                                   blanks=('sample', 'can')))
iofq.push(sample=[file(run=611)])                       # state 1
iofq.push(sample=[file(run=612), file(run=613)])        # state 2
iofq.push(sample=[file(run=617)], can=[file(run=614)])  # state 3
client.output(iofq, 'iofq')                             # the output of state 3
```

*State n* is the accumulator after its first n pushes.
Its plain request is the template with each table filled by the rows of the first n pushes, in push order, and it has the outputs of that request.
The rows of one push enter one state, so states 1 and 2 above, which have no can row, are never read.
Tables stand side by side: the first sample row is not paired with the first can row.

A push of no rows is refused, and so is a push into a table that the template fixes.
A push gives rows of one or more tables, each row as a request writes it there: a group as a model or a dict, a file as `file(...)` or a `Path`, a reference to an output.
A push references files and outputs of records, never an accumulator.
A reference to a record that failed or was cancelled is refused, and a pending record is not waited for when the push is logged.
`client.accumulator` and `push` return once logged; the workspace makes the driver and adds each push later, once the records it references have completed.

**Distinct rows.** A push refuses a row that its table already holds, since pushing a run twice is usually a mistake, such as running a notebook cell again.
Rows are compared as the record stores them: a value written inline by its JSON form, a file by its identity, and a reference by record, output, and key.
So `file(run=611)` and `file(path=...)` of the same file are one row, but two references to equal data are two rows.
A plain request with a repeated row is refused too.

## Reading a state

Each call below is of state n, where n is the number of pushes logged when it is called.
It does not wait for these pushes to be added before it returns, except where it returns the value itself:

| Call | Returns | Push n+1 waits |
|---|---|---|
| `client.output(acc, 'iofq')` | a copy of the output of state n (or the part `select=` names); no record | while it waits for state n and copies |
| `client.freeze(acc)` | the record of state n | no push follows |
| `client.provenance(acc)` | the provenance of state n's plain request, at once | no |

`select=` copies only the part it names, but the outputs of a state are computed whole, the first time a read or a freeze needs them.
Every client that holds an open accumulator may read it.

A request, a template, and a push never reference an accumulator.
Each is refused with a `SubmitError` that says to freeze the accumulator first.
A request reads an accumulator's outputs through the record of its freeze, as any other record's (see Freeze).

**Push n+1 waits for the readers of state n.** A driver only moves forward, and may add the next push into the arrays its last outputs returned.
So the workspace adds push n+1 only once the *readers* of state n are done: the reads of state n, until they have copied, and a freeze of state n, while it is checked.
The calls still read as if they ran one after another:

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push(runs=[run])                                   # returns once logged
    show(client.output(volume, 'volume', select={'q': 0}))   # waits for this state, copies a slice
```

In Rust's terms, each reader of a state holds a shared borrow of the driver, and a push waits for a mutable borrow.

## Freeze, release, stop

```python
total = client.freeze(iofq)                 # the record of the last state; no copy, no more pushes
client.output(total, 'iofq')
final = client.freeze(volume, persist=True) # the store keeps the outputs, not the workspace
cut = client.submit(CUT, params={'position': Position(index=0), 'volume': final.ref('volume')})
```

At the end of a scan, freezing makes the final volume an ordinary record, which the workspace keeps.
A client can reference that record from a request, a template, or a push, where the accumulator itself may not be referenced.
A record of a state while the accumulator goes on is not offered ([ADR 0003](adr/0003-accumulators-add-in-place.md), Open).
A controller that should skip a run whose reduction failed waits for that record before it pushes, since a failed push stops the accumulator.
[`WorkspaceProtocol`](../../packages/essapps/src/ess/apps/protocol.py) states the rules of `freeze`, `release`, and a stopped accumulator.

## Validation

Templates, stage calls, and pushes are validated in the same two phases as a plain request ([params.md](../../packages/essspec/docs/params.md#when-values-are-validated)), each on what is new:

| What | When logged | When it runs |
|---|---|---|
| a template, when `client.stage` or `client.accumulator` makes it | each fixed value by its type; each fixed reference readable and fitting; the table rules on fixed tables | each fixed value, once, when the stage or driver is made |
| a stage call | the same, for the values given | each given value |
| a push | each row by the table's element type; each reference readable and fitting; the table rules on the table with the push | each row, with the data of its references, before the driver gets it |
| a read or a freeze of a state | the number of rows of each table, by its length constraints | (none) |

The *table rules* are a table's length constraints and distinct rows.
A value that fails when it runs stops the accumulator, as any failing push does.

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs:

| Symmetry | The caller may change | Unchanged | Kept by |
|---|---|---|---|
| one-row | the single-run spec ↔ the multi-run spec with one row per table | the outputs | the workflow package: its driver computes what its pipeline computes |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework for the record; for the outputs, `sciline.Stage`, given a pure pipeline |
| arrival | rows pushed one at a time, or in groups ↔ one plain request | the outputs of state n | the driver's promises |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver's promises |
| placement | where a request runs | the record and its outputs | the framework |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries that a workflow package keeps: `check_one_row`, `check_caching`, and `check_arrival_and_order`.
Only `check_one_row` compares against an independent reference, the single-run binding.

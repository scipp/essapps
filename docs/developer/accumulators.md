# Stages and accumulators

For a client author who tunes a parameter or sums runs.
After reading you can use stages and accumulators, and know what a read of an accumulator waits for.

A plain request loads its runs again each time it is submitted.
Tuning `reduction.bins` over three values loads the sample three times, and adding a run to a sum of a hundred runs reduces all hundred again.
Stages and accumulators hold what such work computes in the workspace's memory between requests:

| | Made from | Each call specifies | Holds | Needs a binding such as |
|---|---|---|---|---|
| stage | a template | values of the blanks | what depends only on the fixed values | `PipelineBinding` |
| accumulator | a template whose blanks are tables | rows of tables (a *push*) | what its driver holds over the rows so far | `DriverBinding` |

A call through a stage, and every state of an accumulator, has a *plain request*: the same values, submitted without a stage or accumulator.
Its record is the record of that plain request.
The workspace holds a stage or an accumulator until a client releases it or the workspace closes.
Every client that holds the object may call through it, push into it, and read it.
No client call lists stages and accumulators or reopens one by its ID, and they do not appear in the changes.
Creating one over a binding that does not serve it raises.
[ADR 0006](adr/0006-the-unit-is-an-accumulating-workflow.md) and [ADR 0003](adr/0003-accumulators-add-in-place.md) give the reasons; [bindings.md](../../packages/essspec/docs/bindings.md) gives the binding's side.

## Stage

```python
tune = client.stage(Template(IOFQ, params={'sample': file(run=60339), 'can': file(run=60338),
                                           'transmission': t.ref('transmission')},
                             blanks=('reduction',)))
for bins in (50, 100, 200):
    client.compute(tune, params={'reduction': Reduction(bins=bins)}, label='iofq')
```

The first call has the binding create its stage from the fixed values, and compute what depends only on them.
Later calls compute only the rest.
A blank is a whole parameter, never a field of a group.
A file name in the template is resolved when the stage is created, so the stage uses the file that the name meant then.

## Accumulator

A *table* is a list parameter that the spec names in `tables=` ([params.md](../../packages/essspec/docs/params.md#tables-several-runs)), and its elements are *rows*.
An accumulator is created from a template whose blanks are tables:

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
Its plain request is the template with each table filled by the rows of the first n pushes, in push order.
The rows of one push enter one state, so states 1 and 2 above, which have no can row, are never read.
Tables stand side by side: the first sample row is not paired with the first can row.

A push specifies rows as a request specifies them in that table: a group as a model or a dict, a file as `file(...)` or a `Path`, a reference to an output.
A push may reference a pending record, and does not wait for it.
`client.accumulator` and `push` return once logged.
The workspace creates the driver and adds each push later, once the records it references have completed.

**Distinct rows.** A push raises for a row that its table already holds, since pushing a run twice is usually a mistake, such as running a notebook cell again.
Rows are compared as the record stores them: a value written inline by its JSON form, a file by its identity, and a reference by record, output, and key.
So `file(run=611)` and `file(path=...)` of the same file are one row, but two references to equal data are two rows.

## Reading a state

A read is of the *logged state*, state n for the n pushes logged when it is called:

| Call | Returns | Push n+1 waits |
|---|---|---|
| `client.output(acc, 'iofq')` | a copy of the output of state n, or of the part `select=` picks; no record | until the copy is made |
| `client.freeze(acc)` | the record of state n | no push follows |
| `client.provenance(acc)` | the provenance of state n's plain request, at once | no |

`client.output` waits until the pushes of state n are added.
The outputs of a state are computed whole, the first time a read or a freeze needs them.

**Push n+1 waits for the readers of state n.** A driver only moves forward, and may add the next push into the arrays its last outputs returned.
So the workspace adds push n+1 only once the reads of state n have copied, and a freeze of state n has been checked.
The calls still read as if they ran one after another:

```python
volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push(runs=[run])                                   # returns once logged
    show(client.output(volume, 'volume', select={'q': 0}))   # waits for this state, copies a slice
```

In Rust's terms, each reader of a state holds a shared borrow of the driver, and a push waits for a mutable borrow.

## Freeze

```python
total = client.freeze(iofq)                 # the record of the logged state; no copy, no more pushes
client.output(total, 'iofq')
final = client.freeze(volume, persist=True) # the store holds the outputs, the workspace does not
cut = client.submit(CUT, params={'position': Position(index=0), 'volume': final.ref('volume')})
```

A request, a template, and a push never reference an accumulator, only the record of its freeze.
`submit` raises a `SubmitError` for a reference to an accumulator, and the message names the record of its freeze if there is one.
A failed push stops the accumulator for good.
So a controller that should skip a run whose reduction failed waits for that reduction's record before it pushes.
[`WorkspaceProtocol`](../../packages/essapps/src/ess/apps/protocol.py) states the rules of `freeze`, `release`, and a stopped accumulator.

## Validation

Templates, stage calls, and pushes are validated in the same two phases as a plain request ([params.md](../../packages/essspec/docs/params.md#when-values-are-validated)), each on what is new:

| What | When logged | When it runs |
|---|---|---|
| a template | each fixed value and reference; the table rules on fixed tables | each fixed value, once |
| a stage call | each value given, and its references | each value given |
| a push | each row and its references; the table rules | each row, with its data |
| a read or a freeze of a state | the length constraints of each table | nothing |

The *table rules* are a table's length constraints and distinct rows.
A row that fails when it runs stops the accumulator, as any failing push does.

## Symmetries

Each symmetry names a change that a caller may make without changing the outputs:

| Symmetry | The caller may change | Unchanged | Held by |
|---|---|---|---|
| one-row | single-run spec ↔ multi-run spec with one row per table | the outputs | the workflow package |
| caching | a call through a stage ↔ the plain request | the record and its outputs | the framework, and `sciline.Stage` for a pure pipeline |
| arrival | rows pushed one at a time or in groups ↔ one plain request | the outputs of state n | the driver |
| order | the order of pushes into different tables | the outputs once the same rows are in | the driver |
| placement | where a request runs | the record and its outputs | the framework |

Arrival, order, and placement hold up to rounding.
`ess.spec.testing` checks the symmetries that a workflow package holds: `check_one_row`, `check_caching`, and `check_arrival_and_order`.
Only `check_one_row` compares against an independent reference, the single-run binding.

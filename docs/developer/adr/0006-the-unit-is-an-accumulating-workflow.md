# ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-11-10

## Context

What the requirements ask of combining runs:

- SANS sums numerators and denominators separately and divides once ([sans](../../requirements/sans.md)).
- One I(Q) reads a list of sample runs and a list of can runs.
- The runs of one sum share their settings, such as a beam centre; a value that differs per run comes with the run's data ([users](../../requirements/users.md)).
- Spectroscopy adds each run to a grid of up to hundreds of GB, and users look at cuts while it grows ([spectroscopy](../../requirements/spectroscopy.md)).

`ess.reduce.streaming.StreamProcessor`, which esslivedata uses for the live I(Q) of LoKI, holds what such a sum needs and computes the outputs from it.
A spike ran esssans through it on LoKI@Larmor data:

- The eight sums it held, numerators and denominators for sample and can, took 0.34 MB.
- Two sample and two can runs, pushed one at a time, gave the same float32 I(Q) as esssans over the same lists.
- Peak memory stayed at about 4 GB however many runs were pushed. The plain reduction of two plus two runs took 7.5 to 9.9 GB.

## Decision

**Runs are combined by an accumulator.**
A client opens it from a template whose blanks are tables, and each push adds one or more rows to one or more tables ([accumulators.md](../accumulators.md)).
State n is the accumulator after n pushes.
It gives the outputs of the plain request over the first n pushes.
The `Accumulator` class of sciline and of `StreamProcessor` holds one key and is part of a driver, not an accumulator in this sense.

**The caller chooses how rows arrive, and the workflow package's driver chooses how a state is computed.**
A *driver* is an object with `push(table, rows)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
An accumulator exists only over a binding that makes drivers, such as `DriverBinding`, which makes a driver from the template's fixed values and pushes each client push into it ([bindings.md, A driver](../../../packages/essspec/docs/bindings.md#a-driver)).
The same binding computes a plain request with a driver of its own and one push per table, so both give the same outputs.
A driver for SANS holds a numerator and a summed monitor per table.
Its author promises the outputs of the plain request, however the rows were grouped into pushes, as `StreamProcessor` asks its users to promise that the workflow is linear in the inputs that change per chunk.
A joint fit, such as the scale factors of reflectometry angles, has a driver that keeps its rows and computes over all of them at each read.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
The framework needs no particular driver; a package writes its own or adapts a generic one, such as `StreamProcessor`.

**A freeze makes a record of the plain request of the logged state.**
`client.freeze(acc)` logs the record of the plain request of the accumulator's *logged state*, state n for the n pushes logged so far ([ADR 0003](0003-accumulators-add-in-place.md) says when n is taken).
A request references an output of that record, `OutputRef(record=..., output='iofq')`, as it references any record.
Records never name an accumulator, as records of calls through a stage never name the stage.
The record's outputs are what the driver returned, not a copy.
That record reads the driver, not the values its rows reference.
So its edges to the records its rows reference are provenance, and the client may release those records after pushing.
A read of a state, `client.output(acc, ...)`, makes no record ([ADR 0003](0003-accumulators-add-in-place.md)).

**What the framework sees is flat.**
A row is what the reduction repeats: the file of a run, or a group written inline, such as a time slice.
Structure below a row, such as detector banks, belongs to the driver, so that the per-run work runs once per row, not once per bank.
The framework cannot tell a bank index from a per-run value, so nothing checks this rule for workflow authors.
Records, checks, pushes, and the forms of apps stay the same for every technique.

**Two examples.**
*One table of groups.*
A slicing reduction sums the events of one run within each time slice, a group of the slice's start and stop:

```python
class TimeRange(BaseModel):
    start: float                 # s after the run's start
    stop: float

class SlicesParams(IofQSettings):        # the settings that IofQParams has too
    sample: RunFile                      # a file field, from ess.spec
    can: RunFile
    time_range: list[TimeRange] = Field(min_length=1)

SLICES = WorkflowSpec(name='sans-slices', version=1, title='I(Q) of time slices', description='...',
                      params=SlicesParams, outputs=IofQOutputs, tables=('time_range',))
acc = client.accumulator(Template(SLICES, params={'sample': file(run=611), 'can': file(run=614)},
                                  blanks=('time_range',)))
acc.push(time_range=[TimeRange(start=0, stop=60)])
acc.push(time_range=[TimeRange(start=60, stop=120)])
```

Start and stop of a slice are in one row, so one push gives both to the driver.
Two tables of their own could be paired wrongly.

*Two independent tables.*
Sample and can runs are two blanks, pushed as they arrive.
Rows of different tables have nothing to do with each other: a push that gives rows of both tables stands them side by side, as a request does, and never pairs them.
This needs a driver whose sample sums depend only on sample rows, and whose can sums depend only on can rows.
Then the order of pushes changes no state.
esssans builds its pixel masks from the sample run, so its can sums depend on the sample too.
So a driver over esssans that reads masks from files cannot sum the can runs on their own.
A spec that takes the masks as an input, such as `IOFQ` of `ess.instruments.loki`, removes the dependency without a change to esssans.

```python
class IofQMultiParams(IofQSettings):     # IofQSettings holds the transmission, one for all sample runs
    sample: list[RunFile] = Field(min_length=1)
    can: list[RunFile] = Field(min_length=1)

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
iofq = client.accumulator(Template(IOFQ_MULTI, params={'transmission': t.ref('transmission')},
                                   blanks=('sample', 'can')))
iofq.push(sample=[file(run=611)])
iofq.push(can=[file(run=614)])
iofq.push(sample=[file(run=613), file(run=615)])   # one push, one state
```

## Alternatives considered

- **Combining the outputs of requests**, with a per-run spec, a sum, and a finalizing spec. Each partial sum becomes a record. Two tables of runs, shared parameters next to per-run ones, and the finalizing step would each need a mechanism of their own.
- **List parameters, with the framework finding new runs by comparing lists.** The framework would have to compare lists, and track which tuned values the per-run part read. A push names the new row.
- **A chain of totals**, each a request over the previous total and one new run. A record would not mean the sum of its runs, and a growing total is copied at every step.
- **The framework keeps the rows for a binding without a driver**, and computes the plain request over all of them at each read. How rows combine would be decided in two places, and a framework strategy would serve workflows that never chose it. A workflow that needs every row at once writes a driver that keeps them.
- **Records that name a state**, by a reference to an accumulator and a push count, or as a record from `client.submit(acc)` that holds the driver's outputs. This adds a third kind of reference, which provenance and rerunning a record would have to resolve into pushes. Such a record's value changes at the next push.
- **Accumulating with `StreamProcessor`** from essreduce, as in the spike. Of `StreamProcessor`, an accumulator needs only the static part, the dynamic keys, the summed accumulators, and finalize. The context keys, rolling windows, and `clear()` of `StreamProcessor` would break the arrival symmetry. Importing it loads essreduce, with scippneutron, scippnexus, scipy, and dask. A package's driver may still adapt it: `accumulate` is `push`, and `finalize` is `outputs`. The framework itself does not import it.
- **Nested tables**, such as rows that hold a table of banks. The framework would need a push per level, and rules on which work repeats at which level.

## Consequences

- A package may also offer a single-run spec, which agrees with the multi-run spec at one row per table ([accumulators.md, Symmetries](../accumulators.md#symmetries)). It does not output partial sums, because that would split the reduction into per-run and summing specs again.
- What a summing driver sums must add over runs, such as a numerator and a denominator, not a normalized curve.
- A driver keeps each table's contributions apart, or it breaks the order symmetry; `check_arrival_and_order` tests this in the package.
- A spec bound to a driver has no stage, so a workflow that needs every row at once, such as `STITCH`, chooses between accumulating and tuning over fixed rows.
- An accumulator cannot tune a fixed value used before the sum, such as `bins`. Changing it opens a new accumulator, and every row is pushed again. Tuning over a fixed set of runs is a stage's job.
- Spreading one accumulator over nodes needs a merge of two drivers, which the binding protocol does not offer (README, [Open questions](../README.md#open-questions), Grouping).
- A driver computes the contributions of the rows of a push inside `push`, before it adds any ([bindings.md, What a driver promises](../../../packages/essspec/docs/bindings.md#what-a-driver-promises)). It may run them on several threads: with eight rows through the backend, a driver that mapped its rows over eight threads took 0.21 s instead of 1.61 s for rows that sleep 0.2 s, and 0.73 s instead of 1.06 s for a scipp histogram of 30 M events per row. The `Driver` protocol could gain an optional form in which the framework, not the driver, computes the contributions:

  ```python
  class SplitDriver[C, O](Protocol):
      def contribute(self, table: str, row: Any) -> C: ...                # touches no driver state; may run in parallel
      def add(self, table: str, contributions: Sequence[C]) -> None: ...  # serial, in the order of the rows
      def outputs(self) -> O: ...
  ```

  `push(t, rows)` equals `add(t, [contribute(t, r) for r in rows])`, so a driver with `push` works unchanged. With this form, the framework could read the files of the next push while the driver adds the current one, and could keep an accumulator after a row fails, since a failed `contribute` provably leaves the sums as they were. It could also run `contribute` in other processes or on other nodes. This form is not implemented. It is worth building once a per-run reduction takes longer than the measurement of a run, once file reads need processes to run in parallel, or once one accumulator is spread over nodes.

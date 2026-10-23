# ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-15

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
A client opens it from a template whose blanks are tables, and each push adds one or more rows to one or more tables (README, [Stages and accumulators](../README.md#stages-and-accumulators)).
State n is the accumulator after n pushes.
It gives the outputs of the plain request over the first n pushes.
The `Accumulator` class of sciline and of `StreamProcessor` holds one key and is part of a driver, not an accumulator in this sense.

**The caller chooses how rows arrive, and the workflow package's driver chooses how a state is computed.**
A *driver* is an object with `push(table, rows)` and `outputs()`, in the sense of [sciline ADR 0003](https://github.com/scipp/sciline/pull/245).
An accumulator exists only over a binding that makes drivers, such as `DriverBinding`, which makes a driver from the template's fixed values and pushes each client push into it (README, [What a binding provides](../README.md#what-a-binding-provides)).
The same binding computes a plain request with a driver of its own and one push per table, so both give the same outputs.
A driver for SANS holds a numerator and a summed monitor per table.
Its author promises the outputs of the plain request, however the rows were grouped into pushes, as `StreamProcessor` asks its users to promise that the workflow is linear in the inputs that change per chunk.
A joint fit, such as the scale factors of reflectometry angles, has a driver that keeps its rows and computes over all of them at each read.
Only a driver accumulates: the framework never decides how rows combine, not even by keeping them.
The framework needs no particular driver; a package writes its own or adapts a generic one, such as `StreamProcessor`.

**Pinning a state makes a record of its plain request.**
A submission that references `acc.ref('iofq')` also logs the record of the plain request of the pinned state.
The reference becomes `OutputRef(record=..., output='iofq')`, an output of that record.
Records never name an accumulator, as records of calls through a stage never name the stage.
Each submission makes its own record of the state, whose outputs are what the driver returned, not a copy.
That record reads the driver, not the values its rows reference.
So its edges to the records its rows reference are provenance, and the client may release those records after pushing.
No client keeps the record of a state that a submission makes, since no client asked for it.
So no later submission can reference it ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)), and only the requests of its submission read it.
This is why no wait goes round in a circle ([ADR 0003](0003-accumulators-add-in-place.md)).

**What the framework sees is flat.**
A row is what the reduction repeats per run: a run, or a group that holds a run and the values given next to it, such as that run's own transmission run.
Structure below a row, such as detector banks, belongs to the driver, so that the per-run work runs once per row, not once per bank.
The framework cannot tell a bank index from a per-run value, so nothing checks this rule for workflow authors.
Records, checks, pushes, and the forms of apps stay the same for every technique.

**Two examples.**
*One table of groups.*
Suppose each sample run had its own transmission run, unlike in esssans, given to `IOFQ` with the parameter `sample: Sample`:

```python
class Sample(BaseModel):
    file: Path                   # the sample run's file
    transmission: Path           # the file of its transmission run
```

In the multi-run spec, each row of the table `sample` is such a group.
So the two are in one row, and one push gives both to the driver.
Two tables of their own could be paired wrongly.

```python
class IofQMultiParams(IofQSettings):     # the settings that IofQParams has too
    sample: list[Sample] = Field(min_length=1)
    can: list[Can] = Field(min_length=1)       # Can: a group of the run's file: Path

IOFQ_MULTI = WorkflowSpec(name='sans-iofq-multi', version=1, title='I(Q) of summed runs', description='...',
                          params=IofQMultiParams, outputs=IofQOutputs, tables=('sample', 'can'))
iofq = client.accumulator(Template(IOFQ_MULTI, params={'can': cans}, blanks=('sample',)))  # can runs fixed
iofq.push(sample=[{'file': dataset(run=611), 'transmission': dataset(run=610)}])
iofq.push(sample=[{'file': dataset(run=613), 'transmission': dataset(run=612)}])
```

*Two independent tables.*
Sample and can runs are two blanks, pushed as they arrive.
Rows of different tables have nothing to do with each other: a push that gives rows of both tables stands them side by side, as a request does, and never pairs them.
This needs a driver whose sample sums depend only on sample rows, and whose can sums depend only on can rows.
Then the order of pushes changes no state.
esssans builds its pixel masks from the sample run, so its can sums depend on the sample too.
So a driver over esssans cannot sum the can runs on their own.
Masks per run type would fix this in esssans.

```python
iofq = client.accumulator(Template(IOFQ_MULTI, blanks=('sample', 'can')))
iofq.push(sample=[{'file': dataset(run=611), 'transmission': dataset(run=610)}])
iofq.push(can=[{'file': dataset(run=614)}])
iofq.push(sample=[{'file': dataset(run=613), 'transmission': dataset(run=612)},
                  {'file': dataset(run=615), 'transmission': dataset(run=616)}])   # one push, one state
```

## Alternatives considered

- **Combining the outputs of requests**, with a per-run spec, a sum, and a finalizing spec. Each partial sum becomes a record. Two tables of runs, shared parameters next to per-run ones, and the finalizing step would each need a mechanism of their own.
- **List parameters, with the framework finding new runs by comparing lists.** The framework would have to compare lists, and track which tuned values the per-run part read. A push names the new row.
- **A chain of totals**, each a request over the previous total and one new run. A record would not mean the sum of its runs, and a growing total is copied at every step.
- **The framework keeps the rows for a binding without a driver**, and computes the plain request over all of them at each read. How rows combine would be decided in two places, and a framework strategy would serve workflows that never chose it. A workflow that needs every row at once writes a driver that keeps them.
- **Records that name a state**, by a reference to an accumulator and a push count, or as a snapshot from `client.submit(acc)`. This adds a third kind of reference, which provenance and rerunning a record would have to resolve into pushes. A snapshot's value changes at the next push.
- **Accumulating with `StreamProcessor`** from essreduce, as in the spike. Of `StreamProcessor`, an accumulator needs only the static part, the dynamic keys, the summed accumulators, and finalize. The context keys, rolling windows, and `clear()` of `StreamProcessor` would break the arrival symmetry. Importing it loads essreduce, with scippneutron, scippnexus, scipy, and dask. A package's driver may still adapt it: `accumulate` is `push`, and `finalize` is `outputs`. The framework itself does not import it.
- **Nested tables**, such as rows that hold a table of banks. The framework would need a push per level, and rules on which work repeats at which level.

## Consequences

- A package may also offer a single-run spec, which agrees with the multi-run spec at one row per table (README, [Combining runs: tables](../README.md#combining-runs-tables)). It does not output partial sums, because that would split the reduction into per-run and summing specs again.
- What a summing driver sums must add over runs, such as a numerator and a denominator, not a normalized curve.
- A driver keeps each table's contributions apart, or it breaks the order symmetry; `check_arrival_and_order` tests this in the package.
- A spec bound to a driver has no stage, so a workflow that needs every row at once, such as `STITCH`, chooses between accumulating and tuning over fixed rows.
- An accumulator cannot tune a fixed value used before the sum, such as `bins`. Changing it opens a new accumulator, and every row is pushed again. Tuning over a fixed set of runs is a stage's job.
- Spreading one accumulator over nodes needs a merge of two drivers, which the binding protocol does not offer (README, [Open questions](../README.md#open-questions), Grouping).

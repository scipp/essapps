# ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-09

## Context

What the requirements ask of combining runs:

- SANS sums numerators and denominators separately and divides once ([sans](../../requirements/sans.md)).
- One I(Q) reads a list of sample runs and a list of can runs.
- Parameters may differ per run within one sum, and all runs share values such as a beam centre ([users](../../requirements/users.md)).
- Spectroscopy adds each run to a grid of up to hundreds of GB, and users look at cuts while it grows ([spectroscopy](../../requirements/spectroscopy.md)).

`ess.reduce.streaming.StreamProcessor`, which esslivedata uses for the live I(Q) of LoKI, holds what such a sum needs and computes the outputs from it.
A spike ran esssans through it on LoKI@Larmor data:

- The eight sums it held, numerators and denominators for sample and can, took 0.34 MB.
- Two sample and two can runs, pushed one at a time, gave the same float32 I(Q) as esssans over the same lists.
- Peak memory stayed at about 4 GB however many runs were pushed. The plain reduction of two plus two runs took 7.5 to 9.9 GB.

## Decision

**Runs are combined by an accumulator.**
A client opens it from a template whose blanks are tables, and each push adds at most one row to each table (README, [Stages and accumulators](../README.md#stages-and-accumulators)).
State n is the accumulator after n pushes.
It gives the outputs of the plain request over the first n pushes.
The `Accumulator` class of sciline and of `StreamProcessor` holds one key and is part of a binding, not an accumulator in this sense.

**The caller chooses how rows arrive, and the binding chooses how a state is computed.**
Every spec with a table takes its rows in one plain request or through an accumulator, with the same outputs.
A binding with `held_state(fixed)` adds each push to a held state of its own, such as a numerator and a summed monitor (README, [What a binding provides](../README.md#what-a-binding-provides)).
Its author promises the outputs of the plain request, as `StreamProcessor` asks its users to promise that the workflow is linear in the inputs that change per chunk.
For any other binding, the held state keeps the rows, so a joint fit, such as the scale factors of reflectometry angles, can be accumulated too.

**Pinning a state makes a record of its plain request.**
A submission that references `acc.ref('iofq')` also logs the record of the plain request of the pinned state.
The reference becomes `OutputRef(record=..., output='iofq')`, an output of that record.
Records never name an accumulator, as records of calls through a stage never name the stage.
Each submission makes its own record of the state, whose outputs are what the held state returned, not a copy.
That record reads the held state, not the values its rows reference.
So its edges to the records its rows reference are provenance, and the client may release those records after pushing.
No client keeps the record of a state that a submission makes, since no client asked for it.
So no later submission can reference it ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)), and only the requests of its submission read it.
This is why no wait goes round in a circle ([ADR 0003](0003-accumulators-add-in-place.md)).

**What the framework sees is flat.**
A row is a flat model, and it is what the reduction repeats per run: a run, or the runs that belong together.
Structure below a row, such as detector banks, belongs to the binding, so that the per-run work runs once per row, not once per bank.
The framework cannot tell a bank index from a per-run value, so nothing checks this rule for binding authors.
Records, checks, pushes, and the forms of apps stay the same for every technique.

**Two examples.**
*One table with two columns.*
Suppose each sample run had its own transmission run, unlike in esssans.
The two are fields of one row, and one push gives both to the binding.
Two aligned tables could be paired wrongly.

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params=shared, blanks=('sample_runs',)))  # can runs in shared
iofq.push({'sample_runs': {'run': dataset(run=611), 'transmission': dataset(run=610)}})
iofq.push({'sample_runs': {'run': dataset(run=613), 'transmission': dataset(run=612)}})
```

*Two independent tables.*
Sample and can runs are two blanks, pushed as they arrive.
This needs a binding whose sample sums depend only on sample rows, and whose can sums depend only on can rows.
Then the order of pushes changes no state.
esssans builds its pixel masks from the sample run, so its can sums depend on the sample too.
A binding of esssans that accumulates (scipp/essapps#40) would refuse to open this accumulator.
Masks per run type would fix this in esssans.

```python
iofq = client.accumulator(Template(IOFQ_MULTI, params=shared, blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611)}})
iofq.push({'can_runs': {'run': dataset(run=614)}})
iofq.push({'sample_runs': {'run': dataset(run=613)}, 'can_runs': {'run': dataset(run=615)}})  # one push, one state
```

## Alternatives considered

- **Combining the outputs of requests**, with a per-run spec, a sum, and a finalizing spec. Each partial sum becomes a record. Two tables of runs, shared parameters next to per-run ones, and the finalizing step would each need a mechanism of their own.
- **List parameters, with the framework finding new runs by comparing lists.** The framework would have to compare lists, and track which tuned values the per-run part read. A push names the new row.
- **A chain of totals**, each a request over the previous total and one new run. A record would not mean the sum of its runs, and a growing total is copied at every step.
- **Only specs whose binding makes a held state can be accumulated.** The caller would have to know which specs accumulate.
- **Records that name a state**, by a reference to an accumulator and a push count, or as a snapshot from `client.submit(acc)`. This adds a third kind of reference, which provenance and rerunning a record would have to resolve into pushes. A snapshot's value changes at the next push.
- **Nested tables**, such as rows that hold a table of banks. The framework would need a push per level, and rules on which work repeats at which level.

## Consequences

- A package may also offer a single-run spec, which agrees with the multi-run spec at one row per table (README, [Combining runs: tables](../README.md#combining-runs-tables)). It does not output partial sums, because that would split the reduction into per-run and summing specs again.
- What the binding sums must add over runs, such as a numerator and a denominator, not a normalized curve.
- Each sum the binding holds must depend only on the fixed values and one table's fields, or the binding refuses to open.
- An accumulator cannot tune a fixed value used before the sum, such as `bins`. Changing it opens a new accumulator, and every row is pushed again. Tuning over a fixed set of runs is a stage's job.
- Spreading one accumulator over nodes needs a merge of two held states, which the binding protocol does not offer (README, [Open questions](../README.md#open-questions), Grouping).

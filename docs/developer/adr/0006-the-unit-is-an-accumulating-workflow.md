# ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-11-10

## Context

What the requirements ask of combining runs:

- SANS sums numerators and denominators separately and divides once ([sans](../../requirements/sans.md)).
- One I(Q) reads a list of sample runs and a list of can runs.
- The runs of one sum share their settings, such as a beam centre. A value that differs per run comes with the run's data ([users](../../requirements/users.md)).
- Spectroscopy adds each run to a grid of up to hundreds of GB, and users look at cuts while it grows ([spectroscopy](../../requirements/spectroscopy.md)).

`ess.reduce.streaming.StreamProcessor`, which esslivedata uses for the live I(Q) of LoKI, holds what such a sum needs and computes the outputs from it.
A spike ran esssans through it on LoKI@Larmor data.
The eight sums it held took 0.34 MB, and gave the same I(Q) as esssans over the same runs.
Peak memory stayed at about 4 GB however many runs were pushed, where the plain reduction of two plus two runs took 7.5 to 9.9 GB.

## Decision

**Runs are combined by an accumulator, and the workflow package's driver decides how.**
A client opens an accumulator from a template whose blanks are tables, and each push adds rows to one or more tables.
State n has the outputs of the plain request over the rows of the first n pushes.
A *driver* is an object with `push(table, rows)` and `outputs()`, as in [sciline ADR 0003](https://github.com/scipp/sciline/pull/245); its author promises these outputs however the rows were grouped into pushes.
The framework never decides how rows combine, not even by holding them.
A freeze creates the record of the plain request of the logged state, so records never name an accumulator.
A row is what the reduction repeats, such as the file of a run or a time slice, and tables stand side by side without pairing their rows.
Structure below a row, such as detector banks, belongs to the driver.

## Alternatives considered

- **Combining the outputs of requests**, with a per-run spec, a sum, and a finalizing spec. Two tables of runs, shared parameters next to per-run ones, and the finalizing step would each need a mechanism of their own.
- **List parameters, with the framework finding new runs by comparing lists.** The framework would track which tuned values the per-run part read, where a push contains the new row.
- **A chain of totals**, each a request over the previous total and one new run. A record would not mean the sum of its runs, and a growing total is copied at every step.
- **The framework holds the rows for a binding without a driver.** How rows combine would be decided in two places, and a workflow that needs every row writes a driver that holds them.
- **Records that name a state.** A third kind of reference, which provenance and rerunning would have to resolve into pushes, and whose value changes at the next push.
- **Accumulating with `StreamProcessor` in the framework.** Its context keys, rolling windows, and `clear()` break the arrival symmetry, and importing it loads essreduce. A package's driver may still adapt it.
- **Nested tables**, such as rows that hold a table of banks. The framework would need a push per level, and rules on which work repeats at which level.

## Consequences

- A package may also offer a single-run spec, which agrees with the multi-run spec at one row per table ([accumulators.md, Symmetries](../accumulators.md#symmetries)).
- What a summing driver sums must add over runs, such as a numerator and a denominator, not a normalized curve.
- A driver holds each table's contributions apart, or it breaks the order symmetry. So a can sum must not depend on the sample, as esssans's masks built from the sample run do.
- A spec bound to a driver has no stage, so a workflow that needs every row at once, such as a stitch, chooses between accumulating and tuning over fixed rows.
- An accumulator cannot tune a fixed value used before the sum, such as `bins`. Tuning over a fixed set of runs is a stage's job.
- Spreading one accumulator over nodes needs a merge of two drivers, which the binding protocol does not offer.

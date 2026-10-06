# ADR 0006: The unit of combining runs is an accumulating workflow, not a running combination of request outputs

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05

## Context

Combining runs needs, according to the requirements:

- SANS sums the runs of a sample, with numerators and denominators summed separately and divided once. To grow such a sum without computing it again, both must be kept until the division ([sans](../../requirements/sans.md), [tensions](../../requirements/tensions.md)).
- One I(Q) reads runs in several roles, and sample and can each take a list of runs ([sans](../../requirements/sans.md)).
- Parameters may differ per run within one sum ([users](../../requirements/users.md)).
- The reduction of each run needs values that all runs share, such as a beam centre, a direct-beam function, or the grid of a volume.
- Spectroscopy adds each run to a fixed grid of up to hundreds of GB, and users look at cuts through it while it grows ([spectroscopy](../../requirements/spectroscopy.md), [tensions](../../requirements/tensions.md)).

From 2026-09-04 to 2026-10-05 the design made a low-level accumulator the unit.
That was a running combination of values that requests produced, with the reduction split around it into a per-run spec, a sum, and a finalizing spec.
Over that month the design went through about sixteen forms, among them:

| Date (2026) | Unit | Why it went |
|---|---|---|
| 09-07 | `StreamProcessor` as the session's adapter; lists of runs to sum as its dynamic keys (ab677eb) | the parameters people tune sit upstream of where it accumulates, and it histograms (e6d7241) |
| 09-09 | a declared additive combine: contribute, combine, and finalize, with each partial sum a record chained through disk (362ce45) | one spec stood for three callables, and the two member tables of esssans (sample, can) could not be declared (a9af6a2) |
| 09-18 | a contribute spec, a combine spec, and a group, with an accumulating mark (a9af6a2) | too complicated: two specs, a mark on the spec, an adapter class |
| 09-23 | stage records: a member stage per run and a finalize stage (f5a6bff) | the backend checked that the pieces fit without seeing the graph, and refused a SANS member (48e2c17) |
| 09-24 | sums as list parameters, with a held stage that finds the new runs by comparing lists (48e2c17) | comparing lists, and tracking which tuned parameters the per-run part read, was too complex |
| 09-28 | `AccumulatorSpec` with policies for holding, flattening, and a tree over processes (b840b1f) | deferred: "the aggregation design changed four times in ten days" |
| 09-29 | an `AccumulatorSpec` holder in a session, read by a record of the spec over every element pushed so far (89e7e01) | story D7 stored 501,500 references for 1000 angles |
| 09-30 | snapshot records naming the accumulator and a count, in an event log (4f9edd5, c52e20b) | a snapshot was a record that is not a request, and the one record whose value ends at a push; replaced by reads that pin a state ([ADR 0003](0003-accumulators-add-in-place.md)) |
| 10-01 | a spec over a table, whose only parameter is the list of rows | a volume needs fixed parameters, such as its grid, which such a spec cannot have |
| 10-05 | an accumulator opened from a template, with rows that name runs (ffbc181) | a sample sum and a can sum still met in a separate FINALIZE request |

Each form made the partial sum visible to the framework, and then had to say what its record means.
Each split the reduction into pieces whose interfaces the framework checked without seeing the workflow.
Each fixed a symptom of the one before: quadratic records led to the event log, then to snapshot records, then to adding in place, then to pinning references at submission.
The same problems came back in each form: two tables in one sum, values shared by all runs next to values that differ per run, where the finalizing step runs, and which values a record of a partial sum claims.

`ess.reduce.streaming.StreamProcessor` has been in essreduce since 2024.
esslivedata uses it for LOKI's live I(Q): the detector and monitor data are its dynamic keys; it accumulates the numerator in Q and two monitors, and computes I(Q) and the transmission fraction from them when asked.
It holds exactly what a sum of runs needs, and computes the outputs from it.

The design reached this shape before and set it aside each time:

- **2026-09-07 to 08.** It was adopted by name, and dropped for tuning and histogramming. The same review called it "the tool for growing lists", which is the combining case.
- **2026-09-24.** It came back without the name, as sums over list parameters, and was dropped for the complexity of comparing lists.
- **2026-09-28.** It was rejected in writing as "an accumulating sibling of `Stage`" (proposals/accumulators.md at b840b1f), for three reasons:
  - where values accumulate is the author's choice, not the caller's;
  - what it holds depends on the graph, which sciline keeps out of `Stage`;
  - spectroscopy needs fan-out over processes anyway.

None of these reasons holds now:

- the author declares the accumulated keys inside the binding;
- the binding is where the graph is known;
- [ADR 0005](0005-the-service-writes-every-output.md) reduces an accumulator's runs inside its own job, not spread over nodes.

A spike on LoKI@Larmor data checked the shape on the real esssans workflow:
- `Filename[SampleRun]` and `Filename[BackgroundRun]` were the dynamic keys, and each push loaded and reduced one run. The transmission and empty-beam runs, the bins, and the masks were computed once.
- The eight accumulated keys were the numerators and denominators in Q and in (Qx, Qy), for sample and can, with 0.34 MB of held state in total.
- Two sample runs and two can runs, pushed one at a time, gave the same background-subtracted I(Q) as esssans's reduction over the same lists, with no difference in the float32 outputs.
- Peak memory stayed at about 4 GB however many runs were pushed. The plain reduction of two plus two runs took 7.5 to 9.9 GB, and grows with the number of runs.

## Decision

**The unit is an accumulating workflow, called an accumulator in the API.**
A client opens it from a template whose blanks are table fields, such as `sample_runs` and `can_runs`, pushes rows into those tables, and reads it like a record ([ADR 0003](0003-accumulators-add-in-place.md)).
Each push adds at most one row to each table.
The `Accumulator` class of sciline and of `StreamProcessor`, which holds one key, is part of a binding; these documents call what it holds an accumulated key.

```python
iofq = client.accumulator(Template(SANS_IOFQ, params={'beam_centre': ..., 'direct_beam': ...},
                                   blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611)}})
iofq.push({'can_runs': {'run': dataset(run=614)}})
client.output(iofq, 'iofq')          # what the plain request over these rows gives
```

- **Its outputs are the spec's outputs**, such as I(Q) and the transmission fraction, computed from its held state when they are read.
- **Its held state is private to its binding.** No spec, call, or record names it. A binding with `held_state(fixed)` chooses what it holds, such as a numerator and a summed monitor, as for `StreamProcessor`; for any other binding, the held state is the rows pushed so far.
- **The plain request of the same spec**, with every row given at once, gives the same outputs. The author of `held_state(fixed)` promises this, as `StreamProcessor` asks its users to promise that the workflow is linear in its dynamic keys up to the accumulated keys.
- **A running combination of the pushed values themselves** is the simplest such workflow (`combine(operation)`), not a concept of its own.

**The caller chooses how rows arrive; the binding chooses how a state is computed.**
Every spec with a table parameter can be run both ways.
A caller who has every row gives them in one request.
A caller whose rows arrive over time, such as the runs of a scan or of a growing sum, opens an accumulator.
Both give the same outputs for the same rows.
How an accumulator computes a state is its binding's choice, which the caller sees only in cost:

- A binding with `held_state(fixed)` makes its own held state and adds each push to it. A read computes the outputs from the held state.
- For any other binding, the held state keeps the pushed rows, and a read computes the plain request over all of them. The part that depends only on the fixed values is computed once if the binding's stage keeps it, as a stage of `PipelineBinding` does. A read then repeats the per-run part of every row so far.

The held state that keeps the rows is Simon's decision (2026-10-05): every spec with a table takes rows through the same push, whatever its binding.
It also meets the requirement "Reducing a growing series again at every new run, such as all angles of one sample so far, must be possible, but not as the only behaviour" ([users](../../requirements/users.md)), but does not follow from it: a rule with a series meets that requirement too.
A joint fit, such as the scale factors of reflectometry angles, cannot add one run at a time, but it can be accumulated this way:

```python
series = client.accumulator(Template(STITCH, params={'reference': reference}, blanks=('runs',)))
series.push({'runs': {'run': dataset(run=608)}})
series.push({'runs': {'run': dataset(run=609)}})
client.output(series, 'stitched')    # STITCH over runs 608 and 609, fitted again at this read
```

**Opening.** The framework refuses to open an accumulator whose template has no table blank, has a blank that is not a table, references an accumulator, or has values a request would refuse ([ADR 0003](0003-accumulators-add-in-place.md)).
A binding may also refuse at open: `combine` refuses fixed values, and a `PipelineBinding` that accumulates with `StreamProcessor` would refuse an accumulated key that depends on two tables (see Consequences; scipp/essapps#40, not implemented).

**What the framework sees is flat.**
A spec's parameters are values, data fields, and tables, and the rows of a table are flat models; `ess.reduce.spec` refuses a row that holds another model or table.
A table has one level of rows, and a row is the outermost level of the reduction: a run, or the runs that belong together.
Any structure below a row, such as detector banks, angle settings read from a log, sections of a large file, or groups by a value found in the data, belongs to the binding.
This is deliberate.
Records, checks, forms, and pushes stay the same for every technique, push order needs no rules, and the binding is the one place that knows the workflow's structure.

**Two examples.**
The first has one table whose rows have two columns.
Suppose a package lets each sample run have its own transmission run; esssans uses one transmission run for all runs of a sample ([sans](../../requirements/sans.md)).
A row holds what the per-run part needs at once, so the run and its transmission run are two fields of one row, and one push hands both to the binding; for `StreamProcessor`, both are dynamic keys of one `accumulate`.
Two lists that must stay aligned, one of runs and one of their transmission runs, would be two tables whose rows could be paired wrongly; they are one table with two columns.
A per-run value, such as a time range to keep, is a column in the same way.

```python
class SampleRow(BaseModel):
    run: NexusFile
    transmission: NexusFile | None = None   # this run's own transmission run, if any

class CanRow(BaseModel):
    run: NexusFile

class SansIofQParams(BaseModel):
    sample_runs: list[SampleRow]            # one table, two columns
    can_runs: list[CanRow]
    beam_centre: Array()
    direct_beam: Array()

iofq = client.accumulator(Template(SANS_IOFQ, params={**shared, 'can_runs': [{'run': dataset(run=614)}]},
                                   blanks=('sample_runs',)))
iofq.push({'sample_runs': {'run': dataset(run=611), 'transmission': dataset(run=610)}})
iofq.push({'sample_runs': {'run': dataset(run=613), 'transmission': dataset(run=612)}})
client.output(iofq, 'iofq')
# the plain request of this state:
# {**shared, 'can_runs': [{'run': 614}],
#  'sample_runs': [{'run': 611, 'transmission': 610}, {'run': 613, 'transmission': 612}]}
```

The second has two independent tables: the sample runs and the can runs of SANS, each a blank, pushed as they arrive.
If the accumulated keys of the sample depend only on sample rows, and those of the can only on can rows, the order of pushes does not change any state, and a push may add a row to each table at once.

```python
iofq = client.accumulator(Template(SANS_IOFQ, params=shared, blanks=('sample_runs', 'can_runs')))
iofq.push({'sample_runs': {'run': dataset(run=611), 'transmission': dataset(run=610)}})
client.output(iofq, 'iofq')          # refused while there is no can run, if the spec requires one
iofq.push({'can_runs': {'run': dataset(run=614)}})
client.output(iofq, 'iofq')          # sample 611 minus can 614
iofq.push({'sample_runs': {'run': dataset(run=613), 'transmission': dataset(run=612)},
           'can_runs': {'run': dataset(run=615)}})          # one push, one state
client.output(iofq, 'iofq')
# the plain request of this state:
# {**shared, 'sample_runs': [{'run': 611, ...}, {'run': 613, ...}],
#  'can_runs': [{'run': 614}, {'run': 615}]}
```

For `StreamProcessor`, the two tables are separate dynamic keys, such as `Filename[SampleRun]` and `Filename[BackgroundRun]`, and a push into one table accumulates only the keys that depend on it.

**A `PipelineBinding` that accumulates with `StreamProcessor`** (scipp/essapps#40, not implemented).
A package would describe its workflow once, and both esslivedata and this framework would use that description:

| `StreamProcessor` | accumulator |
|---|---|
| base workflow, with its static part computed once | the template's fixed values, computed once when it opens |
| `dynamic_keys` | the fields of the table rows, such as `Filename[SampleRun]` |
| `accumulate(chunks)` | `push(rows)`: the rows of one push, one per table, added at once |
| `accumulators` | the held state, private to the binding |
| `target_keys`, `finalize()` | the spec's outputs, all computed once for each state that is read |
| `context_keys`, `set_context` | none: a value that differs per run is a field of its row, and a changed shared value opens a new accumulator |
| `RollingAccumulator`, `clear()` | none: a state covers every row pushed so far |

Each difference follows from records.
A read of a state must give what the plain request over its rows gives, which a context change or a rolling window would break.
A row names a run, not a chunk of streamed data, because records name datasets.
Outputs are computed when read, not at every push, so that a driver that looks rarely pays rarely.

## Alternatives considered

- **The low-level accumulator as the unit** (the forms in the table above). The reduction splits into a per-run spec, a sum, and a finalizing spec. The partial sum is a value the framework sees and records. Two tables, shared and per-run values, and the finalizing step each need their own mechanism.
- **Only list parameters, with the framework finding the new runs by comparing lists.** Comparing lists, and tracking which tuned parameters the per-run part read, was the complexity that ended this form on 09-28. An explicit push names the new row.
- **A chain of totals**, each a request over the previous total and one new run. A record does not mean the sum of its runs, and a total that grows is copied at every step.
- **Only specs whose binding makes its own held state can be accumulated.** The caller would then have to know which specs accumulate, and the same spec could be given rows over time or not depending on how its package implements it. Opening an accumulator would fail for the rest.
- **Nested tables**, such as rows that hold a table of banks, or a table per level. The framework would need a push for each level, rules on the order of pushes, and rules on which work is repeated at which level. All of that is structure of the workflow, which the binding knows and the framework does not.
- **Fan-out over nodes as part of the unit.** This needs merging two held states, which neither `StreamProcessor` nor this binding protocol offers. No requirement needs it now: runs arrive over hours, and a finished scan can be reduced again in one job.

## Consequences

- A package offers one spec for a sum, such as I(Q) with tables of sample and can runs. Combining runs needs no split into a per-run spec, a sum, and a finalizing spec. A sum can be written in two ways: as a plain request over the tables, or as an accumulator over them.
- A partial sum is never a record. A read pins a state, and a record of a state is a request that copies it ([ADR 0003](0003-accumulators-add-in-place.md)).
- An accumulator whose binding has no `held_state(fixed)` keeps the values of every pushed row. In the user's process, a row that references a record output keeps that value alive while the accumulator lives, even after the client releases the record. A driver that reads such an accumulator after every push repeats the per-run part of every row so far at each read. A held state of the binding's own avoids both.
- The binding chooses where to accumulate. Its accumulated keys must add over runs to what the plain request over all runs computes from, such as a numerator and a denominator, not a normalized curve. For esssans these are the numerator and denominator in Q, summed over wavelength bands. One step earlier, the numerator is event data that grows with each run.
- esssans builds its pixel masks from the sample run's detector, so the can's accumulated keys also depend on the sample file, and a can run cannot be pushed alone. Masks per run type, or detector IDs from a fixed run, would fix this in esssans.
- sciline's mapped nodes are being replaced by stages and drivers over them. Until then, a parameter table at the line between the part computed once and the per-run part, such as masks given per file, stops `StreamProcessor` from building.
- A row holds what the per-run part needs at once. A run and its own transmission run, if it has one, go in one row. Rows of different tables are independent, even when one push adds them together, and pairing runs into rows is the application's job.
- Each accumulated key must depend only on the fixed values and on the fields of one table. Then pushes may come in any order and give the same state, up to rounding, and the framework has no rules on order. A binding that cannot keep this promise refuses to open; a `PipelineBinding` with `StreamProcessor` can tell, since `StreamProcessor` computes these dependencies at construction (scipp/essapps#40). The esssans masks above are such a case.
- Levels below a row are loops inside the binding, over sciline stages and nested inside the per-run work, so that the per-run work runs once per row and not once per bank. A row of (bank, run) would repeat the per-run work for every bank. The framework cannot tell a bank index from a per-run value, so keeping such levels out of rows is a rule for workflow authors.
- A plain request over a table can run through the same `StreamProcessor`, pushing every row and then computing the outputs once. A run that appears in several rows can be memoized by the binding by its dataset identity.
- Results per bank or per angle are outputs, for example with a bank dimension, and the held state may be keyed by a bank or by a value read from each run.
- If two levels ever both arrive over time, such as sections of runs that are still being written, rows become (run, section). The binding must then keep the per-run work between the rows of one run, as `StreamProcessor` does with context keys. The result still does not depend on the order of pushes, but the cost does. Nothing needs this yet: offline rows are complete files, and watching a file that is still being written is esslivedata's.
- A state is read only if its plain request would be accepted ([ADR 0003](0003-accumulators-add-in-place.md)). If the spec requires rows in every table, a state whose rows fill only some tables is refused with that reason until each has a row. If the spec lets a table be empty, reading such a state computes every output from the rows there are, as the plain request would; an output that needs a table with no rows fails the read unless the spec declares it optional and the binding leaves it out.
- Reducing the runs of one accumulator in parallel would split a push into a part that can run in parallel and an add under the accumulator's lock. Order would stay free. Nothing needs it yet: a LoKI run takes 1.3 to 1.6 seconds.
- A beam centre found from the sample runs would be both computed once and per run. It is a fixed value, given by reference to the record that found it.
- Spreading one accumulator over several nodes needs a merge of two held states (README.md, open question "Grouping"). On 2026-09-28 Simon noted that spectroscopy needs fan-out across processes. If a requirement confirms that, for example to reduce a finished scan again quickly, the binding protocol gains a merge.
- The part of an accumulator computed once from its fixed values is what a stage caches. Whether stages stay as a concept of their own is scipp/essapps#35.

## What we learned

- The combining design was tried out on toy specs whose only structure was the split being designed. They could not show what a real workflow keeps fixed, reads per run, or computes after the sum. A design for combining is now checked on a real workflow, such as LoKI's I(Q), before anything is built on it.
- The one production accumulation of SANS data, `StreamProcessor` in esslivedata's LOKI factory, was read for other questions and not as a model for combining.
- Stories whose numbers and placement had no source drove mechanisms. A thousand angles in story D7, each on its own node, led to the tree policy, to snapshot records, and to the fixes for quadratic cost. The requirements give 100 to 300 angles, possibly in one file, and D7 uses 300.
- The rejected alternatives recorded the right option twice on 09-28. Their reasons later went away, but nothing went back to check them. When the reason for rejecting an alternative goes away, the alternative is looked at again.

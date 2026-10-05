# ADR 0003: Accumulators add rows in place, and a reference to one lasts until the next push

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-05

## Context

An accumulator combines rows pushed into it one at a time, so that adding a run to a sum does not compute the earlier runs again.

Combining many runs is common: SANS sums the runs of a sample ([sans](../../requirements/sans.md)), and spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB ([spectroscopy](../../requirements/spectroscopy.md)).
A machine that holds such a volume once may not hold it twice.

While the volume grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
A cut must not race the addition of the next run.
No earlier state of the volume is ever needed, only the cuts made from it.

The reduction of each run needs values that all runs share, such as a beam centre, a direct-beam function, or the volume's grid.
On the service, every output is written to a file ([ADR 0005](0005-the-service-writes-every-output.md)), so a per-run result that is an output of its own record is written too.
A run's events binned onto a 4D grid are as large as the volume.

For N pushes into a value of size V:

| | Peak memory | Allocated over the run | Held after story D7's loop (a cut after each push), none released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, copy for each request that reads it | 2V | one V per cut | N·V |
| add in place, requests read the value itself | V | V | V |

D7 holds N·V in the first two rows because a client keeps the values it makes ([ADR 0002](0002-the-client-is-the-lifetime.md)).

## Decision

- **An accumulator opens from a template whose one blank is a table field**, and each push adds one row. The template's other values are fixed for every row. What depends only on them is computed once, when the accumulator opens, as a stage does.
- **A row names runs and per-run values**, and the accumulator reduces each run itself. A row may also reference outputs of records where those are small.
- **`accumulator.ref(output)` binds at submission** to the accumulator's value after the pushes so far. The record holds `{'accumulator': id, 'upto': n, 'output': name}`. All references to one accumulator in one submission bind to the same state. A request whose reference names an earlier state is refused.
- **The accumulator adds each row in place, and requests that reference it read the value itself, not a copy.** A push waits until the requests holding the current state, accepted before it, have run, and then adds. No request waits for a push, so the wait ends.
- Releasing the accumulator, or ending its client, waits for nothing. Its value is dropped once the requests holding it have run.
- **A record of the sum itself is a request of a spec that returns a copy of what it reads.** There is no special call.
- A push whose row references an accumulator is refused. Accumulators meet in a request, such as a FINALIZE that reads a sample sum and a can sum.

```python
sample = client.accumulator(Template(SANS_SUM, params={'beam_centre': centre.ref('centre'),
                                                       'transmission': dataset(run=610)},
                                     blanks=('runs',)))
sample.push({'run': dataset(run=611)})           # reduced and added in place
sample.push({'run': dataset(run=613)})
iofq = client.submit(FINALIZE, {'numerator': sample.ref('numerator'),        # bound now: runs 611, 613
                                'denominator': sample.ref('denominator')})
sample.push({'run': dataset(run=615)})           # waits until FINALIZE has run
```

A binding that accumulates provides `accumulator(fixed)`, which returns an element accumulator for the fixed values.
For a spec whose only parameter is the table, `combine(operation)` is such a binding: `operation(total, element)` may modify `total` in place and returns the combined value, and the first element is copied, so that adding in place never changes the output it came from.

## Alternatives considered

- **A new value per push.** A push needs two values at once.
- **Add in place, and copy for each request that reads it.** A run without readers needs one value. But each cut needs a second value for a state no one keeps, and D7 holds one per cut until it is released.
- **Copy-on-write per chunk**, as versioned chunked array stores do, to keep many states of a huge volume cheaply. No earlier state is needed; the cuts that users keep are their own records.
- **A chain of requests instead of an accumulator**, each over the previous total and one new element. It writes one output per element, and copies quadratically when the total grows with each element, such as concatenated event lists.
- **Snapshots as records.** `client.submit(accumulator)` makes a record whose output is the value so far, valid until the next push, and requests reference that record. Such a record is not a request, and it is the one record whose value ends at a push while its client keeps it. A bound reference says the same without either.
- **A table as the only parameter.** The per-run reduction needs the shared values, so a package splits it into a per-run spec, a sum, and a finalizing spec. The per-run outputs then become elements, which the service writes, and an element binned onto a 4D grid is as large as the volume.

## Consequences

- An accumulator holds one value, plus the row being added.
- No earlier state of an accumulator is kept. A cut is a request that references it; the cut's record and output stay after the next push.
- A long request that references an accumulator holds back the next push, so the driver decides how often it looks.
- Only a request binds a reference to an accumulator. A stage or an accumulator opened from a template that references one is refused, since it would hold that state, and so hold back every push, for as long as it lives.
- A workflow must not return an output that shares memory with a value it reads, such as a slice that is a view of it. This is documented, not enforced.
- The binding decides whether a push needs a second volume-sized array. Binning a run's events into the accumulator's value does not. Binning them with `sc.hist` and adding the result does.
- `ess.reduce.spec` has a third reference form, to the state of an accumulator, next to outputs of records and datasets.
- History keeps each accumulator's template, so that provenance expands `(accumulator, upto)` into the plain request over the first `upto` rows ([ADR 0004](0004-history-is-append-only-lists.md)).
- Reading the current value into a notebook is a request of a copying spec, and so makes a record like any other.
- Totals that grow with each element, such as event lists for SQW files, are not served by adding in place.

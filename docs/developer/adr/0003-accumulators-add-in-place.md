# ADR 0003: An accumulator keeps one held state, and a read pins the state at that moment

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-05

## Context

[ADR 0006](0006-the-unit-is-an-accumulating-workflow.md) makes an accumulator a workflow opened from a template, whose tables grow at each push by one row in each table the push names.
This ADR decides how such an accumulator keeps its held state, which pushes it accepts, and what a read of it means while rows are still being pushed.
A push adds rows one at a time, while a spec's rules apply to whole requests: a table may need a minimum length, and a params validator may read several fields.

Combining many runs is common: SANS sums the runs of a sample ([sans](../../requirements/sans.md)), and spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB ([spectroscopy](../../requirements/spectroscopy.md)).
A machine that holds such a volume once may not hold it twice.

While the volume grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
A cut must not race the addition of the next run.
No earlier state of the volume is ever needed, only the cuts made from it.

For N pushes into a value of size V:

| | Peak memory | Allocated over the run | Held after story D7's loop (a cut after each push), none released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, copy for each request that reads it | 2V | one V per cut | N·V |
| add in place, requests read the value itself | V | V | V |

D7 holds N·V in the first two rows because a client keeps the values it makes ([ADR 0002](0002-the-client-is-the-lifetime.md)).

## Decision

README.md (Accumulator, Reads, Pushes wait for readers, What a binding provides) states the rules in full; this section gives each decision and its reason.

**Held state.** An accumulator keeps one held state and no earlier one.
Its binding may add each push to it in place ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and its outputs may be what it holds, not a copy.

**Reads.** Every call that reads an accumulator pins its state at that moment, the state after the pushes so far.
It gives what the same call gives on the record of the plain request over those rows:

```python
acc = client.accumulator(Template(SANS_IOFQ, params=shared, blanks=('sample_runs', 'can_runs')))
acc.push({'sample_runs': {'run': dataset(run=611)}})
acc.push({'can_runs': {'run': dataset(run=614)}})

client.output(acc, 'iofq')                 # as client.output(record, 'iofq')
client.output(acc)                         # every output, by name
client.provenance(acc)                     # the plain request over the rows so far
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})   # pinned when submitted
```

- A reference is pinned to the state when its request is submitted. The record holds `{'accumulator': id, 'upto': n, 'output': name}`. All references to one accumulator in one submission are pinned to the same state. A request whose reference names an earlier state is refused, since that state is gone.
- A request reads the output itself, so a read costs no second volume. `client.output` returns a copy, so that, like a record's output, the value does not change afterwards.
- A state is read exactly when its plain request would be accepted, a state with nothing pushed included. A read of any other state is refused with the reason that request would be refused.

**Outputs.** An output of a state is computed the first time a reader asks for it, outside the backend's lock, and kept while the state has readers.
No output is computed for a state that no one reads.

**Pushes.** A push waits until the readers of the current state that came before it have run, and then adds.
Readers are the requests that reference the state and were accepted before the push, including those cancelled while they run, and `client.output` calls in progress.
No reader waits for a push, so the wait ends.
A submission or read made while a push waits or adds blocks until the push is done, and then pins the state after it.

**Ending.** Releasing the accumulator, or ending its client, waits for nothing: a push that waits is refused at once, and the held state is dropped once its readers have run.

**Opening.** The template is checked as a stage checks its template, with the table blanks left out.
The fixed values are validated by the params model with the tables empty, so that its field validators apply.
If a validator of the model fails for lack of rows, each value is validated against its own field only, since no whole request exists before rows.
A binding may refuse to open ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

**Checks at a push.** Each row is checked by its table's row model alone, since one row is not a whole request.
Checks on a whole table, such as its length, and the params model's own validators apply to the plain request over all rows pushed so far, which the accumulator validates when it opens and at each push.
If that request would be refused, the rows are still added, and the state is not readable until a later push makes the request acceptable.
If it is accepted but gives other values than the held state was given, fixed values or rows, as a validator may, the push is refused and nothing is added: the held state could not give that request's outputs.

**What may be referenced.** Only a read or `client.provenance` pins a state.
A row pushed into an accumulator may not reference an accumulator: it would read the other accumulator's state while it adds, without holding back that accumulator's next push.
A stage or an accumulator opened from a template that references one is refused too, since it would hold that state, and so hold back every push, for as long as it lives.
To combine two accumulators, a request references both.

**A record of a state** is a request of a spec that returns a copy of what it reads. There is no special call.

## Alternatives considered

- **A new value per push.** A push needs two values at once.
- **Add in place, and copy for each request that reads it.** Each cut needs a second value for a state no one keeps, and D7 holds one per cut until it is released.
- **Copy-on-write per chunk**, as versioned chunked array stores do, to keep many states of a huge volume cheaply. No earlier state is needed; the cuts that users keep are their own records.
- **Snapshots as records.** `client.submit(accumulator)` makes a record whose output is the value so far, valid until the next push, and requests reference that record. Such a record is not a request, and it is the one record whose value ends at a push while its client keeps it. A read that pins the state says the same without either.
- **Computing every output at every push**, as `StreamProcessor` callers do on a fixed cadence. It costs a finalize per push whether or not anyone reads it. Computing at the read lets the driver decide how often to look.
- **Pinning a submission made during a push to the state after it, without blocking the call.** The record would then wait for a push that may fail, so it could name a state that never exists. Reads that must follow a slider are views, which make no record (README.md, open question "Views").
- **Every spec that is accumulated accepts an empty table.** A spec could then not require at least one run of a plain request, and a reference to an empty state means nothing.
- **Refusing a push whose plain request would be refused.** A table that needs two rows, or two tables that each need one, could then not be filled one push at a time.

## Consequences

- An accumulator keeps one held state, plus the rows of the push being added, plus the outputs of a state while its readers run.
- If `outputs` allocates, such as a normalized volume, each read state costs those outputs once. A volume whose outputs are what it holds, such as intensity and normalisation grids, costs nothing more, and a cut divides after slicing.
- A long reader holds back the next push, so the driver decides how often it looks.
- A workflow must not return an output that shares memory with a value it reads, such as a slice that is a view of it. This is documented, not enforced.
- The binding decides whether a push needs a second volume-sized array. Binning a run's events into the held grid does not. Binning them with `sc.hist` and adding the result does.
- `ess.reduce.spec` has a third reference form, to a state of an accumulator, next to outputs of records and datasets.
- History keeps each accumulator's template, so that provenance expands `(accumulator, upto)` into the plain request over the rows of the first `upto` pushes ([ADR 0004](0004-history-is-append-only-lists.md)).
- Each push validates the plain request over every row pushed so far, so its cost grows with the number of rows. In the in-process backend, this check adds about 0.4 s in total to story D7's 300 pushes, and about 4 s to a thousand.
- A table with a maximum length becomes unreadable for good once a push exceeds it, since no row can be removed.
- Totals that grow with each run, such as event lists for SQW files, are not served by adding in place.

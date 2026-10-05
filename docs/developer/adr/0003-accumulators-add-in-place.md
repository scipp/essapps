# ADR 0003: Accumulators add rows in place, and a read binds to the state at that moment

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-05

## Context

[ADR 0006](0006-the-unit-is-an-accumulating-workflow.md) makes an accumulator a workflow opened from a template, whose table parameters grow at each push by one row in each table the push names.
This ADR decides how such an accumulator holds its state, and what a read of it means while rows are still being pushed.

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

**State.** The accumulator holds one state and adds each row to it in place.
No earlier state is kept.

**Reads.** Every call that reads an accumulator binds to its state at that moment, the state after the pushes so far.
It gives what the same call gives on the record of the plain request over those rows:

```python
acc = client.accumulator(Template(SANS_IOFQ, params=shared, blanks=('sample_runs', 'can_runs')))
acc.push({'sample_runs': {'run': dataset(run=611)}})
acc.push({'can_runs': {'run': dataset(run=614)}})

client.output(acc, 'iofq')                 # as client.output(record, 'iofq')
client.output(acc)                         # every output, by name
client.provenance(acc)                     # the plain request over the rows so far
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})   # bound when submitted
```

- A reference in a request binds when the request is submitted. The record holds `{'accumulator': id, 'upto': n, 'output': name}`. All references to one accumulator in one submission bind to the same state. A request whose reference names an earlier state is refused.
- `client.output` of an accumulator returns a copy, so that, like a record's output, the value does not change afterwards. Without a name it leaves out an optional output that the binding did not return, as for a record.
- A state is read only if its plain request would be accepted. Binding to any other state, such as one with nothing pushed, or with no row yet in a table that needs one, is refused with the reason that request would be refused.

**Outputs.** The binding computes an output when a state is first read for it: only the outputs asked for, once per state while it has readers, and outside the backend's lock.
It keeps them while the state's readers run.

**Pushes.** A push waits until the readers of the current state that came before it have run, and then adds.
Readers are the requests that reference the state and were accepted before the push, including those cancelled while they run, and `client.output` calls in progress.
No reader waits for a push, so the wait ends.
A submission or read made while a push waits or adds blocks until the push is done, and then binds to the state after it.

**Ending.** Releasing the accumulator, or ending its client, waits for nothing. Its state is dropped once its readers have run.

**Opening.** The template is checked as a stage checks its template: what a request would refuse is refused, with the table blanks left out.
Each fixed value is typed by its field alone, since no whole request exists before rows.
Defaults are filled in when a state is expanded into its plain request.

**Checks at a push.** A push names one row for each of one or more tables, and its rows enter one state.
A key that is not a table blank of the accumulator is refused.
Each row is checked by its table's row model alone: names are resolved, the row is validated, and its values must be storable and its references allowed.
Rules on a whole table, such as its length, and the params model's own validators apply to the plain request over all rows pushed so far.
Each push validates that request once, and the accumulator keeps the reason it would be refused, if any.
If it would be refused, the rows are still added, and the state is not readable until a later push makes the request acceptable.
If it is accepted but gives a fixed value other than the one the binding was opened with, as a validator may, the push is refused and nothing is added.

**What may be referenced.** Only a request or a read binds a reference to an accumulator.
A row that references an accumulator is refused, and so is a stage or an accumulator opened from a template that references one, since it would hold that state, and so hold back every push, for as long as it lives.
Accumulators meet in a request.

**A record of a state** is a request of a spec that returns a copy of what it reads. There is no special call.

**Binding protocol.** A binding that accumulates provides `accumulator(fixed)`, which returns a held state with `push(rows)` and `outputs(names)`.
`push` takes the rows of one push, one per table, so that a binding can add them at once.
It may modify what it holds in place, but not the rows.
`outputs(names)` computes the named outputs from what it holds; what it returns may be what it holds, not a copy.
It may leave out an output that the spec declares optional.
It must not modify what an earlier call for the same state returned, since running readers still use it while later names are computed.
The backend calls `push` and `outputs` from one thread at a time.

## Alternatives considered

- **A new value per push.** A push needs two values at once.
- **Add in place, and copy for each request that reads it.** Each cut needs a second value for a state no one keeps, and D7 holds one per cut until it is released.
- **Copy-on-write per chunk**, as versioned chunked array stores do, to keep many states of a huge volume cheaply. No earlier state is needed; the cuts that users keep are their own records.
- **Snapshots as records.** `client.submit(accumulator)` makes a record whose output is the value so far, valid until the next push, and requests reference that record. Such a record is not a request, and it is the one record whose value ends at a push while its client keeps it. A read bound to the state says the same without either.
- **Computing every output at every push**, as `StreamProcessor` callers do on a fixed cadence. It costs a finalize per push whether or not anyone reads it. Binding at the read lets the driver decide how often to look.
- **Binding a submission made during a push to the state after it, without blocking the call.** The record would then wait for a push that may fail, so it could name a state that never exists. Reads that must follow a slider are views, which make no record (README.md, open question "Views").
- **Every accumulating spec accepts an empty table.** A spec could then not require at least one run of a plain request, and a reference to an empty state means nothing.
- **Refusing a push whose plain request would be refused.** A table that needs two rows, or two tables that each need one, could then not be filled one push at a time.

## Consequences

- An accumulator holds one state, plus the rows of the push being added, plus the outputs of a state while its readers run.
- If `outputs` allocates, such as a normalized volume, each read state costs those outputs once. A volume whose outputs are what it holds, such as intensity and normalisation grids, costs nothing more, and a cut divides after slicing.
- A long reader holds back the next push, so the driver decides how often it looks.
- A workflow must not return an output that shares memory with a value it reads, such as a slice that is a view of it. This is documented, not enforced.
- The binding decides whether a push needs a second volume-sized array. Binning a run's events into the held grid does not. Binning them with `sc.hist` and adding the result does.
- `ess.reduce.spec` has a third reference form, to a state of an accumulator, next to outputs of records and datasets.
- History keeps each accumulator's template, so that provenance expands `(accumulator, upto)` into the plain request over the rows of the first `upto` pushes ([ADR 0004](0004-history-is-append-only-lists.md)).
- Each push validates the plain request over every row pushed so far, so its cost grows with the number of rows. In the in-process backend, story D7's thousand pushes take about 2 s longer in total than without this check.
- A table with a maximum length becomes unreadable for good once a push exceeds it, since no row can be removed.
- Totals that grow with each element, such as event lists for SQW files, are not served by adding in place.

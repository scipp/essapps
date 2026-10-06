# ADR 0003: An accumulator keeps one held state, and a read pins the state at that moment

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-06

## Context

[ADR 0006](0006-the-unit-is-an-accumulating-workflow.md) makes an accumulator a workflow opened from a template, whose tables grow at each push by one row in each table the push names.
This ADR decides how such an accumulator keeps its held state, which pushes it accepts, and what a read of it means while rows are still being pushed.
A push adds rows one at a time, while a spec's rules apply to whole requests: a table may need a minimum length, and a params validator may read several fields.

Combining many runs is common: SANS sums the runs of a sample ([sans](../../requirements/sans.md)), and spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB ([spectroscopy](../../requirements/spectroscopy.md)).
A machine that holds such a volume once may not hold it twice.

While the volume grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
A cut must not race the addition of the next run.
No earlier state of the volume is ever needed, only the cuts made from it.

Reducing a LoKI run and adding it takes about 1.5 s ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and longer for a run binned into a large volume.
Applications call the client from their UI thread, and on the service each call is an HTTP request.
A call that lasts as long as an addition freezes the one and holds the other open for minutes.

For N pushes into a value of size V:

| | Peak memory | Allocated over the run | Held after story D7's loop (a cut after each push), none released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, copy for each request that reads it | 2V | one V per cut | N·V |
| add in place, requests read the value itself | V | V | V |

D7 holds N·V in the first two rows because a client keeps the values it makes ([ADR 0002](0002-the-client-is-the-lifetime.md)).

## Decision

README.md (Accumulator, Reads, Adding waits for readers, What a binding provides) states the rules in full; this section gives each decision and its reason.

**Calls return once accepted.** `client.accumulator` and `push` return once the backend has checked and logged them, as `client.submit` does.
They check what can be checked without waiting or reading data: models, dataset names, and references against what the backend knows at the call.
A reference to a record that has failed is refused; a pending one is not waited for.
Reading data, opening the held state, and adding the rows of each push run on the backend's workers, one step at a time, in the order logged.
Only the calls that read results wait: `client.wait`, `client.compute`, `client.output`, and `client.as_completed`.

**Held state.** An accumulator keeps one held state and no earlier one.
Its binding may add each push to it in place ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and its outputs may be what it holds, not a copy.

**Reads.** A *read* is a call that pins the accumulator's state: submitting a request that references the accumulator, `client.output`, or `client.provenance`.
It pins the state at that moment, the state after the pushes logged so far, and gives what the same call gives on the record of the plain request over those rows:

```python
acc = client.accumulator(Template(IOFQ_MULTI, params=shared, blanks=('sample_runs', 'can_runs')))
acc.push({'sample_runs': {'run': dataset(run=611)}})
acc.push({'can_runs': {'run': dataset(run=614)}})

client.output(acc, 'iofq')                 # as client.output(record, 'iofq'): waits until both pushes are added
client.output(acc)                         # every output, by name
client.provenance(acc)                     # the plain request over the rows so far, at once
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})   # pinned when submitted, runs once both pushes are added
```

Pinning never waits for a push to be added.
A request that reads a state the accumulator has yet to reach waits for it, as it waits for a record it references.
`client.output` waits for it, as it waits for a pending record.

- A reference is pinned to the state when its request is submitted. The record holds `{'accumulator': id, 'upto': n, 'output': name}`. All references to one accumulator in one submission are pinned to the same state. A request whose reference names an earlier state is refused, since that state is gone.
- `n` counts pushes, not rows. A push may add a row to each of several tables, and the log of the first `n` pushes gives the rows of each table:

  ```python
  acc.push({'sample_runs': {'run': dataset(run=611)}})
  acc.push({'can_runs': {'run': dataset(run=614)}})
  acc.push({'sample_runs': {'run': dataset(run=612)}, 'can_runs': {'run': dataset(run=615)}})
  # 'upto': 2 is the plain request with sample_runs [611] and can_runs [614]
  ```
- A request reads the output itself, so a read costs no second copy of the held state, such as the 4D volume of spectroscopy. `client.output` returns a copy, so that, like a record's output, the value does not change afterwards.
- A read is refused if the plain request of the state would be refused, with that request's reason, such as no can run when the spec requires one. A state with nothing pushed is no exception.

**Checks.** A push checks each row by its table's row model alone, since one row is not a whole request.
Checks on a whole table, such as its length, and the params model's own validators apply when a state is read: the first read of a state validates its plain request.
So a push costs the same however many rows came before it, and a driver that reads rarely pays rarely.

**Opening.** The template is checked as a stage checks its template, with the table blanks left out.
Each fixed value is typed by its own field and that field's validators; the params model's own validators read whole requests, so they run at each read.
A binding may refuse to open ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).
It needs the values read to decide, so it refuses after the call has returned, and the accumulator stops (Failures).

Validators that read more than one value, such as the params model's own or those of a table field, must not change a value, for example derive a fixed value from the rows.
The held state was given the values typed one by one, so it could not give the outputs of a plain request whose values differ.
This is the spec author's promise; it is documented, not checked.

**Outputs.** The outputs of a state are computed all at once, outside the backend's lock, the first time the state is read, and kept until the next push.
No output is computed for a state that no one reads.
Two reads of one state, such as `client.output(acc, 'a')` and then `client.output(acc, 'b')`, compute once.

**Adding.** A push is added once the records its rows reference have completed and the readers of the state before it have run.
Readers are the requests that pinned that state, including those cancelled while they run, and `client.output` calls in progress; `client.provenance` reads no output and holds back no push.
A reader waits only for the pushes up to its state and for records submitted before it, so no wait goes round in a circle.

**Failures.** If adding a push fails, or a record that its rows reference fails or is cancelled, the accumulator stops.
Later pushes and reads are refused with the reason.
Requests pinned to a state it did not reach fail with it, as a request fails whose input failed.
Their records still name a plain request, since the log holds the rows of every push accepted; that request would most often fail for the same reason, such as a corrupt file.
The same holds if the held state fails to open.
A driver that wants to skip a run whose reduction failed waits for that record before pushing it.

**Ending.** Releasing stops no work, so a driver may release an accumulator right after its last read:

```python
acc.push(row_1)
acc.push(row_2)
acc.push(row_3)                                          # each returns once logged
cut = client.submit(CUT, {'data': acc.ref('counts')})    # pins the state after three pushes
client.release(acc)                                      # the pushes are added, and the cut runs
client.output(cut, 'cut')
```

Releasing waits for nothing.
The pushes logged are still added, and the held state is dropped once its readers have run.
A released accumulator takes no more pushes or reads.
To stop the readers too, the driver cancels them with `client.cancel`.
Ending the client releases the accumulator in the same way.

**What may read an accumulator.** A request reads at most one accumulator, and a row or a template reads none.

- A request reads a state in place, only while it runs, and the next push is added after it. A row or a template's value lives as long as the accumulator or stage that holds it: the held state that keeps the rows stores each row's value, and the framework cannot tell whether a binding's own held state does. Read in place, that value would change at the other accumulator's next push; holding back that push instead would last as long as the holder.
- A request reads a held state in place, so it runs in the process that holds it. A request that read two accumulators would need both held states in one process, or one moved to the other. In the user's process both are in one process; where the service holds held states is open ([ADR 0005](0005-the-service-writes-every-output.md), Open), and this rule keeps that choice free. Lifting it later breaks no code.

The cases that use an accumulator's outputs together with other values:

| Case | How | Written on the service |
|---|---|---|
| sample and background runs, both still arriving, cut together | one accumulator with two tables ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)) | nothing |
| the output of a finished accumulator as a fixed value or a row of another, such as a direct beam or the curve of one angle | a record of its last state, referenced by the template or the row | that record's outputs, once |
| a large finished value next to a growing one, such as a background volume | a record of it, referenced by the growing accumulator's template, read once when it opens and held next to its held state | that record's outputs, once |

```python
beam = client.compute(COPY, {'data': direct.ref('function')})   # direct: an accumulator whose runs are all pushed
iofq = client.accumulator(Template(IOFQ_MULTI, params={'beam_centre': ..., 'direct_beam': beam.ref('data')},
                                   blanks=('sample_runs', 'can_runs')))
```

A record of a state does not follow the accumulator's later pushes, and a row cannot be replaced, so the second and third cases fit an accumulator whose runs are all pushed.

The limitation: two separate accumulators that both still grow cannot meet in one request.
A request that combines them reads a record of one, which copies it once per record and, on the service, writes it.

**A record of a state** is a request of a spec that returns a copy of what it reads. There is no special call.

## Alternatives considered

- **A new value per push.** A push needs two values at once.
- **Add in place, and copy for each request that reads it.** Each cut needs a second value for a state no one keeps, and D7 holds one per cut until it is released.
- **Copy-on-write per chunk**, as versioned chunked array stores do, to keep many states of a huge volume cheaply. No earlier state is needed; the cuts that users keep are their own records.
- **Snapshots as records.** `client.submit(accumulator)` makes a record whose output is the value so far, valid until the next push, and requests reference that record. Such a record is not a request, and it is the one record whose value ends at a push while its client keeps it. A read that pins the state says the same without either.
- **Computing every output at every push**, as `StreamProcessor` callers do on a fixed cadence. It costs a finalize per push whether or not anyone reads it. Computing at the read lets the driver decide how often to look.
- **Computing only the outputs a read asks for**, as a `finalize` over a list of keys would. Reads of different outputs of one state would each compute what those outputs share, and the binding, not the backend, knows how to share work between outputs.
- **A push that returns once its rows are added.** A failure to add would then raise in the driver. But an application would have to make every call that touches the accumulator from one thread of its own, since a submission that reads the accumulator would wait for the addition too. On the service, each such call would be an HTTP request held open for as long as a run takes to reduce. A record pinned to a state that a push does not reach is no worse than a record whose input fails, and it fails the same way.
- **Skipping a push whose record failed**, so that the accumulator goes on. State n would then not be the plain request over the first n pushes the log holds.
- **Every spec that is accumulated accepts an empty table.** A spec could then not require at least one run of a plain request, and a reference to an empty state means nothing.
- **Checking the plain request at each push.** Each push then costs time in proportion to the rows before it, whether or not anyone reads the state, and the accumulator must keep whether each state may be read. Refusing such a push would be worse: a table that needs two rows, or two tables that each need one, could not be filled one push at a time.
- **Typing the fixed values by the params model with the tables empty.** A model validator that needs rows would refuse the opening, so every spec that is accumulated would need validators that accept empty tables. Typing each field alone needs no such rule, and the model's validators still run at each read.
- **Rows and templates that read a copy of an accumulator's state when pushed or opened.** This saves the record, not the copy. On the service the value would still have to reach the other accumulator, through a file or a transfer.
- **A request that reads two accumulators.** Their held states would have to be in one process, which the service may not offer (see What may read an accumulator).

## Consequences

- An accumulator keeps one held state, plus the rows of the push being added, plus the outputs of the current state once it has been read.
- If `outputs` allocates, such as a normalized volume, each read state costs those outputs once. They are kept until the next push, so such an accumulator holds them while it waits for the next run; the peak is the same, since a read holds them anyway. A volume whose outputs are what it holds, such as intensity and normalisation grids, costs nothing more, and a cut divides after slicing.
- A long reader holds back the next addition, but not the driver.
- A driver does not slow down when the backend falls behind. Pushes queue as rows, which hold references, not data; but every read the driver submits runs, and each holds back the next addition. A driver that should skip cuts while it is behind checks whether its previous cut has finished.
- `push` does not raise a failure to add. The failure reaches the driver at its next push and at every read of a later state.
- A record that a push references and that fails stops the accumulator.
- A workflow must not return an output that shares memory with a value it reads, such as a slice that is a view of it. This is documented, not enforced.
- Validators that read more than one value must not change a value (Opening). This is documented, not enforced.
- The binding decides whether a push needs a second volume-sized array. Binning a run's events into the held grid does not. Binning them with `sc.hist` and adding the result does.
- `ess.reduce.spec` has a third reference form, to a state of an accumulator, next to outputs of records and datasets. A request holds such references to at most one accumulator.
- History keeps each accumulator's template, so that provenance expands `(accumulator, upto)` into the plain request over the rows of the first `upto` pushes ([ADR 0004](0004-history-is-append-only-lists.md)).
- The first read of each state validates its plain request, at a cost that grows with the number of rows: in the in-process backend, about 1.3 ms at 300 rows and 4 ms at a thousand. A driver that reads after every push, as story D7 does, pays it at every push, so the total grows with the square of the number of pushes: about 0.2 s for D7's 300 pushes, and 2 s for a thousand. A driver that reads rarely pays rarely.
- A table with a maximum length becomes unreadable for good once a push exceeds it, since no row can be removed.

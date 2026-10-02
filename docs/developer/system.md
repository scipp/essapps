# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values.
The in-process backend implements the history, the clients, and dropping values.
It keeps values in memory only, so retention and saving are not implemented.**

[README.md](README.md) describes the API: what workflow authors, app authors, and notebooks write, and what they can rely on.
This document describes how the backend keeps what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where stages and accumulators run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

This document uses the terms of README.md (see its Terms table), in particular spec, binding, client, record, stage, accumulator, and snapshot.
Story IDs such as D7 refer to [user-stories.md](user-stories.md) and [system-stories.md](system-stories.md).
It adds four terms of its own:

| Term | What it is |
|---|---|
| client entry | what the backend keeps for one client: its proposal, the records whose values it keeps, its stages, and its accumulators |
| event | one entry in the log, such as "record #2 finished" |
| log | the backend's history: an append-only list of events |
| view | an index the backend builds from the events, such as records by label; queries read views |

## History and values

The backend keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept for a retention period.
- **Values**: the outputs of records, what a stage computed, the combined value of an accumulator. They are large, and each is kept only while a client keeps it or a pending request that reads it has yet to run (see Values), or once it is saved.

A record is history; its output values are not.
Reading an output whose value is not kept raises an error, and a request that references it is refused at submission; the record stays.

## The log

The backend's history is an append-only log with three kinds of event:

| Event | Holds | Appended when |
|---|---|---|
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or snapshot, its output names, label, member | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `pushed` | accumulator ID, the element as a request over it holds it | an element is pushed into an accumulator |

Client entries are not history: opening or ending a client, making a stage or an accumulator, and releasing anything write nothing, and no client entry survives a restart.
A record does not say which stage it went through.
A snapshot names its accumulator, so that the pushes it covers can be found.

### An example

Story D7 is the loop over arrivals in README.md: each angle of a rotation scan is reduced, pushed into an accumulator `volume` once it has finished, and a snapshot of the volume feeds a cut.
With two angles, where the angle of run 2 finishes first and so is pushed first, the log reads (`#n` is a record, `a` the accumulator, JSON abbreviated):

```text
submitted  #1  angle/v1  {run: uuid:run-1}
submitted  #2  angle/v1  {run: uuid:run-2}
finished   #2  completed
pushed     a   {counts: #2.counts}
submitted  #3  volume/v1  snapshot of a, upto=1
finished   #3  completed
submitted  #4  cut/v1  {data: #3.counts, energy_transfer: 2.0}  label=cut member=17
finished   #1  completed
finished   #4  completed
pushed     a   {counts: #1.counts}
submitted  #5  volume/v1  snapshot of a, upto=2
finished   #5  completed
submitted  #6  cut/v1  {data: #5.counts, energy_transfer: 2.0}  label=cut member=17
finished   #6  completed
```

`uuid:run-1` is the dataset identity the backend resolved `dataset(run=1)` to.
`upto=2` says that the snapshot covers the first two pushes into `a`.
The second push waits until cut `#4` has finished, since `#4` reads the value that the push adds to (see Accumulators).
Each angle adds seven events of constant size, however many angles came before.

### What the log records

- **What the backend accepted, not what the client called.** Dataset names are resolved and defaults filled in. Resolving `dataset(run=4711)` again later could give another dataset; the logged identity cannot change.
- **Only accepted changes.** A change is checked before its event is appended. A refused call writes nothing, so every event in the log can be applied when the log is read again.
- **One event per submission.** A submission of 500 requests is one `submitted` event, so a backend that stops half-way has logged all of it or none of it.
- **No values.** Output values, what a stage computed, and an accumulator's combined value are not history.
- **JSON only.** Events are JSON. Request values already are, since references in `ess.reduce.spec` are plain dicts.
- **The same values, live or read back.** The backend converts each event to JSON and back before applying it. A record therefore holds the same values whether it was just made or read from a file. For example, a tuple given as a parameter is a list in the record. A binding gets the values after the spec's params model has validated them again, so it receives the types the model declares.

## Views

The backend applies each event to its views (`Views.apply` in `views.py`).
It does this the same way when it appends a new event and when it reads an existing log, so a backend that reads its log again has the same views.

| View | Used by |
|---|---|
| records by ID | `client.records`, `client.provenance`, checks of references |
| the `finished` event by record ID; a record without one is pending | `client.status`, `client.wait`, `client.failure`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| each accumulator's elements | what its snapshots read (see Records) |

A view changes only when an event is applied, and how it changes depends only on the events.
Queries read the views, never the log.

Live state that is not history is not in the views either: client entries, output values, which pending records hold which values, which snapshots have ended, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the views, and it is lost when the backend stops.

The views depend on three orders in the log:

- a record's `submitted` before its `finished`;
- the submissions under one label, since `client.latest` returns the newest;
- the pushes into one accumulator, since a snapshot covers the first `upto`.

### Records

A record stores what its `submitted` event holds: a request, or for a snapshot `Snapshot(spec, accumulator, upto)`.
Its status is in its `finished` event, so a record never changes and a client's copy of it is never out of date.
A snapshot does not list what it read; that is the first `upto` elements in its accumulator's view.
Provenance asks the backend what each record read (`Backend.inputs`), so it works for a snapshot as for a request:

```python
total = client.submit(volume)          # a snapshot of volume, upto=1000
total.submitted                        # Snapshot(spec=volume/v1, accumulator=volume.id, upto=1000)
client.provenance(total).records()     # the 1000 angles, from the accumulator's view
total.request                          # TypeError: a snapshot is not a request
```

### Restart

A backend given a log that already has events applies them, then runs the records that are still pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

- A pending record runs from scratch, without its stage, since client entries do not survive a restart.
- A snapshot is pending only if the backend stopped between its `submitted` and `finished` events. It fails, since its accumulator did not survive the restart.
- A pending record fails if the value of one of its inputs is not kept. The in-process backend keeps values only in memory, so after a restart this happens to every pending record with an input that had already completed.
- No client keeps the outputs of a record that completes after a restart, since the client that made it is gone. Its values are dropped once the pending records that read them have run.

This is what system stories B5 (notebook kernel dies) and H2 (backend upgrade with runs in flight) need from history.
H2 also needs every event format to stay readable across versions.

## Where the log lives

The design needs the log to be append-only and ordered; it does not depend on where the log is stored.

- In-process backend: in memory, or a file of JSON lines. A write that fails, as on a full disk (system story H1), leaves the file as it was. If a crash cuts the last line short while it is being written, that line is dropped when the file is read; any other line that is not an event is an error.
- A hosted backend: a file, a database table that is only appended to, or Kafka.

Each log has exactly one backend, and each backend one log.
A backend holds an exclusive lock (`flock`) on its log file from start to close, and a second backend on the same file, in any process, is refused at start.
The operating system releases the lock when the process ends, so a backend started after a crash or for an upgrade (system story H2) takes the file over.
Two notebooks that share results are clients of one backend; a backend in each notebook shares nothing.

A hosted backend may split its log by proposal.
This keeps the three orders the views depend on (see Views), since each lies within one proposal.

## Accumulators

An accumulator lives in its client's entry in the backend; the `Accumulator` that `client.accumulator` returns is a handle to it.
In story D7, the driver pushes each angle into the volume once it has finished.
The backend does this:

```python
held = binding.accumulator()             # when the accumulator opens: the combined value, not history
snapshots = []                           # the snapshots taken since the last push

def push(element):                       # element: a row of the table, as a request takes it
    wait(element)                        # until its records have finished
    check(element)                       # as for a request over [element]; its records have completed
    with lock:                           # the accumulator's (see Push)
        end(snapshots)                   # refuse new requests that reference them
        wait(snapshots)                  # until the requests that read them have run; drops their values
        held.push(read(element))         # combine the values, in place if the binding does
        append(Pushed(accumulator, element))

def snapshot():
    with lock:
        upto = len(elements)             # the elements pushed so far
        append(Submitted(Snapshot(spec, accumulator, upto)))
        complete(record, held.value)     # nothing runs; the outputs are held.value itself, not a copy
        snapshots.append(record)
```

**Push.** A push gets the check a request over that one element gets: the element is a valid row of the table's model, with no field the model lacks, and its references name completed records of the same proposal whose outputs fit the fields and are still kept.
The log holds the element as that request would hold it, with names resolved and defaults filled in.
A push that does not fit, or whose combining fails, appends nothing.
After a failed combine the accumulator takes no more pushes or snapshots, since the binding may hold part of the element; the driver opens a new accumulator.
The accumulator does not keep its elements' values after combining them.

A push waits for the records it references to finish before it is checked, so whether it is refused does not depend on timing.
The element gets its position only once it is combined, so a snapshot never waits for an unfinished element, and an element that finished first is never held back behind one pushed earlier.
A driver that pushes records as they finish, as `client.as_completed` yields them, never waits for them in the push.

Combining can take long.
It runs under the accumulator's own lock, not under the backend's lock that submissions also take, so a long combine holds up only the pushes and snapshots of the same accumulator.
The pushes are logged in the order they were combined.
A snapshot takes the accumulator's lock too, then the backend's, as a push does, so it never reads a value in the middle of a combine.

**Snapshot.** The `finished` event of a snapshot follows its `submitted` event at once.
A snapshot takes no label or member; the requests that read it do.
Its value is the value of the plain request over the same list of elements, since both combine the elements in list order.
The list is in push order, which in D7 is the order in which the angles finished, so two runs over the same scan may list the angles in different orders.
The `submitted` event names the accumulator and a count instead of the list, since the elements are already in the log as the accumulator's `pushed` events (ADR 0001).
The section Records above describes how provenance reads the elements.

A snapshot's outputs are `held.value` itself, not a copy, and the snapshots between two pushes share it.
A binding that adds in place changes that value at the next push, so a snapshot's value is kept until the next push into its accumulator, or until the accumulator is released or its client ends.
The client keeping the snapshot does not extend it.

A push ends the snapshots taken since the previous push before it combines.
From then on a request that references one is refused at submission, with a message that says the value ended at a push.
The push then waits until the requests that read those snapshots and were accepted before have run, not only started, since a running workflow holds the value; this includes a cancelled request whose workflow still runs.
Their values are then dropped, and reading one raises as for any value not kept; the records stay.
No request waits for a push, so the wait ends; a long request that reads a snapshot holds back the next push.
Releasing the accumulator or ending its client ends its snapshots in the same way, but does not wait: their values are dropped once the requests that read them have run.

`client.output` of a snapshot returns a copy, so a value read in a notebook does not change at the next push; the next push waits for the copy as for a request.
A request that reads a snapshot reads the value itself, so its outputs must not share memory with it, such as a view of it: they would change at the next push.
To keep an earlier state, a driver submits a request that reduces or copies the snapshot; saving it is part of the provenance and publication sub-design.

**Binding.** An accumulator needs a binding that makes element accumulators, like `sciline.Accumulator` does for one key; opening one with any other binding is refused.
A plain request over a table works with any binding.

```python
held = binding.accumulator()              # nothing pushed yet
held.push({'counts': counts_1})           # values, not references
held.push({'counts': counts_2})
held.value                                # {'counts': counts_1 + counts_2}
```

The backend reads `value` when a snapshot is taken, and that value itself becomes the snapshot's outputs.
What the element accumulator holds need not be the outputs: one for a mean holds a sum and a count, and `value` divides them.
A push may modify the value in place, since the snapshots that share it have ended and their readers have run.
A push must not modify its element, and an element accumulator that starts from the first element copies it, so that adding in place never changes the output the element came from.
The backend calls `push` and `value` under the accumulator's lock, so from one thread at a time.
`combine(operator.iadd)` adds in place, and `combine(operator.add)` makes a new value at each push; both copy the first element and give the same values, for an accumulator and a plain request over the table alike.
The stories bind `VOLUME` to `combine(operator.iadd)`.

## Clients

A client is one entry in the backend, from `open_client` to `close_client`.
The entry holds the client's proposal and submitter, the IDs of the records whose values it keeps, its stages, and its accumulators.
Every call of the backend names its client, and the backend takes the proposal from the entry.
A call of a client without an entry raises `ClientEnded`; the backend is the only place that checks.
`client.close()` calls `close_client`, which removes the entry and releases everything in it.
A hosted service also ends a client whose lease runs out, by the same call; leases are not implemented.

## Values

README.md states which output values are kept (How long records and values are kept).
The backend keeps two things for that rule:

- in each client entry, the IDs of the records the client made and has not released;
- for each output, how many pending records reference it and have not yet run.

A pending record holds its inputs from submission until its workflow returns, even if the record is cancelled meanwhile, since the workflow still reads them.
An output value is dropped once no client entry keeps its record and no pending record that reads it has yet to run.
A snapshot leaves its client entry when it ends (see Accumulators), so its value is then dropped once the records that read it have run.
The backend checks this when a client releases a record or ends, when a snapshot ends, when a workflow returns or a record finishes without running, and when a record completes, since a record released while pending drops its outputs as soon as it completes.

A stage keeps what it computed from its fixed values, and an accumulator its combined value, until the client releases them or ends.
What a stage computed is a cache: the backend may drop it at any time, and the next call through the stage computes it again and makes the same record.
The in-process backend never drops it.

A label names records and keeps no values.
A value that must outlive its client is saved (part of the provenance and publication sub-design).

How the stories fare:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's client |
| B1, S2: tuning steps | each step: the notebook's client, until the notebook releases it; the loaded run: the stage |
| C5: two stages tuned together, both results read afterwards | the notebook's client |
| C1, G4: a beam centre used by other requests or another notebook | the client of the notebook that made it, until it releases it or ends; for tomorrow's batch, save it |
| D2: overnight batch, laptop closed | the requests submitted with a place to save to, not designed yet; without one, only the records survive the night |
| D6: a batch's results read weeks later | saved; not designed yet |
| D7: a snapshot per angle, a cut per snapshot | the volume: the accumulator, which adds each angle in place, and each snapshot shares it until the next push, which waits until the snapshot's cut has run; each angle: the notebook's client, until it releases the angle after pushing it; the cuts: the notebook's client. One volume is kept, not one per snapshot |
| E1: the curve a rule made, read later | the rule saves what it makes; not designed yet |
| B5: kernel dies | what was saved; all history |

## Retention

History is kept for a retention period.
This works like a garbage collector whose roots are the events younger than that period: an older event is kept while a kept event depends on it, directly or through other kept events; everything else is dropped.
A record's submission depends on the submissions of the records it references, and a snapshot depends on the pushes it covers.

Dependencies point only backwards in time, so there are no cycles: a set of old events that no young event reaches is dropped as a whole.
A long history stays alive only if something young depends on it.
For example, an accumulator with a snapshot every day for a whole cycle keeps all its pushes; pushes are small.
Without this rule, a snapshot whose pushes had expired could not state what it covers.
Publishing writes the provenance, flattened from the log, into the catalogue entry, so what is published does not depend on retention.

Retention is not implemented.

## Open

- Leases of the clients of a hosted backend: how long one lasts and how a client renews it.
- The store of saved values, and where a batch or rule says to save to (with the provenance and publication sub-design).
- A forwarder: something a client keeps that holds the last value pushed into it, as in sciline. It joins stages and accumulators when a story needs one, for example a driving server that shows the latest curve of each sample.

# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values.
The in-process backend implements the history and the holders.
It keeps every value in memory, so dropping values, retention, and saving are not implemented.**

[README.md](README.md) describes the API: what users and workflow authors write, and what they can rely on.
This document describes how the backend keeps what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where sessions run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

This document uses the terms of README.md (see its Terms table), in particular spec, binding, record, session, stage, accumulator, and snapshot.
Story IDs such as D7 refer to [user-stories.md](user-stories.md) and [system-stories.md](system-stories.md).
It adds three terms of its own:

| Term | What it is |
|---|---|
| event | one entry in the log, such as "record #2 finished" |
| log | the backend's history: an append-only list of events |
| view | an index the backend builds from the events, such as records by label; queries read views |

## History and values

The backend keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept for a retention period.
- **Values**: the outputs of records, what a stage computed, the combined value of an accumulator. They are large, and each is kept only while something holds it or once it is saved.

A record is history; its output values are not.
Reading an output that is no longer kept raises an error, and a request that references it is refused at submission; the record stays.

## The log

The backend's history is an append-only log with three kinds of event:

| Event | Holds | Appended when |
|---|---|---|
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or snapshot, its output names, label, member | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `pushed` | accumulator ID, the element: a reference per field | an element is pushed into an accumulator |

Sessions and their holders are not history: opening or ending one writes nothing, and none survives a restart.
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
submitted  #3  sum[Counts]/v1  snapshot of a, upto=1
finished   #3  completed
submitted  #4  cut/v1  {data: #3.counts, energy_transfer: 2.0}  label=cut member=17
finished   #1  completed
pushed     a   {counts: #1.counts}
submitted  #5  sum[Counts]/v1  snapshot of a, upto=2
finished   #5  completed
submitted  #6  cut/v1  {data: #5.counts, energy_transfer: 2.0}  label=cut member=17
finished   #4  completed
finished   #6  completed
```

`uuid:run-1` is the dataset identity the backend resolved `dataset(run=1)` to.
`upto=2` says that the snapshot covers the first two pushes into `a`.
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
| records by ID, with status and failure | `client.wait`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| each accumulator's elements | what its snapshots read (see Records) |

A view changes only when an event is applied, and how it changes depends only on the events.
Queries read the views, never the log.

Live state that is not history is not in the views either: sessions and their holders, output values, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the views, and it is lost when the backend stops.

The views depend on three orders in the log:

- a record's `submitted` before its `finished`;
- the submissions under one label, since `client.latest` returns the newest;
- the pushes into one accumulator, since a snapshot covers the first `upto`.

### Records

A record stores what the log holds: a request, or for a snapshot `Snapshot(spec, accumulator, upto)`.
A snapshot does not list what it read; that is the first `upto` elements in its accumulator's view.
Provenance asks the backend what each record read (`Backend.inputs`), so it works for a snapshot as for a request:

```python
total = client.submit(volume)          # a snapshot of volume, upto=1000
total.submitted                        # Snapshot(spec=sum[Counts]/v1, accumulator=volume.id, upto=1000)
client.provenance(total).records()     # the 1000 angles, from the accumulator's view
total.request                          # TypeError: a snapshot is not a request
```

### Restart

A backend given a log that already has events applies them, then runs the records that are still pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

- A pending record runs from scratch, without its stage, since sessions do not survive a restart.
- A snapshot is pending only if the backend stopped between its `submitted` and `finished` events. It fails, since its accumulator did not survive the restart.
- A pending record whose inputs' values were not kept fails with "no output". The in-process backend keeps values only in memory, so after a restart this happens to every pending record with an input that had already completed.

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

An accumulator lives in the backend; the `Accumulator` a session returns is a handle to it.
In story D7, the driver pushes each angle into the volume once it has finished.
The backend does this:

```python
held = binding.accumulator()             # when the accumulator opens: the combined value, not history

def push(element):                       # element: a reference per field
    check(element)                       # as for a request over [element]; its records have completed
    held.push(read(element))             # combine the values (see Push for which lock)
    append(Pushed(accumulator, element))

def snapshot():
    upto = len(elements)                 # the elements pushed so far
    append(Submitted(Snapshot(spec, accumulator, upto)))
    complete(record, held.value)         # nothing runs
```

**Push.** A push gets the check a request over that one element gets: the element has the fields of the element model, and its references name completed records of the same proposal whose outputs fit the fields and are still kept.
A push that does not fit, or whose combining fails, appends nothing.
After a failed combine the accumulator takes no more pushes or snapshots, since the binding may hold part of the element; the driver opens a new accumulator.
The accumulator does not keep its elements' values after combining them.

The push refuses pending records so that all waiting happens in the driver, for example in `client.as_completed`.
If the backend accepted pending records, it would have to make a snapshot wait for an unfinished element, or hold back an element that finished before one pushed earlier.

Combining can take long.
It runs under the accumulator's own lock, not under the backend's lock that submissions also take, so a long combine holds up only the pushes into the same accumulator.
Those are logged in the order they were combined.

**Snapshot.** The `finished` event of a snapshot follows its `submitted` event at once.
A snapshot takes no label or member; the requests that read it do.
Its value is the value of the plain request over the same list of elements, since both combine the elements in list order.
The list is in push order, which in D7 is the order in which the angles finished, so two runs over the same scan may list the angles in different orders.
The `submitted` event names the accumulator and a count instead of the list, since the elements are already in the log as the accumulator's `pushed` events (ADR 0001).
The section Records above describes how provenance reads the elements.

**Binding.** An accumulator needs a binding that makes element accumulators, like `sciline.Accumulator` does for one key; opening one with any other binding is refused.
A plain request over a list works with any binding.

```python
held = binding.accumulator()              # nothing pushed yet
held.push({'counts': counts_1})           # values, not references
held.push({'counts': counts_2})
held.value                                # {'counts': counts_1 + counts_2}
```

The backend reads `value` after every push, and that value becomes a snapshot's output.
So a later push must not modify a value read before it: each push makes a new value instead of adding in place.
`combine(operator.add)` is such a binding, since `operator.add` returns a new value; the stories bind `SUM.of(Counts)` to it.

## Values

An output value is kept while one of these holds:

1. a pending request reads it;
2. a record in a client holds it, as a dask future holds its value: the value is released when the record object is garbage-collected, or when the client is gone and its lease has run out;
3. a holder in a session holds it: a stage what it computed from its fixed values, an accumulator its combined value;
4. it has been saved (part of the provenance and publication sub-design).

A label names records and keeps no values.
A value that must outlive its client is saved.

How the stories fare:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's record (2) |
| B1, S2: tuning steps | the record of the latest step (2); the stage's fixed part (3) |
| C5: two stages tuned together, both results read afterwards | the records the notebook keeps in a list (2) |
| C1, G4: a beam centre used by other requests or another notebook | the records of the notebooks that hold it (2); for tomorrow's batch, save it (4) |
| D2: overnight batch, laptop closed | the requests submitted with a place to save to (4), not designed yet; without one, only the records survive the night |
| D6: a batch's results read weeks later | saved (4); not designed yet |
| D7: a snapshot per angle, a cut per snapshot | each snapshot until its cut has run (1); the accumulator's combined value (3); the cuts the notebook keeps in a list (2). The volumes of earlier snapshots are not kept |
| E1: the curve a rule made, read later | the rule saves what it makes (4); not designed yet |
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

- Leases for records held by clients of a hosted backend.
- The store of saved values, and where a batch or rule says to save to (with the provenance and publication sub-design).
- A forwarder: a holder of the last value pushed into it, as in sciline. It joins the holders when a story needs one, for example a driving server that shows the latest curve of each sample.

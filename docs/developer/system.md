# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values. The in-process backend implements the history and the holders; which values are dropped, retention, and saving are not implemented, since that backend keeps every value in memory.**

[README.md](README.md) describes the API: what users and workflow authors write, and what they can rely on.
This document describes how the system provides what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where sessions run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

Terms from README.md used here: a *spec*, and its *binding*, the code that computes it; a *record*; a *session* and the *holders* in it, stages and accumulators.
A *snapshot* of an accumulator is what `client.submit(accumulator)` makes: a record of the combined value of the elements pushed so far.

## History and values

The system keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept for a retention period.
- **Values**: the outputs of records, what a stage computed, the combined value of an accumulator. They are large, and each is kept only while something holds it or once it is saved.

A record is history. Reading an output that is no longer kept raises an error, and a request that references it is refused at submission; the record stays.

## The log

The backend's history is an append-only log of what ran, with which inputs, and what came of it, in three events:

| Event | Holds | Appended when |
|---|---|---|
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or snapshot, its output names, label, member | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `pushed` | accumulator ID, the element: a reference per field | an element is pushed |

Sessions and their holders are not history: opening or ending one writes nothing, and none survives a restart.
A record does not say which stage it went through; a snapshot names its accumulator so that the pushes it covers can be found.

Story D7 with two angles writes this (`#n` a record, `a` the accumulator, JSON abbreviated), where the angle of run 2 finishes first and so is pushed first:

```text
submitted  #1  angle/v1  {run: uuid:run-1}
submitted  #2  angle/v1  {run: uuid:run-2}
finished   #2  completed
pushed     a   {counts: #2.counts}
submitted  #3  sum[Counts]/v1  snapshot of a, upto=1
finished   #3  completed
submitted  #4  cut/v1  {data: #3.counts, index: 0}  label=cut member=17
finished   #1  completed
pushed     a   {counts: #1.counts}
submitted  #5  sum[Counts]/v1  snapshot of a, upto=2
finished   #5  completed
submitted  #6  cut/v1  {data: #5.counts, index: 0}  label=cut member=17
finished   #4  completed
finished   #6  completed
```

Each angle adds seven events of constant size, however many angles came before.

What the log records:

- **What the backend accepted, not what the client called.** Dataset names are resolved and defaults filled in. Replaying `dataset(run=4711)` later could name another dataset; the logged identity cannot.
- **Only what applies.** A change is checked before its event is appended. A refused call writes nothing, so the log can always be read again.
- **One event per submission.** A submission of 500 requests is one `submitted` event, so a backend that stops half-way has all of it or none of it.
- **No values.** Output values, what a stage computed from its fixed values, and an accumulator's combined value are not history.
- **Plain data.** Events are JSON. Request values already are, since references in `ess.reduce.spec` are plain dicts.
- **The same values, live or read back.** The backend applies each event as it reads back from its JSON, so a record holds its values the same way whether it was just made or read from a file: a tuple given as a parameter is a list in the record. A binding gets the values validated by the spec's params model again, in the types the model declares.

### Where the log lives

The design depends on the log being append-only and ordered, not on where it is stored.

- In-process backend: in memory, or a file of JSON lines. A write that fails, as on a full disk (system story H1), leaves the file as it was. A last line cut short by a crash while it was written is dropped when the file is read; any other line that is not an event is an error.
- A hosted backend: a file, a database table used only by appending, or Kafka.

One log per backend, and one backend per log.
A backend holds its log file from start to close with an exclusive lock (`flock`), and a second backend on the same file, in any process, is refused at start.
The operating system lets go of the lock when the process ends, so a backend started after a crash or for an upgrade (system story H2) takes the file over.
Two notebooks that share results are clients of one backend; a backend in each notebook shares nothing.
The lock needs `fcntl`, so a log file does not work on Windows; a log in memory does.
The views depend on three orders, and a log split by proposal keeps all three, since each lies within one proposal:

- a record's `submitted` before its `finished`;
- the submissions under one label, since the latest is the current one;
- the pushes into one accumulator, since a snapshot covers the first `upto`.

## Views

The backend applies each event to its views (`Views.apply` in `views.py`), the same way when it appends the event and when it reads an existing log:

| View | Used by |
|---|---|
| records by ID, with status and failure | `client.wait`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| each accumulator's elements | what its snapshots read (see Records) |

A view changes only when an event is applied, and how it changes depends on nothing but the events.
Queries read the views, never the log.
What is not history is not a view either: sessions and their holders, output values, what a stage computed from its fixed values, an accumulator's combined value, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the views, and it is lost when the backend stops.

### Records

A record stores what the log holds: a request, or a snapshot `Snapshot(spec, accumulator, upto)`.
A snapshot does not list what it read: that is the first `upto` elements in its accumulator's view.
Provenance asks the backend what each record read (`Backend.inputs`), so it reaches through a snapshot like through a request:

```python
total = client.submit(volume)          # a snapshot of volume, upto=1000
total.submitted                        # Snapshot(spec=sum[Counts]/v1, accumulator=volume.id, upto=1000)
client.provenance(total).records()     # the 1000 angles, from the accumulator's view
total.request                          # TypeError: a snapshot is not a request
```

### Restart

A backend given a log that already has events applies them and runs the records left pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

A pending record runs from scratch, without its stage, since sessions do not survive a restart.
A snapshot is pending only if the backend stopped between its `submitted` and `finished` events; it fails, since its accumulator did not survive the restart.
A pending record whose inputs' values were not kept fails with "no output"; the in-process backend keeps values in memory, so this happens to every pending record whose inputs completed before the restart.
This is what system stories B5 and H2 need from history; H2 also needs every event format to stay readable across versions.

## Accumulators

An accumulator lives in the backend; the `Accumulator` a session returns is a handle to it.
Story D7, the loop over arrivals in README.md, pushes each angle into a volume once it has finished.
The backend does this:

```python
held = binding.accumulator()             # when the accumulator opens: the combined value, not history

def push(element):                       # a reference per field
    check(element)                       # as a request over [element]; its records have completed
    held.push(read(element))             # under the accumulator's lock, outside the backend's
    append(Pushed(accumulator, element))

def snapshot():
    upto = len(elements)                 # the elements pushed so far
    append(Submitted(Snapshot(spec, accumulator, upto)))
    complete(record, held.value)         # nothing runs
```

**Push.** The check is the one a request over that one element gets: the element has the fields of the element model, and its references name completed records of the same proposal whose outputs fit the fields and are still kept.
Taking only completed records keeps waiting in the driver: the backend never makes a snapshot wait for an element, or holds back an element that finished before one pushed earlier.
A push that does not fit, or whose combining fails, appends nothing.
After a failed combine the accumulator takes no more pushes or snapshots, since the binding may hold part of the element; the driver opens a new accumulator.
The accumulator does not keep its elements' values after combining them.
A long combine holds up only the pushes into the same accumulator, which are logged in the order they were combined.

**Snapshot.** The `finished` event of a snapshot follows its `submitted` event at once.
A snapshot takes no label or member; the requests that read it do.
Its value is the value of the plain request over the same list, since both combine the elements in list order.
The list is in push order, which in D7 is the order in which the angles finished, so two runs over the same scan may list the angles in different orders.
The `submitted` event names the accumulator and a count instead of the list, since the elements are already in the log as the accumulator's `pushed` events (ADR 0001).
The section Records above describes how provenance reads the elements.

**Binding.** An accumulator needs a binding that makes element accumulators, like `sciline.Accumulator` for one key; opening one with any other binding is refused.
A plain request over a list works with any binding.

```python
held = binding.accumulator()              # nothing pushed yet
held.push({'counts': counts_1})           # values, not references
held.push({'counts': counts_2})
held.value                                # {'counts': counts_1 + counts_2}
```

The backend reads `value` after every push, and a later push must not change a value read before it, since a snapshot's output is that value.
`combine(operator.add)` is such a binding; the stories bind `SUM.of(Counts)` to it.

## Values

An output value is kept while one of these holds:

1. a pending request reads it;
2. a record handle in a client holds it, as a dask future holds its value: it is released when the handle is garbage-collected, or when the client is gone and its lease has run out;
3. a holder in a session holds it: a stage what it computed from its fixed values, an accumulator its combined value;
4. it has been saved (the provenance and publication sub-design).

A label names records and keeps no values.
A value that must outlive its client is saved.
A forwarder, a holder of the last value pushed into it as in sciline, joins the holders when a story needs one, for example a driving server that shows the latest curve of each sample.

How the stories fare:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's handle (2) |
| B1, S2: tuning steps | the handle of the latest step (2); the stage's fixed part (3) |
| C5: two stages tuned together, both results read afterwards | the handles the notebook keeps in a list (2) |
| C1, G4: a beam centre used by other requests or another notebook | the handles of the notebooks that hold it (2); for tomorrow's batch, save it (4) |
| D2: overnight batch, laptop closed | the requests submitted with a place to save to (4), not designed yet; without one, only the records survive the night |
| D6: a batch's results read weeks later | saved (4); not designed yet |
| D7: a snapshot per angle, a cut per snapshot | each snapshot until its cut has run (1); the accumulator's combined value (3); the cuts the notebook keeps in a list (2). The volumes of earlier snapshots are not kept |
| E1: the curve a rule made, read later | the rule saves what it makes (4); not designed yet |
| B5: kernel dies | what was saved; all history |

## Retention

History is kept for a retention period, like a garbage collector whose roots are the events younger than that period.
An older event is kept while a kept event depends on it, directly or through other kept events; everything else is dropped.
A record's submission depends on the submissions of the records it references, and a snapshot depends on the pushes it covers.

Dependencies point only backwards in time, so there are no cycles: a set of old events that no young event reaches is dropped as a whole.
A long history stays alive only if something young depends on it, such as an accumulator with a snapshot every day for a whole cycle, which keeps all its pushes; pushes are small.
Without the rule, a snapshot whose pushes had expired could not state its own request.
Publishing writes the provenance, flattened from the log, into the catalogue entry, so what is published does not depend on retention.

Retention is not implemented.

## Open

- Leases for record handles in a hosted backend.
- The store of saved values, and where a batch or rule says to save to (with the provenance and publication sub-design).

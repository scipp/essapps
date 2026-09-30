# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values. The in-process backend implements the history and the holders; which values are dropped, retention, and saving are not implemented, since that backend keeps every value in memory.**

[README.md](README.md) describes the API: what users and workflow authors write, and what they can rely on.
This document describes how the system provides what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where sessions run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

Terms from README.md used here: a *spec*, and its *binding*, the code that computes it; a *record*; a *session* and the *holders* in it, stages and accumulators.
A *snapshot* of an accumulator is what `client.submit(accumulator)` makes: a record of the accumulator spec over the elements pushed so far.

## History and values

The system keeps two things with different lifetimes:

- **History**: what ran, with which inputs, and what came of it. It is small, and it is kept for a retention period.
- **Values**: the outputs of records, what a stage computed, the combined value of an accumulator. They are large, and each is kept only while something holds it or once it is saved.

A record is history. Reading an output that is no longer kept raises an error, and a request that references it is refused at submission; the record stays.

## The log

The backend's history is an append-only log.
Every change the backend accepts is one event:

| Event | Holds | Appended when |
|---|---|---|
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or snapshot, its output names, label, member, stage | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `session-opened`, `session-closed` | session ID, proposal | a session opens or ends |
| `stage-opened` | stage ID, session, spec, blanks | a stage opens |
| `accumulator-opened` | accumulator ID, session, accumulator spec | an accumulator opens |
| `pushed` | accumulator ID, the element: a reference per field | an element is pushed |

Story D7 with two angles writes this (IDs shortened, JSON abbreviated), where the angle of run 2 finishes first and so is pushed first:

```text
session-opened      #0  p1
accumulator-opened  #1  session #0  sum[Counts]/v1
submitted           #2  angle/v1  {run: uuid:run-1}
submitted           #3  angle/v1  {run: uuid:run-2}
finished            #3  completed
pushed              #1  {counts: #3.counts}
submitted           #4  sum[Counts]/v1  snapshot of #1, upto=1
finished            #4  completed
submitted           #5  cut/v1  {data: #4.counts, index: 0}  label=cut member=17
finished            #2  completed
pushed              #1  {counts: #2.counts}
submitted           #6  sum[Counts]/v1  snapshot of #1, upto=2
finished            #6  completed
submitted           #7  cut/v1  {data: #6.counts, index: 0}  label=cut member=17
finished            #5  completed
finished            #7  completed
session-closed      #0
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

- In-process backend: in memory, or a file of JSON lines. A last line cut short by a crash while it was written is dropped when the file is read; any other line that is not an event is an error.
- A local backend shared by two notebooks (system story G4): a file appended under a lock (not implemented).
- A hosted backend: a file, a database table used only by appending, or Kafka.

One log per backend; each event names its proposal.
If the log is ever split, for example by proposal, only two orders must be kept: the pushes into one accumulator, and the events of one record.

## Views

The backend applies each event to its views (`Views.apply` in `views.py`), the same way when it appends the event and when it reads an existing log:

| View | Used by |
|---|---|
| records by ID, with status and failure | `client.wait`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| open sessions, their stages and accumulators | calls through holders |
| each accumulator's elements | the requests of its snapshots (see Records) |

A view changes only when an event is applied, and how it changes depends on nothing but the events.
Queries read the views, never the log.
What is not history is not a view either: output values, what a stage computed from its fixed values, an accumulator's combined value, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the views, and it is lost when the backend stops.

### Records

A record stores what the log holds: a request, or a snapshot `Snapshot(spec, accumulator, upto)`.
`record.request` is the full request either way. For a snapshot, the backend builds it from the accumulator's first `upto` elements when someone accesses it:

```python
total = client.submit(volume)        # the log holds: a snapshot of volume, upto=1000
total.request.params['counts']       # the 1000 references, built now
```

Only provenance, a comparison, code that reads the request, or a snapshot through a binding that is only a function builds this list.
To build the list, a snapshot's record keeps a reference to its accumulator's elements in the backend.
A copy made from the record's plain data alone, such as one sent over a network, cannot build it; a client that receives records over a network would have to fetch the elements itself.

### Restart

A backend given a log that already has events applies them, closes the sessions left open, and runs the records left pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

A pending record runs from scratch, without its stage, since sessions do not survive a restart.
A snapshot is pending only through a binding that is only a function, or if the backend stopped between its `submitted` and `finished` events; it runs as the plain request over its elements.
A pending record whose inputs' values were not kept fails with "no output"; the in-process backend keeps values in memory, so this happens to every pending record whose inputs completed before the restart.
This is what system stories B5 and H2 need from history; H2 also needs every event format to stay readable across versions.

## Accumulators

An accumulator lets a driver add elements one at a time and ask for a record of the combined value at any point, without combining the earlier elements again.
Story D7 reduces the angles of a scan in parallel and pushes each into a volume once it has finished:

```python
with client.session() as session:
    volume = session.accumulator(SUM.of(Counts))
    angles = (client.submit(ANGLE, {'run': run}) for run in datasets.watch(Selector(scan='17')))
    for angle in client.as_completed(angles):    # each angle once it has finished
        volume.push(angle)
        latest = client.submit(volume)           # a snapshot over the angles pushed so far
```

`volume` in the notebook is a handle.
The accumulator itself lives in the backend: its elements, and the combined value it keeps.
Each push is a call to the backend and a `pushed` event in the log.

**A push takes a finished record.** The backend refuses to push a record that has not completed.
The client also refuses a record handle that is still pending, even if the record has completed since, so that whether a push is refused does not depend on timing.
A driver waits for records as it does for any output: with `client.wait`, or for many records with `client.as_completed`, which yields them in the order they finish.
`as_completed` consumes its input in a thread, so the input may be a generator that waits for new datasets.
The elements are computed in parallel, wherever the backend runs them; only the pushes happen one after another.

If the backend took pending records, each snapshot would have to wait for its elements, fail when one of them fails, and hold back elements that finish early until those pushed before them have finished.
Waiting in the driver needs none of this.

**A push is checked and combined.** The backend checks a push as it would check a request over that one element: the element has the fields of the accumulator spec's element model, and its references name completed records of the same proposal whose outputs fit the fields and are still kept.
It then combines the element into the value it keeps, and appends the `pushed` event.
A push that does not fit, or whose combining fails, is refused and appends nothing.
After a failed combine the accumulator takes no more pushes or snapshots, since the binding may hold part of the element; the driver opens a new accumulator.
The accumulator does not keep its elements' values after combining them.

Combining runs outside the backend's lock, under a lock of the accumulator.
A long combine holds up only the pushes into the same accumulator, and those are logged in the order they were combined.

**A snapshot completes at submission.** The combined value covers exactly the elements pushed so far.
Submitting the accumulator makes a record of the accumulator spec over those elements and completes it with that value at once: its `finished` event follows its `submitted` event, and nothing waits.
The snapshot's value is the value of the plain request over the same list, since both combine the elements in list order.
The list is in the order of the pushes, which in D7 is the order in which the angles finished, so two runs over the same scan may list the angles in different orders.

**What a snapshot's record says.** After three pushes, a snapshot is the record of `SUM.of(Counts)` over `counts=[a1, a2, a3]`, the record that the plain request over the same elements makes.
Pushes continue after a snapshot is submitted, so its record must say which elements it covers.
The `submitted` event holds `Snapshot(spec, accumulator, upto)`, where `upto` is the number of elements pushed before the submission.
The elements themselves are already in the log, as the accumulator's `pushed` events in order.
Listing them in each snapshot instead would put 1 + 2 + ... + n references into the log for n snapshots, 500,500 for story D7's 1000 angles.
The section Records above describes how `record.request` builds the list from `upto`.

**Where the combining code comes from.** The binding of an accumulator spec may be a plain function over lists, which combines all elements in one call.
To keep a combined value, the backend needs a binding that can also make element accumulators: objects that take one element at a time and give the combined value so far, as `sciline.Accumulator` does for one key.

```python
held = binding.accumulator()              # nothing pushed yet
held.push({'counts': counts_1})           # values, not references
held.push({'counts': counts_2})
held.value                                # {'counts': counts_1 + counts_2}
```

The backend reads `value` after every push, and a later push must not change a value read before it, since a snapshot's output is that value.
`combine(operator.add)` is such a binding; the stories bind `SUM.of(Counts)` to it.
With a binding that is only a function, a push only checks and appends, and a snapshot runs as the plain request over its elements, calling the function with all of them.

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
A record's submission depends on the submissions of the records it references, and a snapshot depends on its accumulator's opening and on the pushes it covers.

Dependencies point only backwards in time, so there are no cycles: a set of old events that no young event reaches is dropped as a whole.
A long history stays alive only if something young depends on it, such as an accumulator with a snapshot every day for a whole cycle, which keeps all its pushes; pushes are small.
Without the rule, a snapshot whose pushes had expired could not state its own request.
Publishing writes the provenance, flattened from the log, into the catalogue entry, so what is published does not depend on retention.

Retention is not implemented.

## Open

- Leases for record handles in a hosted backend.
- The store of saved values, and where a batch or rule says to save to (with the provenance and publication sub-design).

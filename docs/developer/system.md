# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values. The in-process backend implements the history and the holders; which values are dropped, retention, and saving are not implemented, since that backend keeps every value in memory.**

[README.md](README.md) describes the API: what users and workflow authors write, and what they can rely on.
This document describes how the system provides what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where sessions run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

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
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or read, its output names, label, member, stage | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `session-opened`, `session-closed` | session ID, proposal | a session opens or ends |
| `stage-opened` | stage ID, session, spec, blanks | a stage opens |
| `accumulator-opened` | accumulator ID, session, accumulator spec | an accumulator opens |
| `pushed` | accumulator ID, the element: a reference per field | an element is pushed |

Story D7 with two angles writes this (IDs shortened, JSON abbreviated):

```text
session-opened      #0  p1
accumulator-opened  #1  session #0  sum[Counts]/v1
submitted           #2  angle/v1  {run: uuid:run-1}
finished            #2  completed
pushed              #1  {counts: #2.counts}
submitted           #3  sum[Counts]/v1  read of #1, upto=1
submitted           #4  cut/v1  {data: #3.counts, index: 0}  label=cut member=17
finished            #3  completed
finished            #4  completed
submitted           #5  angle/v1  {run: uuid:run-2}
finished            #5  completed
pushed              #1  {counts: #5.counts}
submitted           #6  sum[Counts]/v1  read of #1, upto=2
submitted           #7  cut/v1  {data: #6.counts, index: 0}  label=cut member=17
finished            #6  completed
finished            #7  completed
session-closed      #0
```

Each angle adds seven events of constant size, however many angles came before.

What the log records:

- **What the backend accepted, not what the client called.** Dataset names are resolved and defaults filled in. Replaying `dataset(run=4711)` later could name another dataset; the logged identity cannot.
- **Only what applies.** A change is checked before its event is appended. A refused call writes nothing, so the log can always be read again.
- **One event per submission.** A submission of 500 requests is one `submitted` event, so a backend that stops half-way has all of it or none of it.
- **No values.** Output values, what a stage computed from its fixed values, and an accumulator's combined value are not history.
- **Plain data.** Events are JSON. Request values already are, since references in `ess.reduce.spec` are plain dicts. The backend applies each event as it reads back from its JSON, so a record holds its values the same way whether it was just made or read from a file: a tuple given as a parameter is a list in the record. A binding gets the values validated by the spec's params model again, in the types the model declares.

### Where the log lives

The design depends on the log being append-only and ordered, not on where it is stored.

- In-process backend: in memory, or a file of JSON lines. A last line cut short by a crash while it was written is dropped when the file is read; any other line that is not an event is an error.
- A local backend shared by two notebooks (system story G4): a file appended under a lock (not implemented).
- A hosted backend: a file, a database table used only by appending, or Kafka.

One log per backend; each event names its proposal.
Only the order of pushes within one accumulator and the order of a record's own events matter.

## Views

The backend applies each event to its views (`Views.apply` in `views.py`), the same way when it appends the event and when it reads an existing log:

| View | Used by |
|---|---|
| records by ID, with status and failure | `client.wait`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| open sessions, their stages and accumulators | calls through holders |
| each accumulator's elements, and how many leading elements have completed | reads |
| for each unfinished record, where it was pushed as an element | releasing the reads that wait for it |

A view changes only when an event is applied, and how it changes depends on nothing but the events.
Queries read the views, never the log.
What is not history is not a view either: output values, a stage's staged callable, an accumulator's held value, what waits for what, and the queue of runnable work.
The backend keeps it apart from the views, and it is lost when the backend stops.

### Records

A record stores what the log holds: a request, or a read `Read(spec, accumulator, upto)`.
`record.request` is the full request either way; for a read it is built from the accumulator's first `upto` elements when accessed.

```python
total = client.submit(volume)             # logs: read of a1, upto=1000
total.request.params['counts']            # the 1000 references, built from a1's elements
total.request == plain.request            # the record of the plain request
```

Only provenance, a comparison, or a user reading the request builds the list, at O(k); scheduling, checks, and the client never do.
The backend binds each record of a read to its accumulator's elements. A copy made from the record's plain data alone, such as one sent over a network, has no elements; a client that receives records binds them to itself.

### Restart

A backend given a log that already has events applies them, closes the sessions left open, and runs the records left pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

A pending record runs from scratch, without its stage, since sessions do not survive a restart.
A pending read over an element that did not complete fails.
A pending record whose inputs' values were not kept fails with "no output"; the in-process backend keeps values in memory, so this happens to every pending record whose inputs completed before the restart.
This is what system stories B5 and H2 need from history; H2 also needs every event format to stay readable across versions.

## Accumulators

An accumulator is opened in the backend, and a push is a backend call that the log records.

- **A push is checked when it is made**, as a request over that element alone would be: the element's fields, readable records of the same proposal, outputs that fit. A read therefore needs no check per element.
- **A read waits until the records of its first k elements have completed.** The accumulator counts how many leading elements have completed, so waiting costs O(1) per event rather than a set of k inputs per read.
- **A read over an element that did not complete fails**, and later reads are refused at submission, naming the element.
- **The accumulator keeps the combined value.** Each read combines only the elements pushed since the previous read. The reads of one accumulator run one at a time and in order, since each continues from where the previous one stopped. A read whose combining fails drops the held value, and the next read combines from the first element.

A binding keeps a combined value if it can make element accumulators, like `sciline.Accumulator` for a whole element:

```python
class AccumulatorBinding(Binding, Protocol):
    def accumulator(self) -> ElementAccumulator: ...   # push(element), value

held = SUM_BINDING.accumulator()
held.push({'counts': a1})
held.push({'counts': a2})
held.value                                           # {'counts': a1 + a2}
```

`combine(operator.add)` is such a binding. It folds the elements in order for a plain request too, so both give the same value.
A binding without `accumulator()` combines all elements on every read.

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
| D2: overnight batch, laptop closed | the requests submitted with a place to save to (4); without one, only the records survive the night |
| D6: a batch's results read weeks later | saved (4); not designed yet |
| D7: a read per angle, a cut per read | each read until its cut has run (1); the accumulator's combined value (3); the cuts the notebook keeps in a list (2). The volumes of earlier reads are not kept |
| E1: the curve a rule made, read later | the rule saves what it makes (4); not designed yet |
| B5: kernel dies | what was saved; all history |

## Retention

History is kept for a retention period, like a garbage collector whose roots are the events younger than that period.
An older event is kept while a kept event depends on it, directly or through other kept events; everything else is dropped.
A record's submission depends on the submissions of the records it references, and a read depends on its accumulator's opening and on the pushes it covers.

Dependencies point only backwards in time, so there are no cycles: a set of old events that no young event reaches is dropped as a whole.
A long history stays alive only if something young depends on it, such as an accumulator read every day for a whole cycle, which keeps all its pushes; pushes are small.
Without the rule, a read whose pushes had expired could not state its own request.
Publishing writes the provenance, flattened from the log, into the catalogue entry, so what is published does not depend on retention.

Retention is not implemented.

## Open

- Whether an accumulator keeps the values of its elements after combining them. It needs them to start again after a failed read, and a pending read after its session has ended needs them all.
- Leases for record handles in a hosted backend.
- The store of saved values, and where a batch or rule says to save to (with the provenance and publication sub-design).

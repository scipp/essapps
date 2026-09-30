# Records as an event log

**Status: a proposal for a decision. The in-process backend on this branch implements the log, records as views of it, and accumulators in the backend. README.md changes when the proposal is adopted; "What changes in README.md" lists how.**

## Summary

- The backend appends every change it accepts to a log, one event per change. Records, labels, and provenance are views built from the log.
- History and values are kept apart. The log says what ran, with which inputs, and what came of it; it holds no output values. History is kept for a retention period. A value is kept while something holds it, or once it is saved.
- A read of an accumulator is logged as "accumulator a1, its first k elements". Its record still means the flat request over those k elements, built when someone asks for it.
- Story D7 becomes linear: 0.86 s for 1000 angles instead of 5.6 s, and 1000 stored references to elements instead of 501,500.
- The API stays as it is, except that a push that does not fit is refused at the push. The lifetime of values is the one question the API must still answer.

## The problem

D7 reads an accumulator after each of 1000 pushes. On `main`, each read's record lists every element pushed so far.
Every layer then handles every element of every read: the client, the checks, the scheduler, and provenance.
The arithmetic is a small part of the cost.

A record on `main` is a self-contained snapshot, kept for the medium term, never changed, and queryable by time.
For D7, the records together hold the element list of the volume at every past state, in full.
That is a time machine built from snapshots, and snapshots of a growing list grow quadratically.

There are two kinds of time machine, with very different costs:

| | Answers | Cost |
|---|---|---|
| History | what ran, with which inputs, what came of it; which angles were in the volume when cut 537 was made; why the automatic reduction did X at 14:02 | small, if stored as a log of changes |
| Values | the volume as it was at cut 537 | one stored output per past state |

This proposal keeps a time machine for history, for a retention period, and none for values.
A value is kept while something holds it, or once it is saved.
Beyond that, a past value can only be computed again (recompute in a recorded environment is deferred).

## The log

Every change the backend accepts is one event:

| Event | Holds | Appended when |
|---|---|---|
| `submitted` | time, proposal, submitter, and for each record: its ID, its request or read, its output names, label, member, stage | a submission is accepted |
| `finished` | record ID, status, failure message | a record completes, fails, or is cancelled |
| `session-opened`, `session-closed` | session ID, proposal | a session opens or ends |
| `stage-opened` | stage ID, session, spec, blanks | a stage opens |
| `accumulator-opened` | accumulator ID, session, accumulator spec | an accumulator opens |
| `pushed` | accumulator ID, the element: a reference per field | an element is pushed |

D7 with two angles writes this (IDs shortened, JSON abbreviated):

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
- **One event per submission.** A submission of 500 requests is one `submitted` event, so a backend that stops half-way has all of it or none of it.
- **No values.** Output values, what a stage computed from its fixed values, and an accumulator's combined value are not history.
- **Only what applies.** A change is checked before its event is appended. A refused call writes nothing, so the log can always be read again.
- **Plain data.** Events are JSON. Request values already are, since references in `ess.reduce.spec` are plain dicts. The backend applies each event as it reads back from its JSON, so a record holds its values the same way whether it was just made or read from a file: a tuple given as a parameter is a list in the record. A binding gets the values validated by the spec's params model again, in the types the model declares.

## Views

The backend applies each event to its views (`Views.apply` in `views.py`), the same way when it appends the event and when it reads an existing log:

| View | Used by |
|---|---|
| records by ID, with status and failure | `client.wait`, `client.output`, checks of references |
| record IDs by proposal and label | `client.records(label=)`, `latest`, `members`, the trigger loop |
| open sessions, their stages and accumulators | calls through holders |
| each accumulator's elements, and how many leading elements have completed | reads |
| for each unfinished record, where it was pushed as an element | releasing the reads that wait for it |

A view changes only when an event is applied, and how it changes depends on nothing but the events. Queries read the views, never the log.
Some state is not history and is not a view: output values, a stage's staged callable, an accumulator's held value, what waits for what, and the queue of runnable work. The backend keeps it apart from the views, and it is lost when the backend stops.

A backend given a log that already has events applies them, closes the sessions left open, and runs the records left pending:

```python
backend = Backend(datasets, bind, log=Log(Path('log.jsonl')))   # applies the events in the file
```

A pending record runs from scratch, without its stage, since sessions do not survive a restart.
A pending read over an element that did not complete fails.
This backend keeps values in memory, so a pending record whose inputs completed before the restart fails with "no output"; a backend that stores values runs it.
This is what system stories B5 and H2 need from history; H2 also needs every event format to stay readable across versions.

## Records are views

A record stores what the log holds: a request, or a read `Read(spec, accumulator, upto)`.
`record.request` is the full request either way; for a read it is built from the accumulator's first `upto` elements when accessed.

```python
total = client.submit(volume)             # logs: read of a1, upto=1000
total.request.params['counts']            # the 1000 references, built from a1's elements
total.request == plain.request            # D7's check still holds
```

Only provenance, a comparison, or a user reading the request builds the list, at O(k).
Scheduling, checks, and the client never do.

## Accumulators in the backend

An accumulator is opened in the backend, and a push is a backend call that the log records.

- **A push is checked when it is made**, as a request over that element alone would be: the element's fields, readable records of the same proposal, outputs that fit. A read therefore needs no check per element.
- **A read waits until the records of its first k elements have completed.** The accumulator counts how many leading elements have completed, so waiting costs O(1) per event rather than a set of k inputs per read.
- **A read over an element that did not complete fails**, and later reads are refused at submission, naming the element.
- **The accumulator keeps the combined value**, as README.md already says. Each read combines only the elements pushed since the previous read. Reads of one accumulator run one at a time and in order, since each continues from where the previous one stopped.

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

## Measurements

D7's loop (submit an angle, push it, read the volume, cut it), with the in-process backend running one worker thread, `main` against this branch on the same machine:

| Angles | `main`: time | `main`: stored element references | Log: time | Log: stored element references | Log: events |
|---|---|---|---|---|---|
| 500 | 1.55 s | 125,750 | 0.39 s | 500 | 3,505 |
| 1000 | 5.55 s | 501,500 | 0.86 s | 1,000 | 7,005 |
| 2000 | 21.54 s | 2,003,000 | 2.17 s | 2,000 | 14,005 |
| 4000 | | | 3.87 s | 4,000 | 28,005 |
| 8000 | | | 7.19 s | 8,000 | 56,005 |

On `main`, time grows about four times per doubling; with the log, two times.
Most of the remaining time is the JSON round trip of every event, made while the backend's lock is held; it about doubles the time of this backend. With four workers, times are noisier on a shared machine (1.2 s for 1000 angles).
Reads must run in order for this: a read that finds the held value already past it has to start again from the first element, and when reads ran out of order, time still grew 2.5 to 3 times per doubling.

## Values and their lifetime

The principle: a value is kept while something holds it, or once it is saved.
The in-process backend keeps every value in memory, which hides the question.
A hosted backend cannot: D7 would keep 1000 volumes, and every tuning step of B1 would stay on disk.

What could hold a value:

1. a pending request that reads it
2. a record handle in a client, as a dask future holds its value: released when the handle is garbage-collected, or when the client is gone and its lease has run out
3. being the latest record under its label and member
4. having been saved (the provenance sub-design)

How the stories fare with all four:

| Story | What keeps the value |
|---|---|
| S1, S3, S8: compute, then read the output | the notebook's handle (2) |
| B1, S2: tuning steps under a label | the latest under the label (3); earlier steps are released |
| C1, G4: a beam centre used later or elsewhere | the latest under `beam-centre` (3); for next month, save it (4) |
| D2: overnight batch, laptop closed | the latest per member of `night` (3); without a label, only the records survive |
| D7: a read per angle, a cut per read | each read until its cut has run (1); the latest cut (3). The 1000 volumes are not kept |
| B5: kernel dies | what is labelled or saved; all history |

Rule 3 makes labels a small store of current values, one per label and member.
That is the part that touches the question of what belongs in the catalogue, and it can be decided separately from the log.

## Retention

- The log is kept for a retention period.
- An event is kept while a kept event depends on it: a record's submission while a kept record reads it, and an accumulator's elements while a kept read covers them. Otherwise a kept record could not say what it read.
- Publishing writes the provenance, flattened from the log, into the catalogue entry. What is published does not depend on retention.
- A rule that pushes into an accumulator for a whole beamtime adds one event per arrival; retention bounds the total.

## Where the log lives

The design depends on the log being append-only and ordered, not on where it is stored.

- In-process backend: in memory, or a file of JSON lines (implemented). A last line cut short by a crash while it was written is dropped when the file is read; any other line that is not an event is an error.
- A local backend shared by two notebooks (G4): a file appended under a lock (not implemented).
- A hosted backend: a file, a database table used only by appending, or Kafka.

One log per backend; each event names its proposal.
Only the order of pushes within one accumulator and the order of a record's own events matter.

## Compared with the other ways

| | Flat list per read | Chain of totals | Array records | Log |
|---|---|---|---|---|
| A read's record means | the flat request | the spec over the previous total and new elements | the flat request | the flat request |
| Stored per read | k references | the new elements | O(1) | O(1) |
| New reference form in `ess.reduce.spec` | no | no | `Rows` | no |
| Elements from anywhere | yes | yes | only from one stage's records | yes |
| Accumulator specs must not depend on grouping | no | yes | no | no |
| Other gains | none | none | a holder's template stored once | restart from the log; views for labels; a retention rule |

Array records made a stage's records into a durable array and let a read name a range of it.
The log turns this around: the accumulator's pushes are the sequence, and a read names a count.
Holders still live in a session; what outlives the session is the history of their pushes, not their values.

## What changes in README.md

- **Specs, requests, records**: "Records are working state for running experiments and are kept for the medium term" becomes: the history of records is kept for a retention period; an output value is kept while something holds it, or once it is saved. The rest of the paragraph stays.
- **Accumulator**: a push is checked when it is made; the accumulator keeps the combined value, and a binding provides it with `accumulator()`.
- **Guarantees**: "Records are kept for the medium term" changes as above. "Only holders keep memory on a user's behalf" changes with the decision on values.
- **Left to the system**: "how records that share most of their references are stored without repeating them" goes; the log answers it. The log's storage, its retention, and how values are stored and dropped are added.
- **Open question 1**: its second half, how an author declares that grouping does not change the result, is no longer needed by the accumulator. It is still needed for a tree of partial sums over a plain request.

User stories do not change. System story D7's property on storage holds by construction.

## Costs and risks

- **Views are code.** Every query needs a view that is kept in step with the log. The log must be the only way state changes, or views and log drift apart.
- **Event formats must stay readable** for as long as the log is kept, and so must the rule that builds a read's request. Views may change freely, because they are rebuilt from the log.
- **A push is a backend call.** Over a network, one call per push; batching pushes is possible later.
- **Reads of one accumulator run one at a time.** A fold is sequential anyway. A plain request over many elements may still be computed as a tree of partial sums.
- **A record of a read is bound to its accumulator's elements** to build its request (decision 5).
- **The JSON round trip of every event** costs time; in this backend about a factor of two.
- **Values need their own lifetime rules**, and rule 2 needs handles with leases over the network. This is the hardest part of the system that remains.

## Prior art

- **Event sourcing**: state kept as a sequence of events; current state and queries are views built by applying them, and rebuilt by replaying the log. Separating the views that answer queries from the log is often called CQRS.
- **Kafka**: the log as the storage abstraction. A compacted topic keeps the latest value per key, which is what `latest(label, member)` is. A consumer's committed offset is what the trigger loop needs to know what it has handled.
- **Datomic**: an immutable log of facts with queries as of any past time; a time machine for history, which is the half taken here.
- **Temporal**: a workflow's event history, replayed to rebuild its state. It caps the history's size and has long-running workflows start again with a fresh one, a warning about logs that grow without bound.
- **AiiDA**: the provenance graph stored as a database of nodes and links, every process recorded. It later added a way to run without storing provenance, because users wanted to explore without leaving a trace.
- **dask.distributed**: a future holds its value on the cluster while a client holds the future; this is rule 2 for values.

## Decisions

**1. Adopt the log.** Records are views of an append-only log of accepted changes; a read of an accumulator is logged as the accumulator and a count. The chain of totals and array records are dropped.

> Simon:

**2. Which rules keep a value.** Recommendation: all four above. Rule 3 can be decided separately if it belongs with the catalogue question.

> Simon:

**3. Pushes are checked when made, and an accumulator's reads run in order.** Both follow from the log; the first changes when a misfitting push is refused.

> Simon:

**4. Retention keeps an event while a kept event depends on it.**

> Simon:

**5. How a record of a read builds its request.**
The record keeps what the log holds, `Read(spec, accumulator, upto)`. `record.request` needs the accumulator's elements, which live in the backend.
Now the backend binds each such record to the elements when it makes the record; a copy made from the record's plain data, say sent over HTTP or pickled, has no elements and raises when its request is read.
The alternative is `client.request(record)` for every record, with `record.request` gone: records are then plain data without exception, at the cost of changing every `record.request` in README.md and the stories (about 35 places).
Recommendation: keep `record.request`, and let a client bind the records it receives to itself when a transport exists. Records are views now, and the request is the field users read most.

> Simon:

**6. Where this goes when adopted.** Recommendation: this document becomes the first part of a system document, `docs/developer/system.md`, since it describes how the system stores history, not the API. README.md changes as listed above.

> Simon:

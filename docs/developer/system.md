# ESS data-reduction framework: the system

**Status: the design of how the system keeps history and values. The in-process backend implements the history and the holders; which values are dropped, retention, and saving are not implemented, since that backend keeps every value in memory.**

[README.md](README.md) describes the API: what users and workflow authors write, and what they can rely on.
This document describes how the system provides what the API promises about records and values.
Other parts of the system, such as the store of values, a hosted backend, and where sessions run, get sections here when they are designed.
[ADR 0001](adr/0001-history-as-an-event-log.md) records why history and values are kept apart.

Terms from README.md used here: a *spec*, and its *binding*, the code that computes it; a *record*; a *session* and the *holders* in it, stages and accumulators.
A *read* of an accumulator is what `client.submit(accumulator)` makes: a record of the accumulator spec over the elements pushed so far.

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
| each accumulator's elements, and how many of them, from the first on, have completed | reads (see Accumulators) |
| for each unfinished record, the accumulators it was pushed into | starting or failing the reads that wait for it |

A view changes only when an event is applied, and how it changes depends on nothing but the events.
Queries read the views, never the log.
What is not history is not a view either: output values, what a stage computed from its fixed values, an accumulator's combined value, which records wait for which, and the queue of work ready to run.
The backend keeps it apart from the views, and it is lost when the backend stops.

### Records

A record stores what the log holds: a request, or a read `Read(spec, accumulator, upto)`.
`record.request` is the full request either way. For a read, the backend builds it from the accumulator's first `upto` elements when someone accesses it:

```python
total = client.submit(volume)        # the log holds: a read of volume, upto=1000
total.request.params['counts']       # the 1000 references, built now
```

Only provenance, a comparison, or code that reads the request builds this list; scheduling and checks work with `upto` alone.
To build the list, a record of a read keeps a reference to its accumulator's elements in the backend.
A copy made from the record's plain data alone, such as one sent over a network, cannot build it; a client that receives records over a network would have to fetch the elements itself.

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

This section follows one accumulator through three pushes and two reads:

```python
with client.session() as session:
    volume = session.accumulator(SUM.of(Counts))
    volume.push(a1)                    # a1, a2, a3: records of ANGLE, possibly still pending
    volume.push(a2)
    first = client.submit(volume)      # a read over a1 and a2
    volume.push(a3)
    second = client.submit(volume)     # a read over a1, a2, and a3
```

`volume` in the notebook is a handle.
The accumulator itself lives in the backend: its elements, and the combined value it keeps.
Each push is a call to the backend and a `pushed` event in the log.
Each read is a `submitted` event holding `Read(spec, accumulator, upto)`, where `upto` is the number of elements pushed before the read: 2 for `first`, 3 for `second`.

**Checking a push.** The backend checks a push as it would check a request over that one element: the element has the fields of the accumulator spec's element model, its references name records of the same proposal that have not failed or been cancelled, and their outputs fit the fields. A push that does not fit is refused at the push, so a read need not check its elements again.

**When a read runs.** A pushed record may still be pending, and a read runs once every record it covers has completed: `first` waits for a1 and a2, `second` for a1, a2, and a3.
For each accumulator the backend keeps one number, how many elements, from the first on, have completed, and starts a read when that number reaches the read's `upto`.
The bookkeeping for a read therefore does not grow with the number of elements it covers.

**When an element fails.** If a2 fails or is cancelled, `first` and `second` fail, since both cover it.
Any later read would cover a2 too, so it is refused at submission, naming the element.

**The combined value.** The backend keeps the combined value of the elements up to the last read.
`second` adds a3 to the value that `first` computed, instead of combining a1, a2, and a3 again.
For this, the reads of one accumulator run one at a time, in the order they were submitted.
If combining fails part-way through a read, the backend drops the combined value, and the next read starts again from the first element.

**Where the combining code comes from.** The binding of an accumulator spec may be a plain function over lists, which combines all elements in one call.
To keep a combined value, the backend needs a binding that can also make element accumulators: objects that take one element at a time and give the combined value so far, as `sciline.Accumulator` does for one key.

```python
held = binding.accumulator()              # nothing pushed yet
held.push({'counts': counts_1})           # values, not references
held.push({'counts': counts_2})
held.value                                # {'counts': counts_1 + counts_2}
```

`combine(operator.add)` is such a binding; the stories bind `SUM.of(Counts)` to it.
It combines the elements in the same order for a plain request, so a read and the plain request over the same elements give the same value.
With a binding that is only a function, every read calls the function with all the elements pushed so far.

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

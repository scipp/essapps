# ADR 0001: Keep the backend's history as an event log, apart from values

- Status: accepted, amended 2026-10-02
- Deciders: Simon
- Date: 2026-09-30

## Context

A record says what ran: a spec, every parameter value, its inputs by reference, its status, and its outputs.
Records were self-contained, kept for the medium term together with their outputs, never changed, and queryable by label and time.

Story D7 takes a snapshot of an accumulator after each of 1000 pushes, and each snapshot's record listed every element pushed so far.
The records together held the element list at every past state, in full, and every layer handled all of it: the client, the checks, the scheduler, and provenance.
D7 took 5.6 s for 1000 angles and 21.5 s for 2000, with 501,500 and 2,003,000 stored references.

The records had become a time machine for two different things:

- **History**: what ran, with which inputs, and what came of it. It is small if stored as the changes that happened.
- **Values**: every output of every past state. It costs one stored output per state.

Keeping outputs for the medium term also made the record store a catalogue of results next to SciCat, which is a non-goal.

## Decision

Keep a time machine for history, for a retention period, and none for values.

- **The backend's history is an append-only log of what ran**: a submission, a record that finished, a push into an accumulator. A change is checked before its event is appended, and a submission is one event. Sessions and holders are live state, not history.
- **Records, labels, and the elements of accumulators are views** built by applying the events in order. A backend that reads its log again rebuilds them.
- **A snapshot of an accumulator is logged as the accumulator and a count** of the elements it covers. Its record says that, not the list of elements; its value is the value of the accumulator's spec over those elements, and provenance reads the elements from the accumulator's pushes.
- **History is kept for a retention period**, like a garbage collector whose roots are the events younger than that period: an older event is kept while a kept event depends on it.
- **Values are not history.** An output is kept while a pending request reads it, while a record handle in a client holds it, while a holder in a session holds it, or once it is saved. Labels name records and keep no values.

## Alternatives considered

- **A flat list per snapshot, with storage that shares repeated references.** Keeps the meaning of a snapshot, but every layer above the store still handles the full list: quadratic in the number of snapshots.
- **A chain of totals**: each snapshot is the accumulator's spec over the previous snapshot and the new elements. Linear, but the record no longer means the sum of its elements, and every spec an accumulator holds must then output its element's fields and give the same result however the elements are grouped.
- **Array records**: a holder's records share one stored template, and a snapshot names a range of them with a new reference form in `ess.reduce.spec`. Linear and keeps the meaning, but adds a second level of identity to every layer, and works only for elements from one stage.
- **A record store that keeps outputs for the medium term.** The time machine for values, and a second catalogue.

## Consequences

- D7 is linear: 0.86 s for 1000 angles, 2.2 s for 2000, and one stored reference per element (in-process backend, one worker).
- A backend can restart from its log: it runs the records left pending, without their stages, since sessions do not survive a restart. This is what system stories B5 and H2 need from history.
- Accumulators move into the backend. A push takes only a finished record and combines it when it is made, so a snapshot completes at submission with the combined value. A driver waits for many records with `client.as_completed`.
- Event formats must stay readable for as long as the log is kept. Views may change freely.
- A snapshot is not a request: `record.request` exists only for requests, and what a snapshot read comes from the backend.
- Keeping values while a client holds a record handle needs leases in a hosted backend, so that a client that disappears releases them.
- A value that must outlive its client, such as a beam centre for tomorrow's batch, is saved. A forwarder, a holder of the last value pushed into it (as in sciline), joins the holders when a story needs one.

## Amendment 2026-10-02

- The consequence on restarting from the log cites system stories B5 and H2. Only H2, a backend upgrade with runs in flight, restarts the backend. B5 is a notebook kernel that dies; its client ends, and the backend keeps running.
- History is not kept for a retention period. Records are the proposal's history: they are kept as long as the proposal, and dropped with it as a whole. The trigger loop knows which datasets a rule has handled only from the records under the rule's label, so a retention that dropped old records would make it reduce those datasets again.
- What keeps an output value, in the decision's last point and in the consequences on record handles, sessions, and holders, is replaced by [ADR 0002](0002-the-client-is-the-lifetime.md): the client that made a record keeps its values until it releases them or ends, and a pending request keeps what it reads until it has run.

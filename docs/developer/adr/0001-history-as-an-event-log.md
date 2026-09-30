# ADR 0001: Keep the backend's history as an event log, apart from values

- Status: accepted
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

- **The backend's history is an append-only log of the changes it accepted**: a submission, a record that finished, a session or holder that opened or closed, a push. A change is checked before its event is appended, and a submission is one event.
- **Records, labels, and the state of holders are views** built by applying the events in order. A backend that reads its log again rebuilds them.
- **A snapshot of an accumulator is logged as the accumulator and a count** of the elements it covers. Its record still means the accumulator spec over those elements, and that request is built when someone asks for it.
- **History is kept for a retention period**, like a garbage collector whose roots are the events younger than that period: an older event is kept while a kept event depends on it.
- **Values are not history.** An output is kept while a pending request reads it, while a record handle in a client holds it, while a holder in a session holds it, or once it is saved. Labels name records and keep no values.

## Alternatives considered

- **A flat list per snapshot, with storage that shares repeated references.** Keeps the meaning of a snapshot, but every layer above the store still handles the full list: quadratic in the number of snapshots.
- **A chain of totals**: each snapshot is the accumulator spec over the previous snapshot and the new elements. Linear, but the record no longer means the sum of its elements, and every accumulator spec must then give the same result however the elements are grouped.
- **Array records**: a holder's records share one stored template, and a snapshot names a range of them with a new reference form in `ess.reduce.spec`. Linear and keeps the meaning, but adds a second level of identity to every layer, and works only for elements from one stage.
- **A record store that keeps outputs for the medium term.** The time machine for values, and a second catalogue.

## Consequences

- D7 is linear: 0.86 s for 1000 angles, 2.2 s for 2000, and one stored reference per element (in-process backend, one worker).
- A backend can restart from its log: it closes the sessions left open and runs the records left pending, without their stages. This is what system stories B5 and H2 need from history.
- Accumulators move into the backend. A push takes only a finished record and combines it when it is made, so a snapshot completes at submission with the combined value. A driver waits for many records with `client.as_completed`.
- Event formats must stay readable for as long as the log is kept, and so must the rule that builds a snapshot's request. Views may change freely.
- A snapshot's record builds its request from its accumulator's elements; a copy made from the record's plain data alone cannot.
- Keeping values while a client holds a record handle needs leases in a hosted backend, so that a client that disappears releases them.
- A value that must outlive its client, such as a beam centre for tomorrow's batch, is saved. A forwarder, a holder of the last value pushed into it (as in sciline), joins the holders when a story needs one.
- Whether "request" is still the abstraction a record needs is left open.

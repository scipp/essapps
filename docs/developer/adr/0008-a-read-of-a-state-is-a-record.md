# ADR 0008: A request that reads an accumulator reads a record of the state's plain request

- Status: accepted
- Deciders: Simon
- Date: 2026-10-06, rewritten 2026-10-07

## Context

A request reads an accumulator through `acc.ref(output)`.
The record of such a request could hold the reference pinned to the current state, as a third kind of reference next to outputs of records and datasets:

```python
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})
cut.request.params   # {'data': AccumulatorRef(accumulator='a1', output='iofq', upto=2)}
```

Such a reference names something that is not a record: an accumulator, which lives only as long as its client, and a state, which the next push replaces.
Everything that follows records would have to handle it apart:

- Provenance would expand each `(accumulator, upto)` into the plain request over the rows of the first `upto` pushes.
- Running a record again, as the replay of every record after each story test does (`replay` in `tests/stories/conftest.py`), could not just submit its request: that would read the same pinned state, or fail once the accumulator is gone.
- A call through a stage makes the record of the plain request and does not name the stage (caching), but a read of an accumulator would name the accumulator. The two templates the backend keeps would be treated differently, although both only save computation.

The arrival symmetry already says what a state is: state n gives the outputs of the plain request over the rows of the first n pushes ([README](../README.md), Symmetries; [ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

A record of a state differs from other records in one way: its value is part of the held state, which the next push changes in place.
So no client may keep it, and no later request may read it.
These look like rules about one kind of record, but they are rules about values, which [ADR 0002](0002-the-client-is-the-lifetime.md) and [ADR 0005](0005-nothing-is-written-unless-persisted.md) state for every record.

## Decision

**A request that reads an accumulator reads a record of the plain request of the state it pins.**
When a request that references an accumulator is submitted, the backend pins the state, and makes a record of that state's plain request for the submission, logged with the submission.
The reference in the record of the request names that record:

```python
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})
cut.request.params   # {'data': OutputRef(record='r7', output='iofq')}
# record r7, logged with the submission, with dataset names resolved:
#   IOFQ_MULTI({**shared, 'sample_runs': [{'run': 'uuid:run-611'}], 'can_runs': [{'run': 'uuid:run-614'}]})
```

- **Records hold only outputs of records and datasets.** `acc.ref(output)` is what a caller writes; the backend replaces it at submission. It names no state.
- **The record of a state is computed from the held state**, as a call through a stage is computed from what the stage holds. Its outputs are the outputs of the state, not a copy, so a workflow that reads them must neither modify them nor return them or a view of them, as for every input ([README](../README.md), Guarantees).
- **Each submission makes its own record of the state**, also when an earlier submission pinned the same state. The accumulator computes the outputs of one state once.
- **Its edges are provenance, not reads.** It reads the held state, not the values of the records its rows reference, so it does not matter that the client released those records after pushing them. The record that `freeze` returns is the same ([ADR 0003](0003-accumulators-add-in-place.md)).
- **It is a record like any other, with no rules of its own.** No client asked for it, so no client keeps it. Its value is kept for the requests of its submission until their workflows have returned ([ADR 0002](0002-the-client-is-the-lifetime.md), the keepers). The general rules about values then give what holds for it:

| What holds for the record of a state | Follows from |
|---|---|
| no client keeps its outputs; they are dropped once its readers have run, and the next push waits until then | the keepers: no client asked for it, and its readers keep it until they have run |
| only the submission that made it reads it, since a later reader could wait for a later push that waits for it | a submission reads only values kept for it; this value is kept for no later submission |
| `client.output` of it raises; the accumulator is read instead | `client.output` reads only values kept for the caller |
| nothing writes it | nothing is written unless persisted, and only records a client asks for can be persisted ([ADR 0005](0005-nothing-is-written-unless-persisted.md)) |
| a pending one fails at a restart | a record that reads a held state fails at a restart, since the held state is gone ([ADR 0005](0005-nothing-is-written-unless-persisted.md)) |

**A read of the state's outputs without a request**, `client.output(acc)`, makes no record.

## Alternatives considered

- **References to states in records** (the form in Context). It needs no record for a read. It costs a third reference form in `ess.spec`, a provenance that expands states from the log, and a rewrite of each state before a record can be run again. History stores the record of a state as the accumulator and its number of pushes ([ADR 0004](0004-history-is-append-only-lists.md)), so the record costs no more history than such a reference.
- **Snapshots as records**: `client.submit(accumulator)` makes a record whose output is the value so far ([ADR 0003](0003-accumulators-add-in-place.md), Alternatives). Such a record is not a request, and a client could keep a value that the next push changes. The record of a state is the plain request, and no client keeps it.
- **Rules of its own for the record of a state**: no client keeps its outputs, only its own submission reads it, `client.output` of it copies, nothing writes it, and a pending one is computed again after a restart. They restate rules about values as rules about one kind of record, and each needs its own check.
- **One record per state, shared by every submission that reads it.** No later submission can reference a record of a state, so sharing would bring back no circular wait. It saves history, not memory, and needs a lookup of the record that an earlier submission made.
- **Computing a pending record of a state again as its plain request after a restart.** It would reduce every row again, with the memory of the plain request: for a spectroscopy volume, hundreds of GB.

## Consequences

- A submission that reads an accumulator makes one more record. `client.records()` lists it with the proposal's records like any other, and provenance includes it.
- History grows by a constant amount per read ([ADR 0004](0004-history-is-append-only-lists.md)).
- Provenance is a walk over records and datasets. The rows of a record of a state come from the accumulator's pushes.
- Running a record again submits its request, and the requests of the records it reads whose outputs are gone.
- Cancelling every reader of a pending record of a state cancels that record too, since nothing else keeps it, and frees the next push once the readers' workflows have returned.
- A user can cancel a record of a state that another client of the proposal submitted, as any record of the proposal; its readers then fail.
- At a restart, a pending record of a state fails, and the requests that read it fail with it.
- A state that its plain request would refuse is refused at the submission that reads it; the record of that request is never made.
- On the service, the outputs of a record of a state are part of the held state; how the service holds held states is open ([ADR 0005](0005-nothing-is-written-unless-persisted.md), Open).

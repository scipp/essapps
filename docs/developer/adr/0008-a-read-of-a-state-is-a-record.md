# ADR 0008: A request that reads an accumulator reads a record of the state's plain request

- Status: accepted
- Deciders: Simon
- Date: 2026-10-06

## Context

A request reads an accumulator through `acc.ref(output)`.
Until now, the backend pinned the reference to the current state, and the record of the request held it as a third kind of reference, next to outputs of records and datasets:

```python
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})
cut.request.params   # {'data': AccumulatorRef(accumulator='a1', output='iofq', upto=2)}
```

Such a reference names something that is not a record: an accumulator, which lives only as long as its client, and a state, which the next push replaces.
Everything that follows records had to handle it apart:

- Provenance expanded each `(accumulator, upto)` into the plain request over the rows of the first `upto` pushes, from the accumulator's template and pushes kept in history (`Provenance.accumulated`).
- Running a record again, as the replay of every record after each story test does (`replay` in `tests/stories/conftest.py`), could not just submit its request: that would read the same pinned state, or fail once the accumulator is gone. It needed a rewrite of each state into a record of its plain request first.
- At a restart, a pending request that read an accumulator failed, since the accumulator is gone.
- A call through a stage makes the record of the plain request and does not name the stage (caching), but a read of an accumulator named the accumulator. The two templates the backend keeps were treated differently, although both only save computation.

The arrival symmetry already says what a state is: state n gives the outputs of the plain request over the rows of the first n pushes ([README](../README.md), Symmetries; [ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

## Decision

**A request that reads an accumulator reads a record of the plain request of the state it pins.**
When a request that references an accumulator is submitted, the backend pins the state, and makes one record of that state's plain request for the submission, logged with the submission.
The reference in the record of the request names that record:

```python
cut = client.submit(EXPORT, {'data': acc.ref('iofq')})
cut.request.params   # {'data': OutputRef(record='r7', output='iofq')}
# record r7, logged with the submission, with dataset names resolved:
#   IOFQ_MULTI({**shared, 'sample_runs': [{'run': 'uuid:run-611'}], 'can_runs': [{'run': 'uuid:run-614'}]})
```

- **Records hold only outputs of records and datasets.** `acc.ref(output)` is what a caller writes; the backend replaces it at submission. It names no state.
- **The record of a state is computed from the held state**, as a call through a stage is computed from what the stage holds. Its outputs are the outputs of the state, not a copy, so a workflow that reads them must neither modify them nor return them or a view of them, as for every input ([README](../README.md), Guarantees).
- **The record of a state has lifetime rules of its own**, which a record made through a stage does not have, since its outputs are part of the held state:
  - No client keeps its outputs. They are kept while the requests that read them have yet to run, as for a record that its client has released, and then dropped. The next push waits until then.
  - Only the submission that made it reads it. A later request, a stage, or a template or row of an accumulator that references it is refused. A stage or an accumulator would keep an output that is part of the held state, and see the next push change it. A later request would hold back the next push, and if it also waited for a later state of the same accumulator, the two would wait for each other. Once the outputs are dropped, a reference is refused as for any released record.
  - `client.output` of such a record returns a copy, since the held state may change after it.
  - On the service, nothing writes its outputs (below).

  What the record says is the same as for any record: its request, which gives its outputs.
- **A read of the state's outputs without a request**, `client.output(acc)`, makes no record.
- **The service holds the outputs of the record of a state as it holds the held state, and nothing writes them.** This is an exception to [ADR 0005](0005-nothing-is-written-unless-persisted.md). A driver that reads after every push, as story D7 does, would otherwise write one spectroscopy volume of hundreds of GB per read. If the outputs are lost, the record is computed again as its plain request.

## Alternatives considered

- **References to states in records** (the form in Context). It needs no record for a read, and history holds a count instead of the rows of each state read. It costs a third reference form in `ess.spec`, a provenance that expands states from the log, a rewrite of each state before a record can be run again, and a restart that fails every pending request that read an accumulator. The record of a state costs one more record per submission that reads an accumulator, history that grows with the square of the number of pushes (Consequences), and the lifetime rules above.
- **Snapshots as records**: `client.submit(accumulator)` makes a record whose output is the value so far ([ADR 0003](0003-accumulators-add-in-place.md), Alternatives). Such a record is not a request, and a client could keep a value that the next push changes. The record of a state is the plain request, and only its own submission reads it.
- **One record per state, shared by every submission that reads it.** Only the submission that made the record of a state may read it, since a later reader could wait for a later push that waits for it. So each submission needs a record of its own. Both records compute from one state, whose outputs the accumulator computes once.

## Consequences

- A submission that reads an accumulator makes one more record. `client.records()` lists it with the proposal's records like any other.
- A record of a state lists every row of the state. A driver that reads after every push, as story D7 does, makes records that hold 1, 2, ..., n rows, so history grows with the square of the number of pushes: for D7's 300 pushes, 45,000 rows of about a hundred bytes each. An application or an HTTP transport that lists the records pays the same. History may store such a record as the accumulator's template and its number of pushes, and expand it when read; that is a matter of storage, not of what the record says.
- Each submission that reads an accumulator logs a request of n rows under the backend's lock.
- Provenance is a walk over records and datasets.
- Running a record again submits its request, and the requests of the records it reads whose outputs are gone.
- Cancelling the readers of a pending record of a state does not free the next push at once: the record still computes the outputs once, unless it is cancelled too.
- A user can cancel a record of a state that another client of the proposal submitted, as any record of the proposal; its readers then fail.
- At a restart, a pending record of a state has no accumulator to compute from, so it runs as its plain request on an ordinary worker, at the cost of the per-run work of every row and with the memory of the plain request: for a spectroscopy volume, hundreds of GB. A memory bound per request is open ([ADR 0005](0005-nothing-is-written-unless-persisted.md), Open). The requests that read it run after it. Several submissions that read one state each made a record of it, so a restart computes that state once per such record.
- A restarted backend keeps no output values, so a pending record of a state runs after a restart only if its rows and template reference datasets alone; one whose rows reference outputs of records fails, as any record does whose inputs' values are gone.
- A state that its plain request would refuse is refused at the submission that reads it; the record of that request is never made.
- The service holds the outputs of records of states next to its files ([ADR 0005](0005-nothing-is-written-unless-persisted.md)); how much it may hold is part of how the service holds held states, which is open there.

# ADR 0002: A value lives while something keeps it, and the client keeps what it asks for

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-07

## Context

History holds records, not values ([ADR 0004](0004-history-is-append-only-lists.md)).
A record names its inputs, and it never changes.
The output values a record names take memory, so something must decide when they go.

A review of the user stories against a rule under which record handles keep values found four problems:

- **Which record handle keeps a value is undefined.** `client.records()`, `client.latest()`, and provenance return equal copies of one record. Jupyter's output cache (`Out[n]`, `_`) keeps the last expression of every cell alive.
- **The README's own example lost what it promised.** It labelled a loop "so that another notebook finds the curves" and dropped every record, so under the handle rule the curves were gone once computed.
- **A session lived exactly as long as its client in every story.** A `with` block cannot span notebook cells, so a notebook that tunes across cells opened a session and never ended it. A hosted backend must end a session when its client is gone anyway.
- **The deployments differ in what can leak.**
  - Interactive work runs in-process (`local()`) on a VISA desktop or a laptop. There the process bounds everything, and the VM's end cleans up the machine.
  - The hosted backend serves batch and automatic reduction, and large work fanned out to a cluster. They run for hours or days, so leaks and peak memory matter there.

Batch and automatic reduction run unattended ([users](../../requirements/users.md)).
The trigger loop submits through one client and never releases anything.
A batch client that closes would take its values with it.
Both write what they keep for later to the store instead ([ADR 0005](0005-nothing-is-written-unless-persisted.md)), so the rule for values must cover the store as one more keeper.

A pending record whose values nothing keeps is wasted work: its values would be dropped the moment they were computed.

## Decision

**A value exists while something keeps it.**
Records form a graph: a record's edges are its inputs, outputs of other records and datasets, and they never change.
A value hangs on a record while something keeps it.
Releasing drops a value, never a record or an edge.
A value that is gone does not come back: running the same request again makes a new record with a value of its own.

| Keeper | Keeps | Until |
|---|---|---|
| the client that asked for the record (`submit`, `compute`, `freeze`) without `persist=` | the record's values | it releases the record or ends |
| a pending request | the values it reads | it has finished and its workflow has returned |
| a pending read of an accumulator: the record of a state, a `freeze`, a `client.output` | the state it pinned, and the pushes up to it | it has finished, or `client.output` has copied |
| a value that is part of a held state, such as an output of the record of a state | the held state at that state: the next push is not added | the value is dropped |
| a push | the values its rows reference | it is added or dropped |
| a stage | the values its template references | it is released |
| an accumulator's template | the values it references | the held state has opened |
| a held state that keeps its rows | the values they reference | the held state is dropped |
| a persist request | the values it names | they are written, or the write has failed |
| the store | written values | the proposal's history is dropped, or a value is dropped earlier |

Nothing else keeps a value: not a record object, not a reference, not a label.
Keeping is the default for what a client asks for.
[ADR 0003](0003-accumulators-add-in-place.md) gives the keepers of an accumulator, [ADR 0005](0005-nothing-is-written-unless-persisted.md) the persist request and the store.

**A submission reads only values kept for it:** values its own client keeps, persisted values ([ADR 0005](0005-nothing-is-written-unless-persisted.md)), datasets, and the current state of its own client's accumulators.
A request that references any other value is refused at submission, whether that record is pending or completed.
Stages, accumulators, and pushes check what their templates and rows reference in the same way.
So whether a submission is accepted depends only on what its client keeps, not on how far other work has come.
Another client, in the same process or not, references a value only once it is persisted.

**`client.output`** reads values its own client keeps, persisted values, and accumulators ([ADR 0003](0003-accumulators-add-in-place.md)).
Reading a value that nothing keeps for the caller raises; the record stays.

**Work that nothing keeps is cancelled.**
When nothing keeps a pending record's values any more, the record finishes as cancelled.
Cancelling a request lets go of what it reads, so this passes along a chain.
A workflow that is already running cannot be interrupted: its record is cancelled at once, and the workflow runs to its end while its outputs are dropped.
Cancelling frees memory, not CPU.

**Stages and accumulators belong to the client**, and are released the same way.
A stage keeps the values its template references until it is released, since what it computed from them stays in memory with it.
What a stage computed is a cache that the backend may drop; its next call computes it again and makes the same record.

**A client ends** at `client.close()` or at the end of `with client:`.
In-process, a client made with `local()` owns its backend: closing the client closes the backend, which first waits until the pending records with a persist request are written, so that a script that persists and exits keeps what it asked for.
A client that is never closed ends with its process; on the service, its lease runs out ([ADR 0005](0005-nothing-is-written-unless-persisted.md)).
A later call of an ended client raises `ClientEnded`.
In the backend, a client is one entry, kept in memory, not in history.

```python
with local(proposal='p1', datasets=source, bind=bind) as client:
    centre = client.compute(BEAM_CENTRE, {'run': dataset(run=60330)})
    result = client.submit(IOFQ, {'run': dataset(run=60339), 'beam_centre': centre.ref('centre')})
    client.release(centre)          # the pending request still reads the centre
    client.output(centre, 'centre') # raises: nothing keeps it for this client
    client.submit(IOFQ, {'run': dataset(run=60340), 'beam_centre': centre.ref('centre')})
                                    # refused, although the pending request still reads it
# the block's end releases everything the client keeps; work that nothing else keeps is cancelled
```

The same rules hold in the user's process and on the service.
How the service bounds what a client keeps, and notices a client that vanished, is in [ADR 0005](0005-nothing-is-written-unless-persisted.md).

## Alternatives considered

- **Record handles keep values, as dask futures do.** This frees a loop that waits for each result by itself. But the stories hold many equal copies of a record. D2's dict of records, kept for the morning check, would keep every value, and so would `Out[n]`.
- **Keep only on request (`keep=True` at submission).** Nothing leaks by default. But the flag must be given at submission: a value dropped when its request finishes cannot be kept afterwards without a race. Almost every interactive call would need it. Forgetting it shows only after the work is done, when a read or a reference is refused, and the result must be computed again. Forgetting `release` under keep-by-default costs memory instead, which the process bounds. The clients that must not keep, the trigger loop and batch applications, are framework code, and they hand their values to the store at submission ([ADR 0005](0005-nothing-is-written-unless-persisted.md)).
- **Releasing and ending stop no work.** Pending work runs on after its client lets go. Its values are dropped the moment they are computed, unless a pending reader keeps them, and then that reader keeps the work alive anyway. The case this was meant for, a driver that releases an accumulator right after its last read, holds without it: the pinned reads keep their states ([ADR 0003](0003-accumulators-add-in-place.md)).
- **A submission reads any value that something keeps**, such as a released record that a pending request still reads. Whether the same code is accepted then depends on whether that record has completed and been dropped yet.
- **Sessions reopened by name from a new kernel.** They are lost at a backend restart and cannot work in-process, so every program still needs a path for "it is gone". They need names, an expiry, a listing, and a rule for two kernels attached at once. dask-gateway clusters left running after kernel restarts, and Ray's detached actors that must be killed by hand, show the leak.
- **Nested scopes (`with client.scope() as s:`).** At its exit a scope must let pending requests run, so the exit is `release` of everything made in it. A notebook cannot hold a `with` block across cells. Scopes add two errors: reading a value after its block, and submitting through the wrong scope. An implicit current scope does not reach the thread in which `as_completed` consumes a generator. No story needs nesting.
- **Values as a cache over the log**, dropped under memory pressure and recomputed from their records when read. A read could take as long as the first computation. A cut of an earlier state of an accumulator could be recomputed only by reducing again every row pushed before it.

## Consequences

- `Session`, `client.session()`, `where=`, record leases, and the word "holder" are gone. Where a request runs is the system's decision.
- A loop that keeps every result keeps every value until it releases them. In-process this behaves like a list of arrays that a notebook never clears.
- A chain may release its intermediate records once the requests that read them are submitted: those requests keep them. Releasing the last request of the chain cancels the whole chain, unless something else keeps a part of it.
- A cancelled workflow that is already running still uses its worker until it returns.
- Two clients share values only through the store, also two clients in one process.
- A kernel restart ends the process, and with it the backend and everything it keeps. Continuing a reduction after the user's process crashed is a non-goal ([requirements](../../requirements/README.md)).
- A backend restarts only on a log file, which in-process only the tests do ([ADR 0004](0004-history-is-append-only-lists.md)). After a restart, no client keeps anything; [ADR 0005](0005-nothing-is-written-unless-persisted.md) (Restart) says what still runs.
- A stage takes calls for as long as it lives, also after the client released the records its template references.

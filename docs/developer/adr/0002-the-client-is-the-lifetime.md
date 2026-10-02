# ADR 0002: The client is the one lifetime of values, stages, and accumulators

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02

## Context

[ADR 0001](0001-history-as-an-event-log.md) keeps values out of history.
It listed four things that keep an output value:

- a pending request that reads it;
- a record handle in a client, released when garbage-collected or when its lease runs out, like a dask future;
- a holder (a stage or an accumulator) in a session, a `with client.session()` block;
- saving.

A review of the user stories against this rule found four problems:

- **Which record handle keeps a value is undefined.** `client.records()`, `client.latest()`, and provenance return equal copies of one record. Jupyter's output cache (`Out[n]`, `_`) keeps the last expression of every cell alive.
- **The README's own example lost what it promised.** It labelled a loop "so that another notebook finds the curves" and dropped every record, so under the handle rule the curves were gone once computed.
- **A session lived exactly as long as its client in every story.** A `with` block cannot span notebook cells, so a notebook that tunes across cells opened a session and never ended it. A hosted backend must end a session when its client is gone anyway.
- **The deployments differ in what can leak.**
  - Interactive work runs in-process (`local()`) on a VISA desktop or a laptop. There the process bounds everything, and the VM's end cleans up the machine.
  - The hosted backend serves batch and automatic reduction, and large work fanned out to a cluster. They run for hours or days, so leaks and peak memory matter there.

## Decision

The client is the one lifetime:

- **A client keeps the output values of the records it makes** until `client.release(...)` or until it ends. A pending request keeps the values it reads until it has run. Nothing else keeps a value: not a record object, not a reference, not a label. Keeping is the default.
- **Stages and accumulators belong to the client**, and are released the same way. What a stage computed is a cache that the backend may drop; its next call computes it again and makes the same record.
- **A client ends** at `client.close()` or at the end of `with client:`. Ending and releasing stop no work. A later call of an ended client raises `ClientEnded`. In-process, a client that is never closed ends with its process.
- **In the backend, a client is one entry.** Like everything that keeps values, it is live state, not history.

```python
with local(proposal='p1', datasets=source, bind=bind) as client:
    tune = client.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
    for bins in (50, 100, 200):
        result = client.compute(tune, {'bins': bins})
    client.release(result)          # optional, as `del` of a large array is
# the block's end releases everything the client keeps; pending work still runs
```

Batch and automatic reduction are to keep nothing.
A submission with `save=` will have the worker that computed a record write its outputs, then drop them, so peak memory is the requests running at once.
This is designed together with saving.

## Alternatives considered

- **Record handles keep values, as dask futures do.** This frees a loop that waits for each result by itself. But the stories hold many equal copies of a record. D2's dict of 1000 records, kept for the morning check, would keep all 1000 values, and so would `Out[n]`.
- **Keep only on request (`keep=True` at submission).** Nothing leaks by default. But the flag must be given at submission: a value dropped when its request finishes cannot be kept afterwards without a race. Almost every interactive call would need it. Forgetting it shows only after the work is done, when a read or a reference is refused, and the result must be computed again. Forgetting `release` under keep-by-default costs memory instead, which the process or a cap per client bounds. Changing the default later is one flag.
- **Sessions reopened by name from a new kernel.** They are lost at a backend restart and cannot work in-process, so every program still needs a path for "it is gone". They need names, an expiry, a listing, and a rule for two kernels attached at once. dask-gateway clusters left running after kernel restarts, and Ray's detached actors that must be killed by hand, show the leak.
- **Nested scopes (`with client.scope() as s:`).** At its exit a scope must let pending requests run, so the exit is `release` of everything made in it. A notebook cannot hold a `with` block across cells. Scopes add two errors: reading a value after its block, and submitting through the wrong scope. An implicit current scope does not reach the thread in which `as_completed` consumes a generator. No story needs nesting.
- **Values as a cache over the log**, dropped under memory pressure and recomputed from their records when read. A read could take as long as the first computation. A value that cannot be recomputed, such as a cut of an old snapshot, would fail or not depending on what was evicted.

## Consequences

- `Session`, `client.session()`, `where=`, record leases, and the word "holder" are gone. Where a request runs is the system's decision.
- A loop that keeps every result keeps every value until it releases them. In-process this behaves like a list of arrays that a notebook never clears.
- A hosted service needs a way to end a client that vanishes, such as a lease renewed by every call, and may cap what one client keeps. Neither is implemented.
- After a kernel restart, a new kernel is a new client. A small value is computed again from its record, or read from its saved output once saving exists. Expensive state belongs in a driver that outlives kernels.
- After a backend restart, no client keeps anything. A record that completes then is dropped once nothing pending reads it.
- A stage whose template references a released record takes no more calls, since each call is checked as the plain request.

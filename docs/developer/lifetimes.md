# How long records and values are kept

Records and output values have different lifetimes.
A record is small and is kept with the proposal's history.
An output value, such as an I(Q) array, may be large and is kept only while something needs it.

**In short:**

- Records are kept until the proposal has been idle for days to weeks, then dropped together.
- An output value lives while a *keeper* keeps it: the client that asked for it, a pending request that reads it, or the store. A record object or a reference keeps nothing.
- Nothing is written to the store unless it is persisted.
- A client reads and references only values it keeps, persisted values, files, and the current states of its own accumulators.
- Work whose outputs nothing would keep is cancelled.

[ADR 0002](adr/0002-a-value-lives-while-something-keeps-it.md) and [ADR 0004](adr/0004-history-is-append-only-lists.md) give the reasons.

## Records

Records are the proposal's history.
They are dropped as a whole once the proposal has been idle for days to weeks.
A proposal is idle while it has no open client, no pending record, and no write to the store in progress.

## Values and their keepers

| Keeper | Keeps the value until |
|---|---|
| the client that asked for the record (`submit`, `compute`, or `freeze` without `persist=`) | the client releases the record or ends |
| a pending request that reads it | the request's workflow has returned |
| a push, a stage, or an accumulator's template that references it | the binding has taken it, or the push, stage, or accumulator is released, stopped, or dropped |
| a persist request | the value is written |
| the store | the proposal's history is dropped |

Nothing else keeps a value, and a value that is gone does not come back.
The binding is a black box: the framework hands it values (a stage's fixed values at its first call, an accumulator's template when it makes the driver, a push's rows when the push is added) and does not track what it keeps after that.

**What a client may read.** A submission may reference, and `client.output` may read, only

- output values its client keeps,
- persisted outputs,
- files,
- the current state of its client's own accumulators.

Any other reference is refused, even to a pending record, and `client.output` raises `NotReadable`.
So another client, even one in the same process, reads a value only once it is persisted.
`client.readable(records)` gives the outputs of each record that a submission may reference now, without waiting or reading, so a form calls it each time it lists records to pick from.

```python
centre = client.compute(BEAM_CENTRE, params={'direct_beam': file(run=60330)})
result = client.submit(IOFQ, params={..., 'beam_centre': centre.ref('centre')})
client.release(centre)              # the pending request keeps the centre until it has run
client.output(result, 'iofq')       # allowed: this client asked for the record
client.output(centre, 'centre')     # raises NotReadable: this client released it
```

`client.close()`, or the end of a `with client:` block, releases everything the client keeps.

**Work that nothing keeps is cancelled.** A pending record whose outputs nothing would keep finishes as `cancelled`.
So releasing a pending record that no pending request reads cancels it, and since a cancelled request keeps nothing it reads, cancelling passes along a chain.
A running workflow is not interrupted: its record is cancelled at once, and its outputs are dropped when it returns.

## Persist

An output is *persisted* while a persist request names it and its write has not failed:

```python
client.persist(result, 'iofq')                  # the store keeps iofq too, next to the client
client.submit(IOFQ, params={...}, label='night',
              persist=('iofq',))                # only the store keeps iofq
morning = connect(url, proposal='p1')           # the next day, any process
morning.output(morning.records(label='night')[0], 'iofq')   # read from the store
```

`persist=` at submission makes the store the only keeper, and outputs not named are dropped.
Such a record completes once its outputs are written, and fails if a write fails.
A client reads the values it keeps from memory, and other persisted values from the store, after waiting for their write.

## The two deployments

In the user's process (`local()`), values live in its memory and `local(store=...)` sets the store.
Closing the client also closes the backend, after the persisted outputs are written.

The service keeps values in its memory and limits the memory of each client (the *cap*).
It ends a client that makes no call within its *lease*, and gives each proposal a store.

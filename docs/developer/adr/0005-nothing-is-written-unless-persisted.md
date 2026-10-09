# ADR 0005: Nothing is written unless persisted, in the user's process and on the service

- Status: accepted
- Deciders: Simon
- Date: 2026-10-05, rewritten 2026-10-07

## Context

Batch and automatic reduction run as services, "so leaks and peak memory must be bounded by how the services work, not by users freeing memory" ([users](../../requirements/users.md)).
Users find the results of a batch reduction they started the day before (same page).
An upgrade of the service is assumed to let the reductions running at that moment finish ([systems](../../requirements/systems.md), Assumed; system story H2).
The framework does not tell intermediate from final results ([users](../../requirements/users.md)), so it cannot decide by itself which outputs to write.

[ADR 0002](0002-a-value-lives-while-something-keeps-it.md) decides what keeps a value: the client that asked for a record keeps its values until it releases them or ends.
That rule alone fails for unattended work.
The trigger loop submits through one client and never releases anything, so automatic reduction would keep every output it makes.
A batch client that closes would take its values with it, so the next morning only the records would be left.

Writing every output costs what nobody asked for:

- The masked counts of story S3, an output meant for looking inside a reduction, are written 500 times in a batch of 500.
- A 4D volume that a web UI only looks at (story B4) is written whole, hundreds of GB.
- Every look at a value is a write and a read back.

[ADR 0006](0006-the-unit-is-an-accumulating-workflow.md) makes the reduction of one run part of the binding of a multi-run spec, never an output.
So what is written are the outputs of requests: results, and inputs worth keeping as results of their own, such as a beam centre or a direct-beam function.

## Decision

**Nothing is written unless persisted**, in the user's process and on the service alike.
To *persist* an output is to write it to the *store*, which then keeps it ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md), the keepers).
An output is *persisted* while a persist request names it and its write has not failed: it is written, or its write is pending.

```python
r = client.submit(IOFQ, {'run': dataset(run=60339)})     # its values are kept for this client
client.persist(r)                                         # writes its outputs to the store
client.submit(requests, label='night', persist=True)      # unattended: each written when it completes
total = client.freeze(volume, persist=('counts',))        # the last state of an accumulator, written
```

- **`client.persist(records, *outputs)`** logs a request to persist the named outputs, or all of them, of records its client keeps. It adds the store as a keeper. The client keeps its own hold until it releases the record, so reads stay in memory until then. It is refused for a record the client does not keep, and for an output that one of the records lacks.
- **`client.submit(..., persist=True)`**, or `persist=('iofq',)` for named outputs, logs the persist request with the submission. The client does not keep these records. The outputs named are kept until written; the others are dropped when the record completes. Batch reduction and the trigger loop submit this way, so their clients keep nothing. A rule names the outputs it persists, all of them by default.
- **`client.freeze(acc, persist=...)`** does the same for the record of the last state ([ADR 0003](0003-accumulators-add-in-place.md)).
- **Every client of the proposal reads and references persisted values**, such as a colleague's notebook, an AI agent, or a rule's template. Reads wait for the write.
- **History** logs each persist request, and each write when it ends, with the outputs written or why it failed ([ADR 0004](0004-history-is-append-only-lists.md)).
- **A failed write of a record persisted at submission fails the record**, with the write's reason. Such a record is done only once it is written, so a batch driver or the trigger loop sees a failed write as it sees any failed record. Nothing keeps the value any more; the request is submitted again.
- **A failed write of `client.persist`** fails that persist request only. The record keeps its status, and the client, which still keeps the value, can ask again. The value is no longer persisted: for other clients, reads raise with the write's reason, and requests that reference it are refused, or fail if they were waiting for the write.
- **Asking to persist an output that is persisted** does nothing.
- **`client.publish`** reads the value as `client.output` does, and puts it in the catalogue with its provenance. It needs no persist request.
- **The store finds a value by the record's ID and the output's name.** History names no file, so a record never changes.
- **Values are dropped from the store with the proposal's history at the latest** ([ADR 0004](0004-history-is-append-only-lists.md)). A value dropped earlier, for example to free disk space (system story H1), is read as a value that nothing keeps: reading it raises, a request that references it is refused at submission, and the record stays.

**The two deployments** follow the same rules. What differs:

| | user's process (`local()`) | service |
|---|---|---|
| values that are not persisted | in the process's memory | in the service's memory, under a cap per client |
| a client ends | at `close()`, which waits until the pending records with a persist request are written, or with its process | at `close()`, or when its lease runs out (scipp/essapps#34) |
| the store | given to `local(store=...)`; without one, `persist` is refused; in the first release, only tests pass a store, a fake one in memory | the proposal's area |
| history | in memory, or in a file | the service's |

**On the service:**

- **The cap** counts the values a client keeps, those its pending requests keep, and its held states. Values with a persist request count against the proposal until written, and the cap never drops them.
- **At the cap**, a completed value that does not fit is dropped, as for a released record, and reading it raises with the reason. A freeze moves the held state's outputs into its record, so memory they share counts once; outputs that allocate count while both exist. If a frozen value does not fit, the freeze record fails, and the accumulator stays readable.
- **A client's lease** is renewed by every call. A call in progress keeps it alive, such as a `client.output` that waits for a long reader.
- **Values reach other nodes** as the system decides, for example through a scratch file deleted once nothing keeps the value.

**A restart** keeps history and the store, and no client.
Each pending record is decided in this order:

1. It is cancelled ("the backend restarted") unless a persist request names it, or a pending record that a persist request names reads it, directly or through other pending records.
2. Otherwise it runs if each value it reads is a dataset, is written, or is an output of a pending record that runs.
3. Otherwise it fails. A record that reads a held state fails, since the held state is gone, and so does a record that reads a completed value that was not persisted.

A write that had not ended at the restart has failed, since the value is gone: a record persisted at submission fails with it, and so do the pending records that read the value.

## Open

To be designed with the service (scipp/essapps#27, scipp/essapps#23):

- **How the cap per client is set**, and where a client's values are held when its work spreads over nodes.
- **How the service holds the held state of an accumulator.** One job per accumulator on the cluster, with a memory size and a deadline that its client declares, fits one case: a spectroscopy volume of hundreds of GB that users look at while it grows ([spectroscopy](../../requirements/spectroscopy.md)). That case rests on items that are Assumed or Open there: one file per angle, the size of the grid, and how often users look. Most held states are small (0.34 MB for LoKI's I(Q), [ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)). For a binding without a held state of its own, the state is the template and the pushes, which history holds, so a read is a plain request. Whether the service may submit cluster jobs at all is open ([systems](../../requirements/systems.md)).
- **A memory bound for every request, not only for accumulators.** The plain request over two plus two LoKI runs needs 7.5 to 9.9 GB, and more with each run ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)). Only the binding knows what a request needs.
- **What a finish records besides the status**, such as the software environment that ran the request, which provenance promises (README.md, Provenance and publication) and which only the worker that runs it knows.
- **When values are dropped from the store before history** (system story H1), and by whom.
- **The file format of each output type, and the layout of the store within a proposal's area** (scipp/essapps#23).
- **Whether `persist=` at submission should also keep the values for the client**, or both calls need more options. This is settled once stories use them.
- **An upgrade with persisted work running.** The requirements assume that the reductions running at an upgrade whose results someone asked to keep finish ([systems](../../requirements/systems.md), Assumed; system story H2). The old instance could finish them before it stops, but only one backend writes history, so the new version could not start until the longest of them ended: hours for an overnight batch. The new instance could instead treat the upgrade as a restart and run them again, which loses the time they had run. Whether the old instance takes submissions meanwhile, and what its clients see, depends on the choice. Either way, the upgrade ends every client of the old instance.

## Alternatives considered

- **The service writes every output.** It writes and reads back what nobody asked for, every look included, to give unattended work, restarts, and other nodes a value that outlives its client. Those needs are met without it: unattended drivers persist at submission, a restart runs pending persisted work again, and the system moves a value to another node only when a reader there needs it.
- **The service persists by default, with an opt-out for looks.** Writing then stays the default cost, and every interactive client must remember to opt out. The clients that must persist, the trigger loop and batch applications, are framework code.
- **No cap per client on the service**, since the trigger loop would reach it, and a closing batch client would take its values with it. Both persist at submission, so neither keeps anything under the cap.
- **Writing only final outputs.** The framework does not tell intermediate from final results.
- **Writing every state of an accumulator.** A volume of hundreds of GB would be written once per run, and most states are never read.
- **The volume written to a file after each run, and cuts read from it.** The service would hold no held state. But cuts through a growing volume of hundreds of GB do not follow a slider when read from disk (Simon, 2026-10-05).
- **A record that names its file.** A record would change when it is written. The store finds a value by the record's ID and the output's name, and history logs when it was written.

## Consequences

- A hosted client keeps values, so the service needs a lease and a cap per client (scipp/essapps#27, scipp/essapps#34).
- An interactive client that persists nothing loses its values when it ends; its records stay and say so.
- An output that nobody persists is never written, such as the masked counts of story S3 when a batch persists only `iofq`.
- The store goes to an area per proposal that the framework owns. `publish` copies a value into the proposal's upload folder and registers it in SciCat, so a result needed for longer goes to SciCat, as the requirements ask ([tensions](../../requirements/tensions.md)).
- The store is given to the backend, not built into it: each deployment configures its own, and tests use a fake.
- Persisting is first-release work for batch and automatic reduction (scipp/essapps#23).
- A record's outputs never change, so a copy of a stored value that a worker keeps never goes stale. A value that many requests read, such as a direct-beam function, can be read from the store once per worker.
- In the user's process, a store on disk is likely needed later, not for the first release ([users](../../requirements/users.md), Assumed): after a notebook or an application restarts, the user finds the results they persisted and what produced them. One backend writes a store at a time, as one backend writes the log; a second process opens it to read. Values that were not persisted are gone after such a restart, and their records say so; stages and accumulators are not resumed.
- Placement (README.md, Symmetries) holds only if every output reads back from the store as the workflow returned it. The fake store passes values in memory, so nothing checks this yet.

# Proposal: nothing is written unless persisted, and an accumulator is read like a record

- Status: proposal, not decided
- Date: 2026-10-07
- Replaces: ADR 0008 as written in PR scipp/essapps#47 (not merged), and the rule of [ADR 0005](../adr/0005-the-service-writes-every-output.md) that the service writes every output
- Answers: scipp/essapps#49 (a record and its outputs)
- Changes: scipp/essapps#48 (views) becomes the selection argument of `client.output`

## Summary

```python
r = client.submit(IOFQ, {'run': dataset(run=60339)})     # a record; its value is kept for this client
client.output(r, 'iofq', index=3)                         # reads (a selection of) the value: nothing written
client.persist(r)                                         # writes its outputs to the store
client.submit(requests, label='night', persist=True)      # unattended: each written when it completes

volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push({'runs': {'run': run}})
    show(client.output(volume, 'counts', index=0))        # copies the slice; makes no record
fit = client.submit(FIT, {'data': volume.ref('counts'), 'index': 2})   # reads the current state in place
total = client.freeze(volume, persist=True)               # the last state as a record: no copy, no more pushes
```

1. **Records form a graph; values come and go.** A record names its inputs, and those edges never change. A value exists while something keeps it. Releasing drops a value, never a record or an edge.
2. **Nothing is written unless persisted**, in the user's process and on the service alike. `persist` writes values to the store, which then keeps them.
3. **A request that reads an accumulator reads a record of the state**, whose value the accumulator keeps for that request. It is a record like any other, and needs no rules of its own.
4. **`client.freeze(acc)`** ends an accumulator and returns the record of its last state, whose value is what the accumulator holds, without a copy.

## Why

The README with ADR 0008 has three problems (Simon, review of #47):

1. **Accumulators differ from other workflows.** ADR 0008 gives the record of a state four rules that no other record has: no client keeps its outputs; only the submission that made it reads it; `client.output` of it copies; on the service, nothing writes it.
2. **The service and the user's process keep values by different rules.** The client keeps values in memory in one ([ADR 0002](../adr/0002-the-client-is-the-lifetime.md)); every output is written to a file in the other ([ADR 0005](../adr/0005-the-service-writes-every-output.md)); both have exceptions for states.
3. **Overhead nobody asked for.** The service writes and reads back every output: the masked counts of story S3 are written 500 times in a batch of 500, and a volume a web UI only looks at is written whole. Copying the last state of an accumulator (`COPY`) needs a second volume.

The rules in (1) are not about accumulators.
They are rules about values, written as rules about records: who may reference a record, and what the service writes.
Stated about values, the general rules give each of them (see Records of states).

## The model

```text
FIT ──> state 3 of volume: VOLUME({'runs': [run 1, run 2, run 3]}) ──> datasets run 1, run 2, run 3
IOFQ ──> BEAM_CENTRE ──> dataset run 60330
```

A record is a request as the backend accepted it, with its inputs: outputs of other records, and datasets.
These edges are its provenance, and they never change.
A record that reads an accumulator, the record of a state or the record that `freeze` returns, reads the accumulator's held state, not its inputs: an edge is not a read.

A value exists while something keeps it:

| Keeper | Keeps | Until |
|---|---|---|
| the client that asked for the record (`submit`, `compute`, `freeze`) without `persist=` | the record's values | it releases the record or ends |
| a pending request | the values it reads | it has finished and its workflow has returned |
| a pending read of an accumulator: the record of a state, a `freeze`, a `client.output` | the value of that state, and the pushes up to it | it has finished |
| a push | the values its rows reference | it is added or dropped |
| an accumulator's template | the values it references | the held state has opened |
| a held state that keeps its rows | the values they reference | the held state is dropped |
| a persist request | the values it names | they are written |
| the store | written values | the proposal's history is dropped, or a file is dropped earlier (system story H1) |

**A submission reads only values kept for it:** values its own client keeps, values with a persist request, datasets, and the current state of its client's accumulators.
A request that needs any other value is refused at submission, whether that record is pending or completed.

**`client.output`** reads values its own client keeps, values with a persist request (waiting for the write if needed), and accumulators (by copying, see Accumulators).
Reading a value that nothing keeps for the caller raises; the record stays.

**Work that nothing will keep is cancelled.** When nothing keeps a pending record's values any more, the record finishes as cancelled: its value would be dropped the moment it was computed. Cancelling a request lets go of what it reads, so this passes along a chain.

**Every workflow** leaves its inputs unchanged and returns no output that shares memory with an input.
A workflow cannot tell whether an input is a record's value or an accumulator's state, so this is one promise for all workflows.

## Persist

- **`client.persist(records, *outputs)`** logs a request to persist the named outputs, or all of them, of records its client keeps. It adds the store as a keeper. The client's own hold stays until it releases the record, so reads stay in memory until then.
- **`client.submit(..., persist=True)`**, or `persist=('iofq',)` for named outputs, logs the request with the submission. The client does not keep these records. The outputs named are kept until written; the others are dropped when the record completes. Batch reduction and the trigger loop submit this way, so their clients keep nothing.
- **`client.freeze(acc, persist=...)`** does the same for the record of the last state.
- **Once a persist request is logged**, every client of the proposal can read and reference the values it names, such as a colleague's notebook, an AI agent, or a rule's template. Reads wait for the write.
- **History** logs each persist request, and a `Written` event when a write ends, with the outputs written or the failure. A record's status is that of its computation. A failed write fails the persist request, not the record, and the request can be made again.
- **Asking to persist an output already persisted** does nothing.

## Accumulators

Pushes work as now ([ADR 0003](../adr/0003-accumulators-add-in-place.md)): a push is added once nothing keeps the value of the state before it.

**Reads.**

- `client.output(acc, name, selection=None)` pins the state after the pushes logged so far, waits until it has been added, and copies the output or the selection. It makes no record.
- A request that references `acc.ref(name)` pins the state at submission. The backend makes the record of the state's plain request for that submission, and the request's record references it. The request reads the state's value in place while it runs.
- A template or a row reads no accumulator: it would hold back every push for as long as the stage or accumulator holding it lives. It references the record that `freeze` returns.
- A request reads at most one accumulator. Two held states must be in one process to be read together, and where the service holds them is open.
- History stores the record of a state as the accumulator and its number of pushes, and lists the rows when it is read, so history grows by a constant amount per push and per read ([ADR 0004](../adr/0004-history-is-append-only-lists.md) keeps the accumulators and their pushes).

### Records of states

The record of a state is made for the submission that reads it.
No client asked for it, so no client keeps it.
Its value is kept for the requests of that submission until their workflows have returned.
The general rules then give each rule that ADR 0008 states on its own:

| ADR 0008 rule | Follows from |
|---|---|
| no client keeps its outputs; they are dropped once its readers have run | the keepers table: no client asked for it, and its readers keep it until they have run |
| only the submission that made it reads it, since a later reader could deadlock with a later push | a submission reads only values kept for it; this value is kept for no later submission |
| `client.output` of it copies | `client.output` of it raises, since nothing keeps it for the caller; the accumulator is read instead |
| on the service, nothing writes it | nothing is written unless persisted, and only values a client keeps can be persisted |
| a pending one is computed again after a restart | a record that reads a held state fails at a restart (Restart) |

Its edges to the rows' records are provenance, not reads, so it does not matter that the client released those records after pushing them.
Every wait is still for work logged before the waiter ([ADR 0003](../adr/0003-accumulators-add-in-place.md), Adding), so no wait goes round in a circle.

### Freeze

`client.freeze(acc, persist=None)` returns the record of the plain request of the state after the pushes logged before it, and ends the accumulator.

- Freeze and pushes are ordered as they are logged: a push logged after a freeze is refused.
- The plain request of that state is checked, as a read checks it. If it would be refused, the freeze is refused and the accumulator stays as it was.
- The record reads the held state: until it has finished, it keeps the held state and the pushes up to its state.
- Its values are the outputs of the held state, computed once and not copied. The rest of the held state is dropped once the record has completed.
- If the record fails or is cancelled, the accumulator takes no pushes, but can still be read and frozen again, unless it has stopped ([ADR 0003](../adr/0003-accumulators-add-in-place.md), Failures).

`COPY` is not needed: a finished accumulator is frozen.

## Restart, release, ending, and upgrade

- **Releasing and ending** drop the client's hold, and work that nothing else keeps is cancelled. A driver may still release an accumulator right after its last read: the pinned reads keep their states and the pushes up to them. Pushes that no pinned read needs are dropped.
- **A restart** keeps history and the store, and no client. A pending record runs if every value it reads is written, will be written for a pending record with a persist request, or is a dataset. A record that reads a held state, or whose other inputs are gone, fails. A pending record that nothing keeps any more is cancelled ("backend restarted").
- **An upgrade of the service** lets the old instance finish the pending records with a persist request, what they read, and the pushes they need, before it stops (system story H2). The upgrade ends every client of the old instance.

## Deployments

The rules above hold in both. What differs:

| | user's process (`local()`) | service |
|---|---|---|
| values that are not persisted | in the process's memory | in the service's memory, under a cap per client |
| a client ends | at `close()`, or with its process | at `close()`, or when its lease runs out (scipp/essapps#34) |
| the store | a folder given to `local(store=...)`; without one, `persist` raises | the proposal's area |
| history | in memory, or in the store's folder | the service's |

On the service:

- **The cap** counts the values a client keeps, those its pending requests keep, and its held states. Values with a persist request count against the proposal until written, and the cap never drops them.
- **At the cap**, a completed value that does not fit is dropped, as for a released record, and reading it raises with the reason. A freeze moves the held state's outputs into its record, so memory they share counts once. Outputs that allocate count while both exist. If a frozen value does not fit, the freeze record fails, and the accumulator stays readable. How a held state that outgrows the cap is bounded is the README's open question 5.
- **A call in progress keeps the client's lease alive**, such as a `client.output` that waits for a long reader.
- **Values reach other nodes** as the system decides, for example through a scratch file deleted once nothing keeps the value.

In the user's process, a store on disk is likely needed later, not for the first release ([users](../../requirements/users.md), Assumed): after a notebook or an application restarts, the user finds the results they persisted and what produced them.
One backend writes a store at a time (the log already holds its file with `flock`); a second process opens it to read.
After a restart, records and persisted values are back; values that were not persisted are gone, and their records say so; stages and accumulators are not resumed.

## Costs

V is the size of a large output, such as a spectroscopy volume of hundreds of GB.

| | README with ADR 0008 | this proposal |
|---|---|---|
| a notebook computes and looks | a record per request; values in memory | the same |
| a batch or a rule on the service | every output written, also those nobody reads | only the outputs named in `persist=`: S3's masked counts are written only if asked for |
| a web UI looks at a large volume on the service (B4) | V written when computed | held under the client's cap, written only if persisted; after `client.persist`, slices still read from memory until the client releases it |
| look at a cut after each of N pushes | a request per push (D7): 2N records, O(N²) rows in history | `client.output` with a selection: no record; the slice is copied |
| a cut or fit of the growing state, kept, after each push | a record of the state and of the request; no copy of V; the next push waits | the same; history stores each record of a state as the accumulator and its count, so it grows by a constant per push |
| keep the last state of a large accumulator | `COPY`: one more V in memory until the accumulator is released; on the service, V written | `freeze`: no copy; written only if persisted |
| outputs that allocate, such as a volume normalized from sums | one V per state read | the same; `freeze` holds the sums and the outputs at once, 2V, until the record completes |

## What changes

- **ADR 0002** states the keepers table for both deployments, and that work nothing keeps is cancelled; "releasing and ending stop no work" goes.
- **ADR 0003**: `freeze`; templates and rows reference frozen records; `COPY` goes; the record of a state is stored as the accumulator and its count.
- **ADR 0004**: history logs persist requests and `Written` events.
- **ADR 0005** is rewritten: nothing is written unless persisted; the cap and the lease; other clients read persisted values; an upgrade finishes pending persisted work.
- **ADR 0008** is rewritten: the record of a state, with the table above.
- **README**: Terms (persist, freeze), From a for loop, Requests and records, How long records and values are kept, References, Stages and accumulators (Reads, Releasing, On the service), Drivers, Batch and automatic reduction, Guarantees, open questions 4 and 5.
- **Requirements**:
  - tensions.md: the summary line of Settled, "Fast interactive work versus bounded memory", and "Finding results again versus a second catalogue". Results are kept while users work with them, and written when someone persists them.
  - The requirements README, "In one minute": what the framework keeps for days to weeks is persisted results.
  - systems.md, Assumed: an upgrade lets the persisted reductions running at that moment finish.
- **Stories**:
  - Batch and rule stories persist at submission: D1, D2, D3, D6, and E1 to E4. D2 reads its night outputs through the morning client.
  - B4 selects from a record.
  - D7 looks through selections and freezes at the end.
  - F2 uses the client that an upgrade returns.
  - Toy specs: `COPY` goes, and `ANGLE` and `CONTRIBUTE` return copies, since no output shares memory with an input.
  - System stories B4, B6, D2, D7, and H2.
- **Code**:
  - the store, persist requests, and `Written` events;
  - the read checks against what is kept for the caller;
  - cancelling work that nothing keeps;
  - `freeze`;
  - selections.

## Alternatives considered

- **The service writes every output** ([ADR 0005](../adr/0005-the-service-writes-every-output.md)). It writes and reads back what nobody asked for, every look included, to give unattended work, restarts, and other nodes a value that outlives its client. Those needs are met without it: unattended drivers persist at submission, an upgrade finishes pending persisted work, and the system moves a value to another node only when a reader there needs it.
- **The service persists by default, with an opt-out for looks.** Writing then stays the default cost, and every interactive client must remember to opt out. The clients that must persist, the trigger loop and batch applications, are framework code.
- **A cap per client was rejected in [ADR 0005](../adr/0005-the-service-writes-every-output.md)**, because the trigger loop would reach it, and a closing batch client would take its values with it. Both persist at submission, so neither keeps anything under the cap.
- **ADR 0008 as written.** Its four rules restate rules about values as rules about one kind of record.
- **Requests read only frozen accumulators**, and looks read the growing state. A request that reads the state in place does what a selection does, with more work done in place, and its record is an ordinary one (Records of states).
- **Views first, with records made only at persist** (first draft, reviewed 2026-10-07). It needed a second handle type, records of upstream views without values that a later `persist` could not complete, and pinned states kept inside views. A record is one entry in history, so making it lazily saves nothing worth that.
- **`with client.borrow(acc, 'counts') as counts:`**, a read in the client's own code without a copy. Code in the block that waits for a later state of the same accumulator waits for itself, an interactive plot made in the block keeps the data after it, and on the service a borrow is a copy over the network. Likely revisited in some form, for example once jobs that read an accumulator run where it is held (Simon).

## Open questions

1. **Names.** `persist` or `save`; `freeze` or `close` (`client.close()` ends a client).
2. **Selections** (scipp/essapps#48): their form, and a look that computes, which runs a request next to the state and returns its result without a record.
3. **One accumulator per request.** Lift it once the service places a request next to the held states it reads.
4. **Modified values.** In the user's process, `client.output` returns the value itself. A notebook that modifies it in place breaks the promise every workflow makes, and changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.
5. **The service.** How the cap per client is set, and where a client's values are held when its work spreads over nodes.
6. **Rules that push into an accumulator** ([automatic-reduction.md](../automatic-reduction.md), open).

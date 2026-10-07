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
total = client.freeze(volume)                             # the last state as a record: no copy, no more pushes
```

1. **Records form a graph; values come and go.** A record names its inputs, and those edges never change. A value exists while something keeps it. Releasing drops a value, never a record or an edge.
2. **Nothing is written unless persisted**, in the user's process and on the service alike. `client.persist` writes values to the store, which then keeps them.
3. **A request that reads an accumulator reads a record of the state** whose value the accumulator keeps until its next push. It is a record like any other, and needs no rules of its own.
4. **`client.freeze(acc)`** ends an accumulator and returns the record of its last state, whose value is what the accumulator holds, without a copy.

## Why

The README with ADR 0008 has three problems (Simon, review of #47):

1. **Accumulators differ from other workflows.** ADR 0008 gives the record of a state four rules that no other record has: no client keeps its outputs; only the submission that made it reads it; `client.output` of it copies; on the service, nothing writes it.
2. **The service and the user's process keep values by different rules.** The client keeps values in memory in one ([ADR 0002](../adr/0002-the-client-is-the-lifetime.md)); every output is written to a file in the other ([ADR 0005](../adr/0005-the-service-writes-every-output.md)); both have exceptions for states.
3. **Overhead nobody asked for.** The service writes and reads back every output: the masked counts of story S3 are written 500 times in a batch of 500, and a volume a web UI only looks at is written whole. Copying the last state of an accumulator (`COPY`) needs a second volume.

The rules in (1) are not about accumulators.
They come from rules about values written as rules about records: who may reference a record, and what the service writes.
Stated about values, the general rules give each of them (see Records of states).

## The model

```text
FIT ──> state 3 of volume: VOLUME({'runs': [run 1, run 2, run 3]}) ──> datasets run 1, run 2, run 3
IOFQ ──> BEAM_CENTRE ──> dataset run 60330
```

A record is a request as the backend accepted it, with its inputs: outputs of other records, and datasets.
These edges are its provenance, and they never change.

A value exists while something keeps it:

| Keeper | Keeps | Until |
|---|---|---|
| the client that asked for the record (`submit`, `compute`, `freeze`) | the record's values | it releases the record or ends |
| a pending request | the values it reads | it has run |
| a push | the values its rows reference | it is added |
| an accumulator's template | the values it references | the held state has opened |
| a held state that keeps its rows | the values they reference | the accumulator ends |
| an accumulator | the value of its current state, while a read of it is pending | its next push is added |
| the store | persisted values | the proposal's history is dropped, or a file is dropped earlier (system story H1) |

**A submission reads only values that are kept for it:** values its own client keeps, persisted values, datasets, and the current state of its client's accumulators.
A request that needs any other value is refused at submission, whether the record is pending or completed.
An edge is not a read: a record whose value comes from what the backend holds, such as a state, reads none of its inputs.

**`client.output` reads only values the caller's own client keeps, or the store.**
It reads an accumulator by copying (see Accumulators).
Reading a value that is not kept raises, and the record stays.

**Work that nothing will keep is cancelled.** When the last keeper of a pending record's value lets go, and no pending request reads it, the record finishes as cancelled. Its value would be dropped the moment it was computed.

**Every workflow** leaves its inputs unchanged and returns no output that shares memory with an input.
A workflow cannot tell whether an input is a record's value or an accumulator's state, so this is one promise for all workflows.

## Persist

`client.persist(records, *outputs)` writes the named outputs, or all of them, of records whose values its client keeps.
Outputs that are not persisted are kept as before, and dropped when their record is released.

- Any order, at any time, any number of times. Persisting a persisted record does nothing.
- A pending record is written when it completes, also if its client has ended by then. Its pending inputs are kept for it until it has run.
- `client.submit(..., persist=True)`, or `persist=('iofq',)` for named outputs, persists at submission, in the same event. Batch reduction and the trigger loop use it, so their clients keep nothing in memory, and a crash cannot leave work done with nothing to show for it.
- History logs the request to persist when it is made. The finish of the record says which outputs were written, after each file is complete. If writing fails, the record fails with that reason.
- Once a value is written, the store keeps it, and the client's own hold on it ends. Reads go to the store. The backend may keep a copy in memory as a cache, but never the object it handed to a caller.
- Other clients of the proposal, such as a colleague's notebook, an AI agent, or the trigger loop, read persisted values. A rule's template references persisted records.

## Accumulators

Pushes work as now ([ADR 0003](../adr/0003-accumulators-add-in-place.md)).

**Reads.**

- `client.output(acc, name, selection=None)` pins the state after the pushes logged so far, waits until it has been added, and copies the output or the selection. It makes no record.
- A request that references `acc.ref(name)` pins the state at submission. The backend makes the record of the state's plain request for that submission, and the request's record references it. The request reads the state's value in place while it runs.
- Both hold back the next push until they are done. This is what the accumulator's value being kept until its next push means.
- A template or a row reads no accumulator: it would hold back every push for as long as the stage or accumulator holding it lives. It references the record that `freeze` returns.
- A request reads at most one accumulator. Two held states must be in one process to be read together, and where the service holds them is open.

### Records of states

The record of a state is made for the submission that reads it.
No client asked for it, so no client keeps it.
Its value is kept by the accumulator until the next push, and by the requests of that submission while they are pending.
The general rules then give each rule that ADR 0008 states on its own:

| ADR 0008 rule | Follows from |
|---|---|
| no client keeps its outputs; they are dropped once its readers have run | the keepers table: no client asked for it, and its readers keep it until they have run |
| only the submission that made it reads it, since a later reader could deadlock with a later push | a submission reads only values kept for it; this value is kept for no later submission |
| `client.output` of it copies | `client.output` reads only what the caller's client keeps, or the store; the accumulator is read instead |
| on the service, nothing writes it | nothing is written unless persisted, and only a client's own records are persisted |
| a pending one is computed again after a restart | a restart cancels every pending record that is not persisted (Restart) |

Its edges to the rows' records are provenance, not reads, so it does not matter that the client released those records after pushing them.
Every wait is still for work logged before the waiter ([ADR 0003](../adr/0003-accumulators-add-in-place.md), Adding), so no wait goes round in a circle.

### Freeze

`client.freeze(acc)` returns the record of the plain request of the state after the pushes logged so far, and ends the accumulator.

- The plain request is checked at the call, as a read checks it; a state that its plain request would refuse is refused, and the accumulator stays as it was.
- Later pushes are refused. The record is pending until the pushes logged so far have been added.
- Its values are the outputs of the held state, computed once and not copied. The rest of the held state is dropped once the record has completed.
- If the record fails or is cancelled, the accumulator takes no pushes but can still be read and frozen again.
- The client keeps the record like any it asked for. Persisting it holds back nothing.

`COPY` is not needed: a finished accumulator is frozen.

## Restart, release, and ending

- **Releasing and ending** drop the client's hold. Work that nothing else keeps is cancelled (The model). A driver may still release an accumulator right after its last read: the pinned reads keep the states they read, and the pushes up to them. Pushes that no pinned read needs are dropped.
- **A restart** keeps history and the store, and no client. A pending record that is not persisted has no keeper left, so it finishes as cancelled ("backend restarted"). A pending persisted record runs if the values it reads are persisted or datasets, and fails otherwise.
- **An upgrade of the service** lets the old instance finish the pending persisted records, and what they read, before it stops (system story H2).

## Deployments

The rules above hold in both. What differs:

| | user's process (`local()`) | service |
|---|---|---|
| values that are not persisted | in the process's memory | in the service's memory, up to a cap per client |
| a client ends | at `close()`, or with its process | at `close()`, or when its lease runs out (scipp/essapps#34) |
| the store | a folder given to `local(store=...)`; without one, `persist` raises | the proposal's area |
| history | in memory, or in the store's folder | the service's |

On the service:

- **The cap counts** the values a client keeps and the held states of its accumulators. Persisted values leave it once written. A value that does not fit is dropped when its record completes, as for a released record, and reading it raises with the reason. Values that pending requests keep after their client has ended are counted against the proposal.
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
| a batch or a rule on the service | every output written, also those nobody reads | only the outputs named in `persist=`: S3's masked counts are not written unless asked for |
| a web UI looks at a large volume on the service (B4) | V written when computed | held under the client's cap; written only if persisted |
| look at a cut after each of N pushes | a request per push (D7): 2N records, O(N²) rows in history | `client.output` with a selection: no record; the slice is copied |
| a fit of the growing state, kept | a record of the state and of the fit; no copy of V; the next push waits | the same |
| keep the last state of a large accumulator | `COPY`: one more V in memory until the accumulator is released; on the service, V written | `freeze`: no copy; written only if persisted |
| outputs that allocate, such as a volume normalized from sums | one V per state read | the same; `freeze` holds the sums and the outputs at once, 2V, until the record completes |

## What changes

- **ADR 0002** states the keepers table for both deployments, and that work nothing keeps is cancelled; "releasing and ending stop no work" goes.
- **ADR 0003**: `freeze`; templates and rows reference frozen records; `COPY` goes.
- **ADR 0004**: history logs requests to persist; a finish lists the outputs written.
- **ADR 0005** is rewritten: nothing is written unless persisted; the cap and lease; other clients read persisted values; an upgrade finishes pending persisted work.
- **ADR 0008** is rewritten: the record of a state, with the table above.
- **README**: Terms (persist, freeze), From a for loop, Requests and records, How long records and values are kept, References, Stages and accumulators (Reads, Releasing, On the service), Drivers, Batch and automatic reduction, Guarantees, open questions 4 and 5.
- **Requirements**: tensions.md, "Fast interactive work versus bounded memory" and "Finding results again versus a second catalogue": results are kept while users work with them, and written when someone persists them. The requirements README's summary line on keeping results.
- **Stories**: batch and rule stories (D2, D6, E1 to E4) use `persist=True`, and D2 reads its night outputs through the morning client. B4 selects from a record. D7 looks through selections and freezes at the end. System stories B6, D2, D7, H2.
- **Code**: the store and `persist`; the read checks against what is kept for the caller; cancelling work nothing keeps; `freeze`; selections; `COPY` leaves the toy specs.

## Alternatives considered

- **The service writes every output** ([ADR 0005](../adr/0005-the-service-writes-every-output.md)). It writes and reads back what nobody asked for, every look included, to give unattended work, restarts, and other nodes a value that outlives its client. Those needs are met without it: unattended drivers persist at submission, an upgrade finishes pending work, and the system moves a value to another node only when a reader there needs it.
- **The service persists by default, with an opt-out for looks.** Writing then stays the default cost, and every interactive client must remember to opt out. The clients that must persist, the trigger loop and batch applications, are framework code.
- **A cap per client was rejected in [ADR 0005](../adr/0005-the-service-writes-every-output.md)**, because the trigger loop would reach it and a closing batch client would take its values with it. Both persist at submission, so neither keeps anything under the cap.
- **ADR 0008 as written.** Its four rules restate rules about values as rules about one kind of record.
- **Requests read only frozen accumulators**, and looks read the growing state. A request that reads the state in place does what a selection does, with more work done in place, and its record is an ordinary one (Records of states).
- **Views first, with records made only at persist** (first draft, reviewed 2026-10-07). It needed a second handle type, records of upstream views without values that a later `persist` could not complete, and pinned states kept inside views. A record is one entry in history, so making it lazily saves nothing worth that.
- **`with client.borrow(acc, 'counts') as counts:`**, a read in the client's own code without a copy. Code in the block that waits for a later state of the same accumulator waits for itself, an interactive plot made in the block keeps the data after it, and on the service a borrow is a copy over the network. Likely revisited in some form, for example once jobs that read an accumulator run where it is held (Simon).

## Open questions

1. **Names.** `persist` or `save`; `freeze` or `close` (`client.close()` ends a client).
2. **Selections** (scipp/essapps#48): their form, and a look that computes, which runs a request next to the state and returns its result without a record.
3. **One accumulator per request.** Lift it once the service places a request next to the held states it reads.
4. **A driver that submits a request per push** logs a record of n rows per push, so history grows with the square of the pushes. History may store the record of a state as the accumulator and its number of pushes ([ADR 0004](../adr/0004-history-is-append-only-lists.md)).
5. **Modified values.** In the user's process, `client.output` returns the value itself. A notebook that modifies it in place changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.
6. **The service.** How the cap per client is set, and where a client's values are held when its work spreads over nodes.
7. **Rules that push into an accumulator** ([automatic-reduction.md](../automatic-reduction.md), open).

# Proposal: values are written when persisted, and an accumulator is read like a record

- Status: proposal, not decided
- Date: 2026-10-07
- Replaces: the record of a state with rules of its own (ADR 0008 in PR scipp/essapps#47, not merged)
- Answers: scipp/essapps#49 (a record and its outputs)
- Changes: scipp/essapps#48 (views) becomes the selection argument of `client.output`

## Summary

```python
r = client.submit(IOFQ, {'run': dataset(run=60339)})     # a record; its value is held for this client
client.output(r, 'iofq')                                  # the value itself: no copy, nothing written
client.persist(r)                                         # writes its outputs to the store
client.submit(requests, label='night', persist=True)      # unattended: each written when it completes

volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
for run in runs:
    volume.push({'runs': {'run': run}})
    show(client.output(volume, 'counts', index=0))        # copies the slice; makes no record
fit = client.submit(FIT, {'data': volume.ref('counts'), 'index': 2})   # reads the current state in place
total = client.freeze(volume)                             # the last state as a record: no copy, no more pushes
```

Four changes to the design in the README:

1. **Nothing is written unless persisted.** Every request still makes a record, one entry in history. Its values are held for the client that made it, in the user's process and on the service alike, until the client releases it or ends. `client.persist` writes values to the store, which then keeps them.
2. **A request references only values that are kept:** outputs of records whose values its client keeps, persisted outputs, datasets, and the current state of its client's accumulators.
3. **The record of a state is a record that no client keeps.** The general rules give each rule that ADR 0008 states for it, so it needs none of its own.
4. **`client.freeze(acc)`** ends an accumulator's pushes and returns the record of its last state, whose values are what the accumulator holds, without a copy.

## Why

The README with ADR 0008 has three problems (Simon, review of #47):

1. **Accumulators differ from other workflows.** The record of a state has four rules that no other record has: no client keeps its outputs; only the submission that made it reads it; `client.output` of it copies; on the service, nothing writes it.
2. **The service and the user's process keep values by different rules.** In the user's process, the client keeps values in memory ([ADR 0002](../adr/0002-the-client-is-the-lifetime.md)). The service writes every output to a file ([ADR 0005](../adr/0005-the-service-writes-every-output.md)). Both have exceptions for records of states.
3. **Overhead that nobody asked for.** The service writes and reads back every output, including those only looked at. Looking at an accumulator through a request logs a record of the state for every look.

The cost in (3) is in writing and copying, not in the records: a record is one entry of metadata in history.
The rules in (1) are exceptions only because the general rules let a request reference a record its client does not keep, and let the service write values that nobody asked to keep.
With those two rules tightened, each exception follows from the general rules (see Records of states).

## Keeping values

The same in both deployments.

**What keeps a value:**

- the client that made the record, until it releases the record or ends;
- a pending request, for the values it reads, until it has run;
- a push, for the values its rows reference, until it is added; an accumulator's template, until its held state has opened; a held state that keeps its rows, for the values they reference;
- the store, for persisted values, until the proposal's history is dropped, or a file is dropped earlier (system story H1).

**What a request references:** an output of a record whose value its client keeps; an output of a persisted record; a dataset; an output of one of its client's accumulators (see Accumulators).
Anything else is refused at submission, whether the record is pending or has completed.
Today a request that references a released record is accepted while that record is pending and refused once it has completed, so the outcome depends on timing.

**Reading.** `client.output` of a record reads the value its client keeps, or the store.
A value that nothing keeps raises; the record stays.

**Workflows** do not modify their inputs, and return no output that shares memory with an input.
A workflow cannot tell whether an input is a record's output or an accumulator's state, so this is one promise for every workflow, not one for readers of accumulators.

**A restart** keeps history and the store.
A pending record whose inputs' values were kept only in memory fails.

## Persist

`client.persist(records)` writes the outputs of records whose values its client keeps.

- Any order, at any time. Persisting a persisted record does nothing.
- A pending record is written when it completes, also if its client has ended by then.
- `client.submit(..., persist=True)` persists at submission, in the same event. Batch and automatic reduction use it: a crash cannot leave work done and nothing written, and their clients hold nothing in memory.
- Once a value is written, the store keeps it, and the client no longer holds it in memory. The backend may keep a copy in memory as a cache; on the service, that copy does not count against the client's cap.
- History notes which records are persisted, so that a record whose value was never written differs from one whose file was dropped.
- A batch submitted without `persist=True`, whose client then ends, loses its values but not its records. The next morning shows what ran and what failed, and resubmitting the requests computes the values again. This answers [ADR 0002](../adr/0002-the-client-is-the-lifetime.md)'s objection to `keep=True` for the case where it applies.

## Accumulators

Pushes work as now ([ADR 0003](../adr/0003-accumulators-add-in-place.md)).
Reads:

- **`client.output(acc, name, selection=None)`** pins the state after the pushes logged so far, waits until it has been added, and copies the output or the selection. It makes no record.
- **A request that references `acc.ref(name)`** pins the state at submission. The backend makes a record of the state's plain request, logged with the submission, and the request's record references that record. The request reads the state's outputs in place while it runs. This is the same as a selection: the state is pinned, the next push waits, and the reader reads in place. Only the work done in place differs.
- **Templates and rows** reference no accumulator. A stage or an accumulator that held a state would hold back every later push for as long as it lives. A template references the record that `freeze` returns.
- **A request reads at most one accumulator.** Two held states must be in one process to be read together, and where the service holds them is open.
- **`client.freeze(acc)`** refuses later pushes and returns the record of the plain request of the last state. The record is pending until the pushes logged so far have been added. Its values are the outputs of the held state, computed once and not copied, and the rest of the held state is dropped. The client keeps the record like any record it makes, and persisting it holds back nothing.

The one rule specific to accumulators is the one a selection has too: the next push waits for whoever reads the state.
`COPY` is not needed: a finished accumulator is frozen, and a copy of a state that keeps growing is the record of a request that copies, as for any input.

### Records of states

The backend makes the record of a state for a submission; no client makes it, so no client keeps it.
The general rules then give each rule that ADR 0008 states on its own:

| ADR 0008 rule | General rule that gives it |
|---|---|
| no client keeps its outputs; they are dropped once its readers have run | only the client that made a record keeps it; a pending request keeps what it reads |
| only the submission that made it reads it, since a later reader could deadlock with a later push | a request references only values its client keeps, or persisted ones |
| `client.output` of it copies | `client.output` of a value that nothing keeps raises; the accumulator is read instead |
| on the service, nothing writes it | nothing is written unless persisted, and only values the client keeps can be persisted |
| after a restart, a pending one is computed again as its plain request | a pending record whose inputs were kept only in memory fails |

Every wait is still for work logged before the waiter ([ADR 0003](../adr/0003-accumulators-add-in-place.md), Adding), so no wait goes round in a circle.

## Deployments

| | user's process (`local()`) | service |
|---|---|---|
| unpersisted values are held in | the process's memory | the service's memory, up to a cap per client |
| a client ends | at `close()`, or with its process | at `close()`, or when its lease runs out (scipp/essapps#34) |
| the store | given to `local()`; tests pass a fake in memory | the proposal's area ([ADR 0005](../adr/0005-the-service-writes-every-output.md)) |
| history | in memory, or in the store (below) | the service's |

- **The cap fails, and never waits.** A submission or a completion that would exceed it fails with the reason. A cap that waited for space could wait for a release that comes after a read that waits for the same work.
- **A call in progress keeps the client's lease alive**, such as a `client.output` that waits for a long reader.
- Batch and automatic reduction persist at submission, so their memory stays bounded by how they work, not by users releasing values.
- A web UI that explores a large volume on the service holds it under its client's cap, and writes it only if it persists it (story B4).

**A store on disk in the user's process** is likely needed later, not for the first release ([users](../../requirements/users.md), Assumed): after a notebook or an application restarts, the user finds the results they persisted and what produced them.

| after a restart in the user's process | |
|---|---|
| records, labels, `client.latest` | back: the store holds history, as on the service |
| persisted values | back: read from the store |
| values that were not persisted | the records stay; the values are gone, and resubmitting computes them again |
| work pending at the restart | finished as failed ("backend ended"); running it again could use a binding edited since |
| stages and accumulators | not resumed; an application pushes its rows again, which reduces every run again |

One backend writes a store at a time; the log already holds its file with `flock`.
A second process opens the store to read.

## Costs

V is the size of a large output, such as a spectroscopy volume of hundreds of GB.
Rows marked (#48) or (freeze) come from those parts alone, which do not depend on the rest.

| | README with ADR 0008 | this proposal |
|---|---|---|
| a notebook computes and looks | a record per request; values in memory | the same |
| a web UI looks at a large volume on the service (B4) | V written when computed | held under the client's cap; written only if persisted |
| a batch or a rule on the service | every output written | the same, with `persist=True` |
| look at a cut after each of N pushes (#48) | a request per push (D7): 2N records and O(N²) rows in history | `client.output` with a selection: no record; the slice copied |
| a request reads the growing state, such as a fit of state n | a record of the state and of the request; no copy of V; the next push waits | the same |
| keep the last state of a large accumulator (freeze) | `COPY`: one more V in memory until the accumulator is released; on the service, V written | no copy; written only if persisted |
| the output of a finished accumulator in another's template (freeze) | `COPY`: one more V | no copy |
| outputs that allocate, such as a volume normalized from sums | one V for each state read | the same; `freeze` computes them once and drops the sums |

## What changes

- **ADRs.**
  - 0002 covers both deployments: what keeps a value, and what a request may reference.
  - 0003: `freeze`; templates reference frozen records; `COPY` goes.
  - 0004: history holds records, finishes, and which records are persisted. Accumulators and pushes leave history; the record of a state lists its rows.
  - 0005: the service writes persisted outputs, and holds unpersisted values per client under a cap and a lease. It has no exception for states.
  - 0008 is rewritten: the record of a state, with the table above.
- **README.** Terms (persist, freeze), Requests and records, How long records and values are kept, References, Stages and accumulators (Reads, Releasing, On the service), Drivers (D7), Batch and automatic reduction (`persist=True`), Guarantees (one promise about inputs for every workflow), and open questions 4 and 5.
- **Requirements.** tensions.md, "Fast interactive work versus bounded memory": results are held while users work with them and written when kept, in both ways of working. users.md gets the likely restart item (added with this proposal).
- **system.md** (the history lists) and **the stories.** Batch and rule stories (D2, D6, E1 to E4, H1 to H3) use `persist=True`. D7 uses selections and `freeze`. `COPY` leaves the toy specs.
- **Code.** `local()` takes a store; history notes persisted records; the reference check; `freeze`.

## Alternatives considered

- **ADR 0008 as it stands.** The record of a state has four rules of its own, and the service writes every output, also those only looked at.
- **Views first, with records made at persist** (the first draft of this proposal, reviewed 2026-10-07). It added a second handle type next to records. It made records of upstream views without values, which a later `persist` could not complete. It kept pinned states inside views for a later `persist`, which is the third reference form that ADR 0008 rejected. And unattended drivers held every view. A record costs one entry in history, so making it lazily saves little.
- **Requests read only frozen accumulators**, with a copy for any other state. A request that reads the state in place is the same as a selection, and the general rules cover its record.
- **A storage policy per record, chosen at submission** (scipp/essapps#49, option A). `persist=True` is that policy for unattended work; `persist` afterwards serves the rest.
- **`with client.borrow(acc, 'counts') as counts:`**, a read in the client's code without a copy, which holds back pushes until the block ends. Code in the block that waits for a later state of the same accumulator waits for itself. An interactive plot made in the block keeps the data after it. And on the service, a borrow is a copy of the whole output over the network. Likely revisited in some form, for example once jobs that read an accumulator run where it is held (Simon).

## Open questions

1. **Names.** `persist` or `save`; `freeze` or `close` (`client.close()` ends a client).
2. **Selections** (scipp/essapps#48): their form, and a look that computes, which runs a request next to the state and returns its result without a record.
3. **One accumulator per request.** Lift it once the service places a request next to the held states it reads.
4. **A driver that submits a request per push** logs a record of n rows per push, so history grows with the square of the pushes. History may store the record of a state as the accumulator's template and its number of pushes ([ADR 0004](../adr/0004-history-is-append-only-lists.md)), which needs the pushes in history again.
5. **Modified values.** In the user's process, `client.output` returns the value itself. A notebook that modifies it changes what later requests read and what `persist` writes. A shallow copy protects the dicts of coordinates and masks, not arithmetic in place.
6. **The service.** How the cap per client is set; whom the values that pending requests read are charged to once their client has ended; where a client's values are held when its work spreads over nodes.
7. **Rules that push into an accumulator** ([automatic-reduction.md](../automatic-reduction.md), open). With pushes out of history, a rule knows what it handled only from records.

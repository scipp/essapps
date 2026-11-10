# ADR 0003: An accumulator's driver adds in place, and push n+1 waits for the readers of state n

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-11-10

## Context

Spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB, which a machine may hold once but not twice ([spectroscopy](../../requirements/spectroscopy.md)).
While it grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
No earlier state of the volume is needed, only the cuts made from it.

For N pushes into a value of size V, with a read of a cut of size v after each push:

| | Peak memory | Allocated over the run | Held by the workspace after the loop if nothing is released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, a copy of the state for each read | 2V | N·V (one copy per read) | N·V |
| add in place, each read copies its cut | V + v | V + N·v | V |

The first two rows hold N·V after the loop because the workspace keeps what a client asks for ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).

The stories read a growing accumulator only through `client.output`, which makes no record (B2, D7, I1 to I3 in [user-stories.md](../user-stories.md)).
A request reads only the final sum, once the accumulator is frozen (D7).
No application opens an accumulator.

Adding a run takes about 1.5 s for LoKI, and longer for a large volume.
Applications call the client from their UI thread, or over HTTP on the service.
So a call cannot wait for a push to be added.

## Decision

[accumulators.md](../accumulators.md) states the rules in full.

**One driver, changed in place.**
An accumulator keeps one driver, made by its binding ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and each push changes it in place (the last row of the table).

**Calls return once logged**, so that no call waits for a run to be reduced (Context).
The workspace adds the pushes later, in the order logged.
A read or a freeze is of the state after the pushes logged when it is called, whether or not they have been added.
Only `client.output` waits, for its state, before it copies.

**A live accumulator is read by copying, and a request reads only a frozen one.**
`client.output(acc, name, select=...)` returns a copy of the current state, or of the part `select` names, and makes no record.
The caller may keep the copy past the next push, and accumulation goes on.
`client.freeze(acc)` ends accumulation and returns an ordinary record of the last state.
A request, a template, and a push reference that record, never the accumulator.
A reference to an accumulator is refused with a `SubmitError` that says to freeze it first.
A template or a push that referenced a state would let its binding keep the output as long as its own stage or accumulator lives, and the next push would wait that long.
Why a request does not read a live accumulator is under Alternatives.

**Push n+1 waits for the readers of state n**: the reads of state n until they have copied, and a freeze of state n while it is checked ([accumulators.md, Reading a state](../accumulators.md#reading-a-state)).
There are two reasons.
The driver only moves forward, so state n cannot be computed once push n+1 is added.
And the driver's `push` may change in place the arrays its `outputs()` returned.
So no reader sees a value change.
In Rust's terms, a reader holds a shared borrow of the driver, and a push needs a mutable one.
The framework cannot tell whether outputs share memory with the driver, so it waits for every driver.

No wait goes round in a circle, since every wait is for work that came before the waiter was logged.
A read and the record of a freeze wait for the pushes logged before them.
A push waits for the readers of the state before it, all called before the push was logged.
A request waits for the records it references, all submitted before it, and never for a push.

**Outputs computed once per state.**
All outputs of a state are computed at once, the first time a read or a freeze needs them, since the driver, not the workspace, knows how to share work between outputs.
Two reads of one state compute once.

**Checks at the first read, not at push.**
A push checks each row by the table's element type alone, and that its rows are new to the table.
When a state is first read or frozen, the workspace checks the number of rows of each table by its length constraints, so a table that needs a row has one.
So a push costs the same however many rows came before it, and a table with a minimum length of two can be filled one row per push.
Opening an accumulator validates each fixed value by its own field alone.
The framework never runs a model validator of the params model, on a template, a push, or a state: a driver never gets a whole params instance.

**A failed push stops the accumulator**, since skipping it would make state n differ from the plain request over the first n pushes.

**Freeze makes the last state a record without a copy**, so the final volume of a scan needs no second volume.
The accumulator takes no more pushes.

**Releasing returns at once.**
The pushes that a read in progress or the record of a freeze needs are still added, so a controller may release an accumulator right after its last read or its freeze.

## Alternatives considered

- **A copy of the whole state for each read.** Each read of a volume needs a second volume, where a cut needs only the part that `select=` names.
- **A push that returns once its rows are added.** Each push would wait for its run to be reduced, about 1.5 s for LoKI, in the application's UI thread, or with an HTTP request held open on the service (Context).
- **Checking the plain request at each push.** Each push would cost time in proportion to the rows before it. A push that the check refuses could never fill a table that needs two rows.
- **Computing every output at every push**, as `StreamProcessor` callers do. This costs work at every push, whether or not anyone looks.
- **A request reads a live accumulator in place**, as `client.submit(CUT, params={'volume': acc.ref('volume')})`, of the state after the pushes logged so far. The request computes next to a large state without a copy, and its result is an ordinary record with provenance, while accumulation goes on. It is not chosen for three reasons. Each such submission needs a record of the state, fixed at submission and logged with the request, which nothing keeps, and the workspace rewrites the reference to name that record. Every push waits until the workflows of the requests that read the state before it have returned, so a slow request delays the sum. And no story or application needs it (Context).
- **A borrow without a copy in the client's code** (`with client.borrow(acc, 'counts') as counts:`). Code in the block that waits for a later state waits for itself. On the service, a borrow is a copy over the network.

## Consequences

- An accumulator holds one driver, the rows of the push being added, and the outputs of the last state read until the next push. Outputs that allocate, such as a normalized volume, cost one more volume.
- A slow read delays the next addition, not the controller's `push` call. A request never delays a push.
- A failure to add reaches the controller at its next push, read, or freeze, not from `push` itself.
- A binding must not return an output that shares memory with a value it is given, or a record's output could change at the next push ([README, Components](../README.md#components)).
- The first read or freeze of each state builds the state's plain request, in time proportional to the rows pushed so far, and checks the number of rows of each table.
- A request that reads the records of two freezes reads the outputs of both drivers without a copy, so the service must hold the drivers in one process or copy all but one ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md), Open).

## Open

- **A record of a state while accumulation goes on.** A result computed from a state that is still accumulating, recorded with its provenance, is not offered. A later addition could be a call that copies the current state into an ordinary record and lets accumulation go on, with the working name `client.snapshot(acc)`. It is not implemented. It costs one copy of the outputs it copies. For a small sum, such as LoKI's I(Q), whose driver holds 0.34 MB ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)), the copy costs next to nothing. For a small output of a large driver, it costs the size of that output. For a whole spectroscopy volume of hundreds of GB, it costs a second volume, which a machine cannot hold (Context).
- **A cut that computes, chosen anew on a growing volume.** `select=` only indexes. A projection or a thick slice summed, chosen anew at each look, needs a computation next to the value under the read, without a record ([README, open question 4](../README.md#open-questions)).

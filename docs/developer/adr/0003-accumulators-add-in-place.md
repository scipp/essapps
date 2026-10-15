# ADR 0003: An accumulator's driver adds in place, and push n+1 waits for the readers of state n

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-10-09

## Context

Spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB, which a machine may hold once but not twice ([spectroscopy](../../requirements/spectroscopy.md)).
While it grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
No earlier state of the volume is needed, only the cuts made from it.

For N pushes into a value of size V, with a pin of the state after each push:

| | Peak memory | Allocated over the run | Held after the loop if nothing is released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, a copy for each pin | 2V | N·V (one copy per pin) | N·V |
| add in place, requests read the value itself | V | V | V |

The first two rows hold N·V after the loop because a client keeps what it asks for ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).

Adding a run takes about 1.5 s for LoKI, and longer for a large volume.
Applications call the client from their UI thread, or over HTTP on the service.
So a call cannot wait for a push to be added.

## Decision

README, [Stages and accumulators](../README.md#stages-and-accumulators), states the rules in full.

**One driver, changed in place.**
An accumulator keeps one driver, made by its binding ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and each push changes it in place (the last row of the table).

**Calls return once logged, and pinning does not wait for pushes to be added**, so that no call waits for a run to be reduced (Context).
The backend adds the pushes later, in the order logged.
Only `client.output` waits, for its state, before it copies.

**Push n+1 waits for the readers of state n**, the requests that read its record and the pins of it (README, [Stages and accumulators](../README.md#stages-and-accumulators), Adding waits for readers).
There are two reasons.
The driver only moves forward, so state n cannot be computed once push n+1 is added.
And the driver's `push` may change in place the arrays its `outputs()` returned.
So no reader sees a value change.
In Rust's terms, a reader holds a shared borrow of the driver, and a push needs a mutable one.
The framework cannot tell whether outputs share memory with the driver, so it waits for every driver.

No wait goes round in a circle, since every wait is for work that came before the waiter was logged.
A request waits for records submitted before it, and for pushes logged before its submission.
A push waits for the readers of the state before it, all made before the push was logged, since only the submission that made the record of a state reads that record ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

**`client.output` copies, and a request reads in place.**
`client.output(acc, ...)` returns a copy, since the caller may keep it past the next push.
A request reads the outputs of its state's record without a copy, so a cut costs no second volume.
All outputs of a state are computed at once, the first time a pin needs them, since the driver, not the backend, knows how to share work between outputs.
Two pins of one state compute once.

**Checks at pin, not at push.**
A push checks each value by its column's type alone, and that its index values are new to the table.
A state's params model, and that each required index has a row, are checked when the state is first pinned.
So a push costs the same however many rows came before it, and a table with a minimum length of two can be filled one row per push.
Opening an accumulator validates each fixed value by its own field alone, since a params-model validator that needs rows would refuse the empty tables.
So a validator that reads several values must not change one, because the driver got the values validated one by one.

**A failed push stops the accumulator**, since skipping it would make state n differ from the plain request over the first n pushes.

**Freeze makes the last state a record without a copy**, so the final volume of a scan needs no second volume.
The accumulator takes no more pushes.

**Releasing returns at once.**
The pushes up to each pinned state are still added, so a controller may release an accumulator right after its last pin.

**What may pin an accumulator.**

- A row or a template pins none. Its binding could keep the pinned output as long as its own accumulator or stage lives, and the pinned accumulator's next push would wait that long.
- A request may pin several. It reads the outputs of each driver in place, so the service must hold the drivers in one process or copy all but one ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md), Open).

Lifting the first rule later breaks no code.

## Alternatives considered

- **A copy for each pin.** Each pinned state needs a second volume.
- **A push that returns once its rows are added.** A submission that pins the accumulator would wait as long. On the service, each such call would hold an HTTP request open while a run is reduced.
- **Checking the plain request at each push.** Each push would cost time in proportion to the rows before it. A push that the check refuses could never fill a table that needs two rows.
- **Computing every output at every push**, as `StreamProcessor` callers do. This costs work at every push, whether or not anyone looks.
- **Requests read only frozen accumulators**, and only `client.output` reads the growing state. A cut or a fit kept after each push would then need a copy of the state, made by `client.output`. A request that reads the state in place does that work next to the value without a copy, and its record is an ordinary one.
- **A borrow without a copy in the client's code** (`with client.borrow(acc, 'counts') as counts:`). Code in the block that waits for a later state waits for itself. On the service, a borrow is a copy over the network.

## Consequences

- An accumulator holds one driver, the rows of the push being added, and a pinned state's outputs until the next push. Outputs that allocate, such as a normalized volume, cost one more volume.
- A slow reader delays the next addition, not the controller's `push` call.
- A failure to add reaches the controller at its next push or pin, not from `push` itself.
- A binding must not return an output that shares memory with an input, or a record's output could change at the next push (README, [Specs and bindings](../README.md#specs-and-bindings)).
- The first pin of each state checks the state's params model, in time proportional to the rows of tables on the params side.
- A request that reads two accumulators holds back the next push of both until it has run.

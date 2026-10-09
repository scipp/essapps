# ADR 0003: An accumulator adds in place to one held state, and push n+1 waits for the readers of state n

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

**One held state, changed in place.**
An accumulator keeps one held state, made by its binding ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)), and each push changes it in place (the last row of the table).

**Calls return once logged, and pinning does not wait for pushes to be added**, so that no call waits for a run to be reduced (Context).
The backend adds the pushes later, in the order logged.
Only `client.output` waits, for its state, before it copies.

**Push n+1 waits for the readers of state n**, the requests that read its record and the pins of it (README, [Stages and accumulators](../README.md#stages-and-accumulators), Adding waits for readers).
There are two reasons.
The held state only moves forward, so state n cannot be computed once push n+1 is added.
And the binding's `push` may change in place the arrays its `outputs()` returned.
So no reader sees a value change.
In Rust's terms, a reader holds a shared borrow of the held state, and a push needs a mutable one.
The framework cannot tell whether outputs share memory with the held state, so it waits for every binding.

No wait goes round in a circle, since every wait is for work that came before the waiter was logged.
A request waits for records submitted before it, and for pushes logged before its submission.
A push waits for the readers of the state before it, all made before the push was logged, since only the submission that made the record of a state reads that record ([ADR 0006](0006-the-unit-is-an-accumulating-workflow.md)).

**`client.output` copies, and a request reads in place.**
`client.output(acc, ...)` returns a copy, since the caller may keep it past the next push.
A request reads the outputs of its state's record without a copy, so a cut costs no second volume.
All outputs of a state are computed at once, the first time a pin needs them, since the binding, not the backend, knows how to share work between outputs.
Two pins of one state compute once.

**Checks at pin, not at push.**
A push checks each row by its row model alone, and a state's plain request is checked when the state is first pinned.
So a push costs the same however many rows came before it, and a table with a minimum length of two can be filled one row per push.
Opening an accumulator types each fixed value by its own field alone, since a params-model validator that needs rows would refuse the empty tables.
So a validator that reads several values must not change one, because the held state got the values typed one by one.

**A failed push stops the accumulator**, since skipping it would make state n differ from the plain request over the first n pushes.

**Freeze makes the last state a record without a copy**, so the final volume of a scan needs no second volume.
The accumulator takes no more pushes.

**Releasing returns at once.**
The pushes up to each pinned state are still added, so a driver may release an accumulator right after its last pin.

**What may pin an accumulator.**

- A row or a template pins none. Its binding could keep the pinned output as long as its own accumulator or stage lives, and the pinned accumulator's next push would wait that long.
- A request pins at most one. It reads a held state in place, so two would need both held states in one process, which the service may not offer ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md), Open).

Lifting either rule later breaks no code.

## Alternatives considered

- **A copy for each pin.** Each pinned state needs a second volume.
- **A push that returns once its rows are added.** A submission that pins the accumulator would wait as long. On the service, each such call would hold an HTTP request open while a run is reduced.
- **Checking the plain request at each push.** Each push would cost time in proportion to the rows before it. A push that the check refuses could never fill a table that needs two rows.
- **Computing every output at every push**, as `StreamProcessor` callers do. This costs work at every push, whether or not anyone looks.
- **A borrow without a copy in the client's code** (`with client.borrow(acc, 'counts') as counts:`). Code in the block that waits for a later state waits for itself. On the service, a borrow is a copy over the network.

## Consequences

- An accumulator holds one held state, the rows of the push being added, and a pinned state's outputs until the next push. Outputs that allocate, such as a normalized volume, cost one more volume.
- A slow reader delays the next addition, not the driver's `push` call.
- A failure to add reaches the driver at its next push or pin, not from `push` itself.
- A binding must not return an output that shares memory with an input, or a record's output could change at the next push (README, [Specs and bindings](../README.md#specs-and-bindings)).
- The first pin of each state checks its plain request, about 1.3 ms at 300 rows and 4 ms at 1000. Story D7 pins after every push, so its total grows with the square of the pushes, to 0.2 s for 300 and 2 s for 1000.
- Two accumulators that both still grow cannot meet in one request. A finished one is used through the record that `freeze` returns.

# ADR 0003: An accumulator's driver adds in place, and push n+1 waits for the readers of state n

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02, rewritten 2026-11-10

## Context

Spectroscopy adds each run to a fixed 4D grid of up to hundreds of GB, which a machine may hold once but not twice ([spectroscopy](../../requirements/spectroscopy.md)).
While it grows, users look at 1D or 2D cuts through it, at any time ([tensions](../../requirements/tensions.md)).
No earlier state of the volume is needed, only the cuts made from it.

For N pushes into a value of size V, with a read of a cut of size v after each push:

| | Peak memory | Allocated over the run | Held after the loop |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, copy the state for each read | 2V | N·V | N·V |
| add in place, each read copies its cut | V + v | V + N·v | V |

The first two rows hold N·V after the loop, since the workspace holds what a client asks for ([ADR 0002](0002-a-value-lives-while-something-keeps-it.md)).
Adding a run takes about 1.5 s for LoKI, and applications call the client from their UI thread.

## Decision

An accumulator holds one driver, and each push changes it in place.
A push, a freeze, and a submission return once logged, and the workspace adds the pushes later, in the order logged.
A live accumulator is read only by copying: `client.output` copies the output of the logged state, or the part `select=` picks, and creates no record.
A request reads an accumulator only through the record of its freeze, which turns the logged state into a record without a copy.
Push n+1 waits until the readers of state n are done, since the driver only moves forward and may change the arrays its outputs returned.
The outputs of a state are computed once, at its first read.
The number of rows of each table is checked at that read, not at each push.
A failed push stops the accumulator, since skipping it would make state n differ from the plain request over the first n pushes.
[accumulators.md](../accumulators.md) states the rules for a client.

## Alternatives considered

- **A copy of the whole state for each read.** Each read of a volume needs a second volume, where a cut needs only the part `select=` picks.
- **A push that returns once its rows are added.** Each push would wait about 1.5 s for LoKI, in the application's UI thread.
- **Checking the plain request at each push.** A push would cost time in proportion to the rows before it, and a table that needs two rows could never be filled one row at a time.
- **Computing every output at every push**, as `StreamProcessor` callers do. This costs work at every push, whether or not anyone looks.
- **A request reads a live accumulator in place**, as `acc.ref('volume')`. Each such request needs a record of the state that nothing holds, and a slow request would delay every later push.
- **A borrow without a copy in the client's code** (`with client.borrow(acc, 'counts') as counts:`). Code in the block that waits for a later state waits for itself, and on a service a borrow is a copy over the network.

## Consequences

- An accumulator holds one driver, the rows of the push being added, and the outputs of the driver's state once read. Outputs that allocate, such as a normalized volume, cost one more volume.
- A slow read delays the next addition, not the controller's `push` call.
- A fit or an export of a growing sum either freezes the accumulator, which ends accumulation, or submits its plain request (`client.provenance(acc).request`), which reduces every run again.
- A failure to add reaches the controller at its next push, read, or freeze.
- The first read or freeze of each state builds the state's plain request, in time proportional to the rows pushed so far. A read after each of N pushes costs time that grows as N².
- A binding must not return an output that shares memory with a value it receives ([README, Components](../README.md#components)).

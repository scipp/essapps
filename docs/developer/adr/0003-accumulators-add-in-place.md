# ADR 0003: Accumulators add in place, and a snapshot lasts until the next push

- Status: accepted
- Deciders: Simon
- Date: 2026-10-02

## Context

An accumulator combines the elements pushed into it, and a snapshot is a record of its value at one point.
Each push made a new combined value: `combine`'s operation had to return a new value and not modify its arguments.
A snapshot shared that value, so a snapshot never changed.

Combining many elements is common: LoKI sums runs, reflectometry combines angles, SXD and spectroscopy combine sample angles.
A 4D volume over sample angles takes 0.2 to 40 GB on the grids in use, and more on finer grids.
A 64 GB VISA machine cannot hold tens of GB twice.

For N pushes into a value of size V:

| | Peak memory | Allocated over the run | Held after D7's loop (a snapshot after each push), none released |
|---|---|---|---|
| a new value per push | 2V | N·V | N·V |
| add in place, copy at each snapshot | 2V | one V per snapshot | N·V |
| add in place, the snapshot shares the value | V | V | V |

D7 holds N·V in the first two rows because a client keeps the values it makes ([ADR 0002](0002-the-client-is-the-lifetime.md)).

## Decision

An accumulator may add in place, and a snapshot shares its value:

> A snapshot's value is kept until the next push into its accumulator, or until the accumulator is released.

- `combine(operation)`: `operation(total, element)` may modify `total` in place and returns the combined value. `operator.iadd` adds in place; `operator.add` still works. The first element is copied, so adding in place never changes the output it came from.
- A push first ends the snapshots taken since the previous push: from then on a request that references one is refused, and reading one raises. The push then waits until the requests that read those snapshots, accepted before it, have run. Only then does it add the element. No request waits for a push, so the wait ends.
- Releasing the accumulator, or ending its client, ends its snapshots the same way without waiting. Their value is dropped once their readers have run.
- `client.output` of a snapshot returns a copy, so a value read into a notebook does not change at the next push. Requests read the value itself.
- A push that references a snapshot is refused. It would read another accumulator's value while combining, without holding back that accumulator's next push. No story pushes a snapshot into an accumulator; accumulators meet in a request, such as a FINALIZE that reads two sums.

## Alternatives considered

- **A new value per push.** Snapshots never change, but a push needs two values at once, and every kept snapshot keeps its own.
- **Add in place, and copy at each snapshot.** Snapshots never change, and a run without snapshots needs one value. But a snapshot still needs a second value, and D7 keeps one per angle until released.
- **Copy-on-write per chunk**, as versioned chunked array stores do, to keep many states of a huge volume cheaply. No story keeps an older state of the volume after a newer push; what users keep from earlier states are small cuts, which are their own records.
- **A chain of requests instead of an accumulator**, each over the previous total and one new element. It writes one output per element, and copies quadratically when the total grows with each element, such as concatenated event lists.

## Consequences

- An accumulator holds one value, plus the element being added.
- A long request that reads a snapshot holds back the next push, so the driver decides how often it looks.
- Keeping an earlier state takes an explicit act: a request that reduces or copies the snapshot, or saving it.
- A workflow must not return an output that shares memory with a snapshot it reads, such as a slice that is a view of it. This is documented, not enforced.
- The binding decides whether a push needs a second volume-sized array. Binning an angle's events with `sc.hist` and adding the result allocates one per push. Binning into the accumulator's value does not.
- Totals that grow with each element, such as event lists for SQW files, are not served by adding in place. The stories so far read them once at the end, from elements saved as they were made.

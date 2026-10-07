# Architecture Decision Records

Lightweight records of load-bearing design decisions and their rationale.
Each ADR captures one decision; how the design works today is in [README.md](../README.md) (the API) and [system.md](../system.md) (the system).
While the design is in flux, a changed decision is rewritten in place; git history keeps the earlier text.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).
Read ADR 0006 before ADR 0003: 0006 decides what an accumulator is, and 0003, which builds on it, how it keeps its held state and what a read means.

- [ADR 0001: Keep the backend's history as an event log, apart from values](0001-history-as-an-event-log.md) (superseded by ADR 0004)
- [ADR 0002: A value lives while something keeps it, and the client keeps what it asks for](0002-the-client-is-the-lifetime.md)
- [ADR 0003: An accumulator keeps one held state, and a read pins the state at that moment](0003-accumulators-add-in-place.md)
- [ADR 0004: History is append-only lists, dropped per proposal once it is idle](0004-history-is-append-only-lists.md)
- [ADR 0005: Nothing is written unless persisted, in the user's process and on the service](0005-nothing-is-written-unless-persisted.md)
- [ADR 0006: The unit of combining runs is an accumulating workflow, not a running combination of request outputs](0006-the-unit-is-an-accumulating-workflow.md)
- [ADR 0007: Packages are split by what they depend on and where they run](0007-packages-split-by-dependencies.md)
- [ADR 0008: A request that reads an accumulator reads a record of the state's plain request](0008-a-read-of-a-state-is-a-record.md)

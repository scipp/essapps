# Architecture Decision Records

Lightweight records of load-bearing design decisions and their rationale.
Each ADR captures one decision; how the design works today is in [README.md](../README.md) (the API) and [system.md](../system.md) (the system).
While the design is in flux, a changed decision is rewritten in place; git history keeps the earlier text.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).

- [ADR 0001: Keep the backend's history as an event log, apart from values](0001-history-as-an-event-log.md) (superseded by ADR 0004)
- [ADR 0002: The client is the one lifetime of values, stages, and accumulators](0002-the-client-is-the-lifetime.md)
- [ADR 0003: Accumulators add in place, and a snapshot lasts until the next push](0003-accumulators-add-in-place.md)
- [ADR 0004: History is three append-only lists, dropped per proposal once it is idle](0004-history-is-append-only-lists.md)

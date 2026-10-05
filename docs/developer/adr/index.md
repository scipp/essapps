# Architecture Decision Records

Lightweight records of load-bearing design decisions and their rationale.
Each ADR captures one decision; how the design works today is in [README.md](../README.md) (the API) and [system.md](../system.md) (the system).
Accepted text is not rewritten: corrections and extensions land as a dated amendment section at the bottom, flagged in the status line, so that the original stays readable as the reasoning of its time.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).

- [ADR 0001: Keep the backend's history as an event log, apart from values](0001-history-as-an-event-log.md) (superseded by ADR 0004)
- [ADR 0002: The client is the one lifetime of values, stages, and accumulators](0002-the-client-is-the-lifetime.md)
- [ADR 0003: Accumulators add in place, and a snapshot lasts until the next push](0003-accumulators-add-in-place.md)
- [ADR 0004: History is three append-only lists; an event log is one way to store them](0004-history-is-append-only-lists.md)

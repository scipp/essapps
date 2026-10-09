# Architecture Decision Records

Each ADR records one design decision and its reasons.
The rules themselves are stated in [README.md](../README.md).
While the design is in flux, a changed decision is edited in place.
Git history keeps the earlier versions.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).

In reading order:

- [ADR 0004: History is append-only lists, dropped per proposal once it is idle](0004-history-is-append-only-lists.md). Records never change, and output values are not history.
- [ADR 0002: A value lives while something keeps it, and nothing is written unless persisted](0002-a-value-lives-while-something-keeps-it.md). The client keeps what it asks for, work that nothing keeps is cancelled, and only persisted records continue after a restart.
- [ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests](0006-the-unit-is-an-accumulating-workflow.md). State n gives the outputs of the plain request over the first n pushes, and pinning a state makes a record of that plain request.
- [ADR 0003: An accumulator adds in place to one held state, and push n+1 waits for the readers of state n](0003-accumulators-add-in-place.md). Pinning does not wait for pushes to be added, `client.output` copies, and freeze needs no copy.
- [ADR 0007: Packages are split by what they depend on and where they run](0007-packages-split-by-dependencies.md). A package is split off when its dependencies, where it runs, or who must depend on it differ.

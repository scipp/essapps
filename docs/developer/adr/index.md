# Architecture Decision Records

Each ADR records one design decision and its reasons.
The rules themselves are stated in [README.md](../README.md), the pages it links, and the docstrings.
While the design is in flux, a changed decision is edited in place.
Git history keeps the earlier versions.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).

In reading order:

- [ADR 0004: History is append-only lists, and a history no person holds is dropped once its proposal is idle](0004-history-is-append-only-lists.md). Records never change, and output values are not history.
- [ADR 0002: A value lives while something keeps it, and nothing is written unless persisted](0002-a-value-lives-while-something-keeps-it.md). The workspace of a proposal keeps what its clients ask for, work that nothing keeps is cancelled, and only persisted records continue after a restart.
- [ADR 0009: A record names its spec by name, version, and digest, and a record of another spec stays readable](0009-a-record-of-another-spec-stays-readable.md). An output of a record whose spec is not bound is read as plain scipp data.
- [ADR 0010: A file is identified by its catalogue identifier, or else by its path](0010-a-file-is-identified-by-its-catalogue-identifier.md). A moved folder or a second file source leaves the identity of a raw file unchanged.
- [ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests](0006-the-unit-is-an-accumulating-workflow.md). State n gives the outputs of the plain request over the first n pushes, and a freeze makes a record of the plain request of the logged state, state n for the n pushes logged so far.
- [ADR 0003: An accumulator's driver adds in place, and push n+1 waits for the readers of state n](0003-accumulators-add-in-place.md). `client.output` copies a state and makes no record, a request reads an accumulator only through the record of its freeze, and freeze needs no copy.
- [ADR 0007: Distributions are split by who depends on them and when they are released](0007-packages-split-by-dependencies.md). essspec, essapps, and essinstruments; code that needs dependencies only some users install is a subpackage behind an extra.

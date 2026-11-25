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
- [ADR 0011: A person makes the batches of a workspace](0011-a-person-makes-the-batches-of-a-workspace.md). A header per batch, a manager that lists the batches, and at most one batch that follows. Several workflows on one batch are not decided.
- [ADR 0012: A batch is a template and a table](0012-a-batch-is-a-table.md). The template holds the settings of the parameters that have no column, and its blanks are the parameter columns; the rows are one table with one value per cell, which only `put` changes, with what a person asks for or the rows that Follow adds, and which is not computed again from the source.
- [ADR 0013: Follow adds the rows that a batch has not considered](0013-follow-adds-the-rows-not-considered.md). A batch made from a source keeps the keys it has considered; a step adds the rows not among them once the source has filled them, and changes no row that the table has.
- [ADR 0014: An application for science users is a distribution of its own](0014-applications-for-science-users-are-distributions-of-their-own.md). The batch application is `essbatch` (`ess.batch`, the command `essbatch`), built on the framework `essapps`; a single reduction is a batch of one row.

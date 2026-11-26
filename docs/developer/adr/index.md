# Architecture Decision Records

For someone who wants to change a design decision.
Each ADR records one decision and its reasons.
The rules themselves are stated in [README.md](../README.md), the pages it links, and the docstrings.
While the design is in flux, a changed decision is edited in place, and git keeps the earlier versions.
Reversing or replacing a decision gets a new ADR that links back.
The format follows [scipp's ADR convention](https://github.com/scipp/scipp/tree/main/docs/development/adr).

In reading order:

- [ADR 0004: History is append-only lists, and a history no person holds is dropped once its proposal is idle](0004-history-is-append-only-lists.md). Records never change, and output values are not history.
- [ADR 0002: A value lives while something keeps it, and nothing is written unless persisted](0002-a-value-lives-while-something-keeps-it.md). The workspace of a proposal holds what its clients ask for, and work whose outputs nothing would hold is cancelled.
- [ADR 0009: A record names its spec by name, version, and digest, and a record of another spec stays readable](0009-a-record-of-another-spec-stays-readable.md). An output of a record of an unbound spec is read as plain scipp data.
- [ADR 0010: A file is identified by its catalogue identifier, or else by its path](0010-a-file-is-identified-by-its-catalogue-identifier.md). A moved folder or a second file source leaves the identity of a raw file unchanged.
- [ADR 0006: Runs are combined by an accumulator, not by combining the outputs of requests](0006-the-unit-is-an-accumulating-workflow.md). State n returns the outputs of the plain request over the first n pushes, and the workflow package's driver decides how rows combine.
- [ADR 0003: An accumulator's driver adds in place, and push n+1 waits for the readers of state n](0003-accumulators-add-in-place.md). `client.output` copies a state and creates no record, and a request reads an accumulator only through the record of its freeze.
- [ADR 0007: Distributions are split by who depends on them and when they are released](0007-packages-split-by-dependencies.md). essspec, essapps, and essinstruments; code that needs dependencies only some users install is a subpackage behind an extra.
- [ADR 0011: A person makes the batches of a workspace](0011-a-person-makes-the-batches-of-a-workspace.md). A person creates, titles and removes batches, a manager lists them, and at most one batch follows.
- [ADR 0012: A batch is a template and a table](0012-a-batch-is-a-table.md). The parameter columns are the template's blanks, and only `set_table` changes the stored table.
- [ADR 0013: Follow adds the rows that a batch has not seen](0013-follow-adds-the-rows-not-considered.md). A step adds the complete rows whose keys are not seen, and changes no row that the table has.
- [ADR 0014: An application for science users is a distribution of its own](0014-applications-for-science-users-are-distributions-of-their-own.md). The batch application is `essbatch`, built on `essapps`; a single reduction is a batch of one row.

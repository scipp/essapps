# To do

## Next steps, in order

1. Implement the core, with the API-tier story tests as the acceptance suite.
   Done: requests, records, references, labels, templates, `apply`, the dataset source's queries, sessions with stages and accumulators.
   Holders keep their definition but no computed values yet, so every call computes its full request; D7 takes 10 s for this reason.
   Holding values needs `sciline.Stage` and `sciline.Accumulator` behind the bound workflows (sciline ADR 0003); it changes no record and is what system stories S2, B2, C5, and D7 check.
2. Bindings and software tracking as pluggable implementation details (proposed to Simon, not yet decided):
   a binding is any callable that takes the parameter values and returns the outputs, with optional capabilities such as staging; bindings are registered in-process or through entry points; a record carries an opaque description of the software, made by a pluggable recorder.
3. Holders that keep values in memory, through bindings that can stage (sciline `Stage`, `Accumulator`).
4. The sub-design for provenance and publication, with its own document and stories. Batch and automatic reduction is done (`automatic-reduction.md`).

## User stories

- `user-stories.md` (API tier) and `system-stories.md` (system tier) are written against `README.md`.
- System-tier stories get code once a system document exists.

## API

The open questions are listed at the end of `README.md`.

## Implementation notes

The previous design and skeleton are on branch `architecture-sketch` (tip `b840b1f`); read a file with `git show architecture-sketch:<path>`.
Bring code over only when a story test needs it.
A review of that branch (2026-09-28) found:

- Nearly unchanged: records, references, store, data store, spec, binding, dataset sources (about 2,900 lines).
- Rework: runner and launcher (session path), the adapter (its incremental sum goes), the HTTP server and CLI.
- Obsolete: the aggregation toy specs.
- `backend.py` (1,071 lines) does everything; split it rather than rewrite it.
- `batch.py` (885 lines) holds special cases from real stories (a can measured after the sample, a dataset that gains a PID); use the test names in `batch_test.py` as a checklist when the batch sub-design starts, and decide each case rather than copying.

## Dependencies outside this repository

- `AccumulatorSpec` and generic accumulator specs such as `SUM` belong in `ess.reduce.spec`, next to `WorkflowSpec`. That needs a proposal in essreduce.
- Stages and accumulators build on sciline ADR 0003 (`Stage`, `Aggregation`, `Accumulator`), which is still proposed.

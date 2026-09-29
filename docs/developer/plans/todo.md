# To do

## Next steps, in order

1. Implement the core, with the API-tier story tests as the acceptance suite.
   Done: requests, records, references, labels, templates, `apply`, the dataset source's queries, sessions with stages and accumulators, bindings.
   A binding is staged with the fixed values and returns a callable over the blanks (`bindings.py`); a plain function computes everything on each call, and `PipelineBinding` (`pipeline.py`) cuts a sciline pipeline with `sciline.Stage`.
   Stages in a session hold what their binding computed; accumulators keep their elements but not their combined value, so every call combines all of them. D7 takes 10 s for this reason.
2. Accumulators that keep their combined value in memory, so that D7's time drops. Same pattern as stages: every binding of an accumulator spec can accumulate, and a plain function falls back to combining all elements. Uses `sciline.Accumulator` for a sciline `Aggregation`.
3. What a record says about software (proposed to Simon, not decided): a `software` mapping from name to string, filled by a pluggable recorder on the backend. Open points: the backend, not the binding, records where a binding came from (in-process or installed); a flat mapping may not be enough to recompute in a recorded environment later.
4. Registration of installed bindings through entry points, together with the first real workflow (LoKI).
5. The sub-design for provenance and publication, with its own document and stories. Batch and automatic reduction is done (`automatic-reduction.md`).

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

The old `adapter.py` also let a binding ask for a dataset as a local path or as a loaded scipp object (`Inputs`, `Wiring.resolve`). `PipelineBinding` sets whatever the dataset source's `read` returns; a real workflow such as LoKI needs a path for a NeXus file.

## Dependencies outside this repository

- `AccumulatorSpec` and generic accumulator specs such as `SUM` belong in `ess.reduce.spec`, next to `WorkflowSpec`. That needs a proposal in essreduce.
- `PipelineBinding` belongs in ess.reduce, where specs meet sciline workflows; it is the only module here that imports sciline.
- Stages and accumulators build on sciline ADR 0003 (`Stage`, `Aggregation`, `Accumulator`), which is still proposed. The venv needs sciline from `/workspace/sciline`, branch `map-reduce-outside-the-graph`.

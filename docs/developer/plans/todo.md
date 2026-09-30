# To do

## Next steps, in order

1. Implement the core, with the API-tier story tests as the acceptance suite.
   Done: requests, records, references, labels, templates, `apply`, the dataset source's queries, sessions with stages and accumulators, bindings.
   A binding is staged with the fixed values and returns a callable over the blanks (`bindings.py`); a plain function computes everything on each call, and `PipelineBinding` (`pipeline.py`) cuts a sciline pipeline with `sciline.Stage`.
   Stages live in the backend and keep what their binding computed; a request names its stage, which is checked at submission and kept until the requests through it have run.
   The backend keeps its history as an event log (`log.py`), and records are views of it (`views.py`). Accumulators live in the backend, take only finished records, keep their combined value, and a snapshot is logged as the accumulator and a count; D7 is linear (0.86 s for 1000 angles with one worker).
2. Done: history as an event log (ADR 0001, `system.md`), with README.md, the stories, and the system stories changed to match.
3. The sub-design for provenance and publication, with its own document and stories. It now also covers saving: a value that must outlive its client is saved, and D6 and E1 read outputs that only a save keeps (their Gap lines). Publishing is saving to the catalogue with the provenance flattened from the log.
4. After Simon's decisions in `handoff.md`: real workflows (LoKI, then Amor) with entry-point registration, or the system tier.

Known quadratic paths: `TriggerLoop` reads every record under a rule's label on each step (now through a view by label, still every record handled so far); a series record lists every dataset so far.

## Deferred

- Dropping values (the in-process backend keeps every value in memory) and retention of history: designed in `system.md`, not implemented. They come with a backend that stores values.

- What a record says about software. An implementation detail that holds up nothing else. Proposed shape: a `software` mapping from name to string, filled by a pluggable recorder on the backend; the backend, not the binding, records where a binding came from (in-process or installed); a flat mapping may not be enough to recompute in a recorded environment later.

## User stories

- `user-stories.md` (API tier) and `system-stories.md` (system tier) are written against `README.md`.
- System-tier stories get code as `system.md` grows.

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

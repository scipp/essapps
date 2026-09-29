# To do

## Next steps, in order

1. Simon to read the six answers added to `plans/findings-questions.md` (Q1 on what the record store is for, P1, P9, P12, P18, Part 3). Delete that file once they are settled.
2. Promote `core-api.md` to `docs/developer/README.md`; retire `plans/restart.md` and `proposals/accumulating-inputs-draft.md`.
3. Implement the core, with the API-tier story tests as the acceptance suite.
   Records, execution, references, and labels first, then datasets and the dataset source, then sessions with stages and accumulators.
4. The sub-designs for batch and automatic reduction, and for provenance and publication, each with their own document and stories.

## User stories

- `user-stories.md` (API tier) and `system-stories.md` (system tier) are written against `core-api.md`.
- System-tier stories get code once a system document exists.

## Core API proposal

The open questions are listed at the end of `proposals/core-api.md`.

## Dependencies outside this repository

- `AccumulatorSpec` and generic accumulator specs such as `SUM` belong in `ess.reduce.spec`, next to `WorkflowSpec`. That needs a proposal in essreduce.
- Stages and accumulators build on sciline ADR 0003 (`Stage`, `Aggregation`, `Accumulator`), which is still proposed.

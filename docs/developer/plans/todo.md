# To do

## Next steps, in order

1. Decide the findings at the end of `user-stories.md` and fold the decisions into `core-api.md` and the stories.
2. Promote `core-api.md` to `docs/developer/README.md`; retire `plans/restart.md` and `proposals/accumulating-inputs-draft.md`.
3. Implement, with the API-tier story tests as the acceptance suite.
   Records, execution, and references first, then stages and sessions, then accumulators.

## User stories

- `user-stories.md` (API tier) and `system-stories.md` (system tier) are written against `core-api.md`.
- System-tier stories get code once a system document exists.

## Core API proposal

Open questions in `proposals/core-api.md`:

- Generic accumulator specs: how a record tells `SUM` over one model from `SUM` over another; how an author declares that grouping does not change the result.
- Sessions: whether a holder can exist without one the user opened; how the trigger loop owns one.
- Removing a member from an accumulator.
- How a rule reaches a driving server, for example when a UI adds a rule during a beamtime.

## Dependencies outside this repository

- `AccumulatorSpec` and generic accumulator specs such as `SUM` belong in `ess.reduce.spec`, next to `WorkflowSpec`. That needs a proposal in essreduce.
- Stages and accumulators build on sciline ADR 0003 (`Stage`, `Aggregation`, `Accumulator`), which is still proposed.

# To do

## Next steps, in order

1. Decide the names (first open question below) before they spread into the stories and the code.
2. Rewrite the user stories against `proposals/core-api.md` (next section).
   Findings go back into `core-api.md`.
3. Promote `core-api.md` to `docs/developer/README.md`; retire `plans/restart.md` and `proposals/accumulating-inputs-draft.md`.
4. Implement, with the API-tier story tests as the acceptance suite.
   Records, execution, and references first, then stages and sessions, then accumulators.

## User stories

- Rewrite the stories of `core-old` against `proposals/core-api.md`.
  Toy specs must be reductions that take runs directly.
  The old stories chain `LOAD` into nearly everything; outside a session that writes the loaded events and reads them back.
  A separate record is only for a result worth keeping by itself: a beam centre, a vanadium, a contribution.
  Avoiding reloads while tuning is the stage's job.
- Split the stories into two tiers:
  - API tier: checks observe values, provenance, labels, and errors only.
  - System tier: checks observe cost, placement, persistence, and operations.
  A story with both, such as B2 (right sum, and adding costs about one run) or D7 (volume so far, and one node per run), is split.
- System-tier stories get an actor, a goal, and the property to check, but no code until the system document exists.

## Core API proposal

Open questions in `proposals/core-api.md`:

- What to call a record, and `client.run` next to a measurement "run".
- Generic accumulator specs: how a record tells `SUM` over one model from `SUM` over another; how an author declares that grouping does not change the result.
- Sessions: whether a holder can exist without one the user opened; how the trigger loop owns one.
- Removing a member from an accumulator.
- How rules reach a driving server, for example when a UI adds a rule during a beamtime.

## Dependencies outside this repository

- `AccumulatorSpec` and generic accumulator specs such as `SUM` belong in `ess.reduce.spec`, next to `WorkflowSpec`. That needs a proposal in essreduce.
- Stages and accumulators build on sciline ADR 0003 (`Stage`, `Aggregation`, `Accumulator`), which is still proposed.

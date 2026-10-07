# Persist and freeze: where things stand, and what comes next

For a fresh session. Read [persist-and-freeze.md](persist-and-freeze.md) first: it is the design. This page gives the state of the repository, the stances behind the proposal, what to grill before coding, and the order of the work.

## Where things are

- **Branch `persist-and-freeze`** (off `main` at 699f776): the proposal, this page, and the "Later" item in docs/requirements/users.md. Nothing else.
- **`main`**: the README and ADRs 0002 to 0007 describe the design the proposal replaces. In the code, a record that reads an accumulator holds `AccumulatorRef(accumulator, output, upto)` (`ess.spec.data`); `Backend.accumulated` and `Provenance.accumulated` expand such states; story D7 uses `COPY`. The service, which writes every output today, is designed and has no code.
- **Branch `state-reads-are-records`** (PR scipp/essapps#47, open, 94048a1): ADR 0008 and the code that makes a read of an accumulator a record of the state's plain request (`_state_of`, records of states logged with the submission). It also adds the replay of every record after each story test (`tests/stories/conftest.py`) and makes `ess.spec.testing.assert_close` public. The proposal keeps the mechanism; its special checks become the general read rule. Close #47 unmerged once the new PR is open.
- **PR scipp/essapps#46** (`40-accumulating-pipeline-binding`): `AccumulatingPipelineBinding`, independent of this work.
- **Issues**: scipp/essapps#48 (views) becomes selections; scipp/essapps#49 (a record and its outputs) is answered by the proposal. Comment on both when the decision lands.
- **Reviews**: three rounds by fresh reviewers on 2026-10-07. Their summaries are in `.scratch/views-review-round1.md`, `persist-review-round2.md`, and `persist-review-round3.md`, which git ignores and which may be gone. The rejected first draft is described under Alternatives in the proposal.
- **Didactic page**: https://claude.ai/artifact/NVhq7kn7y2SFxbJFPc171q, private to Simon, matching the proposal at dbbca21.

## Stances behind the proposal (Simon, 2026-10-07)

- Records form a graph, and releasing drops a value, not a node. A record is one entry in history; the costs to avoid are writing and copying.
- Avoiding waste, such as writing every output, needs no requirement of its own.
- Reviewers read the current docs as authority. Fix the defects they find, and defend the direction.
- A read without a copy in the client's own code (borrow) is likely revisited, for example once jobs run where an accumulator is held.
- A store on disk in the user's process is likely needed later (users.md, Assumed).
- Simon is not attached to labels in their current form.

## Grill first

The open questions of the proposal, and these decisions in it, which reverse or extend accepted ADRs:

1. Work that nothing keeps is cancelled; "releasing and ending stop no work" ([ADR 0002](../adr/0002-the-client-is-the-lifetime.md)) goes.
2. A submission reads only values kept for it, so a request that references a released record is refused also while that record is pending.
3. Every workflow promises that no output shares memory with an input. Should `ess.spec.testing` check it, for example with `np.shares_memory` on numpy-backed values?
4. The persist calls: `client.persist(records, *outputs)`, `submit(..., persist=True | names)`, `freeze(acc, persist=...)`; history with persist requests and `Written` events.
5. On the service: what the cap counts, dropping a value against failing its record, the lease, and an upgrade that finishes pending persisted work.
6. At a restart: pending work that nothing keeps is cancelled; records that read a held state fail.

## Order of the work

1. **Branch** from `state-reads-are-records`, to reuse #47's records of states and the replay of records, and bring over the commits of `persist-and-freeze`.
2. **Docs**, one commit per ADR:
   - Rewrite ADRs 0002, 0003, 0004, 0005, and 0008 in place. While the design is in flux, an ADR is rewritten cleanly, with no amendment sections.
   - Then the README; the requirements (tensions.md, the requirements README's "In one minute", systems.md); and the user and system stories.
   - Design docs describe the design as it is: no history, no numbered decisions.
3. **Code**, in slices, each with its story tests passing (`.venv/bin/python -m pytest -n auto` in packages/essspec and packages/essdispatch):
   1. The keepers and the read rule.
      - Check what a submission reads against what is kept for its client.
      - `client.output` reads only values kept for the caller. This fixes `_value` in backend.py, which today serves a value that only a pending reader keeps.
      - Cancel work that nothing keeps.
      - Remove ADR 0008's own checks, which the general rule now covers.
   2. `freeze`. Drop `COPY` from the toy specs. Templates and rows reference frozen records.
   3. Selections on `client.output` (#48), in the simplest form that story B4 needs.
   4. The store.
      - A store protocol with a fake in memory.
      - Persist requests and `Written` events; `persist=` and `client.persist`.
      - Reads from the store, and the restart rule.
      - The trigger loop persists what a rule names.
   5. History stores the record of a state as the accumulator and its number of pushes.

   The parts that only concern the service (cap, lease, upgrade) stay design: there is no service code.
4. **Review** before opening the PR: fresh reviewers from several angles (concurrency and lifetimes, stories, simplicity), told that the current docs are not authority.

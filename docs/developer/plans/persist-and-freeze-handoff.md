# Persist and freeze: where things stand, and what comes next

For a fresh session. Read [persist-and-freeze.md](persist-and-freeze.md) first: it is the design. This page gives the state of the repository, the stances behind the proposal, what to grill before coding, and the order of the work.

## Where things are

- **Branch `persist-and-freeze`**, on top of #47's 94048a1: the proposal, this page, and the "Later" item in docs/requirements/users.md, then the work of PR 1 below.
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

## Decided (Simon, 2026-10-07)

1. Work that nothing keeps is cancelled; "releasing and ending stop no work" goes. A running workflow cannot be interrupted: its record is cancelled at once and its outputs dropped, so cancelling frees memory, not CPU. ADR 0002 says so.
2. The read rule is per client. A request that references a released record is refused also while that record is pending. Another client, also in the same process, references a value only once it is persisted.
3. `ess.spec.testing` checks that no output shares memory with an input: a helper over numpy arrays and scipp variables, data arrays, and datasets (data, variances, coords, masks) with `np.shares_memory`, called from `check_one_row` and `check_caching`. Values not backed by numpy are skipped.
4. Names: `persist` and `freeze`.
5. Persist calls as in the proposal; they are easy to change once stories use them (open question 1 of the proposal).
6. Selections: `client.output(what, name, select={'dim': index or slice})`, by dimension name, on records and accumulators. A look that computes is later and separate.
7. Each submission that reads an accumulator makes its own record of the state (as #47 does).
8. The store in the first release is a fake in memory, passed by tests. Its format stays open.
9. Values modified in place stay an open question; the docs warn.
10. History logs pushes, and the record of a state as the accumulator and its number of pushes (the last code slice); details decided while implementing.
11. A record persisted at submission finishes once written: a failed write fails the record with the write's reason, so D2's morning check and the trigger loop see it as any failed record. A failed write of `client.persist` fails only that persist request; the client still keeps the value and can ask again. An output is persisted while its write is done or pending.
12. An upgrade with persisted work running is open (ADR 0005, Open; #27): the old instance finishing it conflicts with the one-backend history lock.
13. A stage keeps the values its template references until it is released.
14. Closing `local()` waits until the pending records with a persist request are written.
15. Closing a client waits for the writes of its `client.persist` requests and raises naming each that failed (review of #52).
16. A rule's `persist` names outputs or is `None` for every one; a rule cannot turn persisting off.
17. A client reads the values it keeps from memory and the others from the store, so it never gets another client's value; a workflow reads its inputs from memory while the backend holds them.

## Order of the work

Three stacked PRs, each with its story tests passing (`.venv/bin/python -m pytest -n auto` in packages/essspec and packages/essdispatch). Stories not yet implemented stay strict xfails.

1. **Docs and the keepers** (branch `persist-and-freeze`, rebased onto #47's 94048a1; replaces #47, closes #49).
   - Docs, one commit per ADR: rewrite ADRs 0002, 0003, 0004, 0005, and 0008 in place, with no amendment sections. Then the README; the requirements (tensions.md, the requirements README's "In one minute", systems.md); and the user and system stories. Design docs describe the design as it is: no history, no numbered decisions.
   - Code:
     - Check what a submission reads against what is kept for its client.
     - `client.output` reads only values kept for the caller. This fixes `_value` in backend.py, which today serves a value that only a pending reader keeps.
     - Cancel work that nothing keeps.
     - Remove ADR 0008's own checks, which the general rule now covers.
     - The shared-memory check in `ess.spec.testing`; `ANGLE` and `CONTRIBUTE` return copies.
2. **`freeze` and selections** (closes #48). Drop `COPY` from the toy specs. Templates and rows reference frozen records. `select=` on `client.output`; B4 and D7 use it.
3. **The store and persist.**
   - A store protocol with a fake in memory.
   - Persist requests and `Written` events; `persist=` and `client.persist`.
   - Reads from the store, and the restart rule.
   - The trigger loop persists what a rule names.
   - History stores the record of a state as the accumulator and its number of pushes. The restart rule needs this first: today the log does not mark a record as a record of a state, so after a restart the backend cannot tell which pending records read a held state.

The parts that only concern the service (cap, lease, upgrade) stay design: there is no service code.
Before opening each PR: fresh reviewers from several angles (concurrency and lifetimes, stories, simplicity), told that the current docs are not authority.

Issues to update when the PRs open: #47 closed unmerged (PR 1); #49 closed (PR 1); #48 closed (PR 2); #23 (saving) and #27 (cap per client, lease) commented, since persist and the cap per client answer parts of them; #25 commented (looking after each push).

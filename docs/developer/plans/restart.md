# Handoff: restart the design and the skeleton from a small core

This plan is for the session that builds branch `core`. Delete it once the plan is done.

## Why

The design on branch `architecture-sketch` has grown to about 5,000 lines of design docs, 7,800 lines of source, 7,200 lines of tests, and 57 glossary terms, in 154 commits over 24 days.
Simon cannot review all of it well.
We rebuild from a small core that he reviews piece by piece, and bring ideas and code over from `architecture-sketch` only when a story needs them.

## Where things are

| What | Where |
|---|---|
| This branch | `core`, from `main` (only `docs/developer/scoping.md`), worktree `/workspace/essapps-core` |
| The previous design and skeleton | branch `architecture-sketch` (the default branch on GitHub), tip `b840b1f` |
| The accumulator proposal | `docs/developer/proposals/accumulators.md` on `architecture-sketch` |
| The previous user stories and story tests | `docs/developer/user-stories.md`, `packages/essapps/tests/stories/` on `architecture-sketch` |

Take a file over with `git checkout architecture-sketch -- <path>`, then prune it; or read it with `git show architecture-sketch:<path>`.
Another session may be working in `/workspace/essapps`; do not switch branches there.

## How Simon wants to work

- He reviews **examples and concise code** better than prose. Every concept arrives with the user code that uses it.
- The unit of review is **story, code, test**:
  - `user-stories.md`: each story has an actor and a goal, the client code that does it, and its checks. No "Outcome" paragraphs that explain mechanisms.
  - one story test per story, running that code; a story the code cannot do yet is a strict xfail.
  - `README.md`: one or two pages, the handful of concepts, each with a code example of three to five lines, and the invariants. No alternatives, no history, no decision numbers.
- **The first PR has no implementation**: README and user stories with code, so that the API is reviewed on paper.
- Later PRs are a few hundred lines each and bring code over until the next group of story tests passes.
- Delegate mechanical work (porting a module, turning a story into a test) to subagents, Sonnet for mechanical edits and Opus where design judgment is needed; keep design and review in the main session.
- Follow the writing style in Simon's CLAUDE.md and the memory note on design docs without history.

## Decisions to carry over

These were settled on 2026-09-28 (see the commit messages on `architecture-sketch` from `ab7ca9a` to `b840b1f`):

- A request is plain data: spec, every parameter value (defaults filled at submit), outputs. A record is the request plus what happened.
- Data is named by reference: an output of a record, or a dataset identity. Stand-ins (run numbers, and later `Current`) resolve at submission.
- A pending output may be an input: the only scheduling mechanism.
- A label is a name over a sequence of records; its latest record is its value.
- `Template` splits: `Stage` is plain data (spec, params, blanks, outputs), the counterpart of `sciline.Stage`; `Template` is a stored, versioned stage that batches and rules fill.
- **Nothing is held unless the user creates a holder.** `client.hold(stage)` and `client.release`, when sessions come; no `vary` hint and no implicit cache of stages.
- A sum over runs in one request is a list parameter, **computed flat**, holding nothing afterwards. The adapter's incremental part (`_Sum`, comparing lists, tracking which varied parameters the contributions read) does not come over.
- A cut that crosses a record boundary is made by the workflow author, where the graph is known; the backend never needs the graph.

Deferred, not rejected: the accumulator proposal (accumulator specs as operations, `Held`/`Flat`/`Tree`, push log, `Current`, `Into`).
Simon says spectroscopy needs fan-out across processes and wants accumulating outputs of one spec into another unplanned spec to stay possible.
Add a spectroscopy fan-out story now, as a strict xfail, and keep the core compatible with the proposal: records, references, stand-ins, pending outputs, and labels are what it builds on.

## Plan

1. **README and user stories with code.** Carry over the stories of `architecture-sketch`, rewritten with client code and checks; add the spectroscopy fan-out story. One PR, for Simon's review.
2. **Records and execution.** From `architecture-sketch`: `spec.py`, `binding.py`, `records.py` (Template split into Stage and Template, `Group` without `vary`), `store.py`, `datastore.py`, `backend.py` without `vary` (split scheduling out if that stays simple), `runner.py` throwaway path only, `launcher.py` (subprocess, and in-process for tests), `client.py`. Story tests S1, S3, S4, S8, C1, D3 to D5, F1, F2.
3. **Adapter, flat only.** `keys`, `targets`, `resolve`, and `aggregations` over a list, computed flat. Story tests S5, S6; B2 with its "adding is fast" check as a strict xfail.
4. **Dataset sources and rules.** `sources.py`, then Template, Lookup (fixed fills and `Nearest`), Selector, Rule, `apply`, trigger loop, `batch_table`. Story tests S7, D1, E2, E4.
5. **Series and rules that follow rules**, for E1, C4, E3. Use the test names in `batch_test.py` on `architecture-sketch` as a checklist of edge cases; decide each with Simon rather than copying.
6. **Real workflows**: LoKI, then Amor.

Left out until a story needs it: sessions and held stages, views beyond whole outputs, the HTTP server and CLI, `reprocess`, `shadowed`, `Bound`, `Complete`, exclusions, `RetryPolicy`, local files identified by checksum, the notebooks, the accumulator proposal.

## Findings of the review that led here

A reviewer read `architecture-sketch` on 2026-09-28:

- Survive nearly unchanged: records, references, store, data store, spec, binding, sources (about 2,900 lines).
- Need rework: runner and launcher (session path), adapter (`_Sum` goes), HTTP and CLI (new calls).
- Mostly obsolete: the aggregation toy specs and `aggregation.md`.
- `backend.py` (1,071 lines) is clean but does everything; split it rather than rewrite it.
- `batch.py` (885 lines) is the hardest to review; its special cases come from real stories (a can measured after the sample, a dataset that gains a PID), so a rewrite would grow back.
- The aggregation design changed four times in ten days; do not build the core around it.
- The 46 story tests (32 passing, 14 strict xfails) use only the client API and toy workflows; they are the most portable asset.

## To check with Simon

- Whether phase 1 needs the HTTP transport and the CLI; the reviewer read the roadmap as saying no, which was not verified.
- Whether `Current` comes in with labels in step 2 or waits for accumulators.
- A venv in this worktree with the `[test]` extra once code arrives; the memory note on the conda base environment applies.

# Handoff

A living document for the next session on branch `event-log`. Read it first, then `docs/developer/README.md`, then `plans/todo.md`. At the start of a session, ask Simon the questions under "Decisions to ask Simon"; fold his answers into the README or `todo.md` and remove them here. Update this file at the end of each session.

## Where things are

| What | Where |
|---|---|
| Branch | `event-log`, off `main`, checked out in `/workspace/essapps`; `main` holds the reviewed-in-conversation `core` (merged 2026-09-30) |
| The API design | `docs/developer/README.md` (was `proposals/core-api.md`) |
| API-tier stories | `docs/developer/user-stories.md`: 45 stories as client code; an "Open" section at the end |
| The system design | `docs/developer/system.md`: history as an append-only event log, records as views, which rules keep a value, retention; the in-process backend implements the history and holders |
| Decisions | `docs/developer/adr/`: ADR 0001 (history as an event log, apart from values); new load-bearing decisions get an ADR in the esslivedata format, short and without implementation detail |
| Simon's review notes | `simon-notes.md` in the checkout, not in git: questions on the README and stories. B6 (is a long-lived record store core, is it replicating SciCat) led to the event log; with labels keeping no values, the SciCat part is answered by saving. A2, A5, B2 to B5, and the README batch example are not yet answered |
| System-tier stories | `docs/developer/system-stories.md`: actor, goal, property; no code yet |
| Sub-design: batch and automatic reduction | `docs/developer/automatic-reduction.md` |
| To-do list | `docs/developer/plans/todo.md` |
| Scoping | `docs/developer/scoping.md` |
| Code | `packages/essapps/src/ess/apps/`: `records.py`, `log.py`, `views.py`, `backend.py`, `client.py`, `bindings.py`, `pipeline.py`, `datasets.py`, `batch.py`, `accumulators.py`, `sessions.py`, `rules.py`, `testing.py` (about 2,000 lines) |
| Tests | `packages/essapps/tests/`: `backend_test.py`, `log_test.py`, `sessions_test.py`, `pipeline_test.py`, `stories/*_test.py` (one test per API-tier story), toy specs and fixtures in `stories/conftest.py` |
| The previous attempt of this session's work | branch `core-old` (README with 8 terms, stories with the old vocabulary, the accumulating-inputs draft) |
| The previous design and skeleton | branch `architecture-sketch`, tip `b840b1f`; see "Implementation notes" in `todo.md` |
| sciline ADR 0003 (Stage, Aggregation, Accumulator) | `/workspace/sciline`, branch `map-reduce-outside-the-graph`; `docs/developer/adr/0003-*.md` and `docs/developer/architecture-and-design/map-reduce-outside-the-graph.md` |
| ess.reduce workflow spec (ADR 0001) | `/workspace/ess`, branch `653-minimal-workflow-spec`, `packages/essreduce/src/ess/reduce/spec/` |

Environment: `.venv` in the worktree, made with `python3 -m venv --system-site-packages .venv`, then `pip install -e /workspace/ess/packages/essreduce --no-deps` and `pip install -e 'packages/essapps[test]' --no-deps`.
Run tests from `packages/essapps`: `../../.venv/bin/python -m pytest tests -q -n auto` (83 pass, 11 strict xfails, about 4 s).
Lint: `ruff check . && ruff format .` (ruff from conda base; no pre-commit hooks are installed in this repository).

## How Simon wants to work

- He reviews examples and concise code better than prose. Every concept arrives with the user code that uses it.
- Design documents describe the design as it is: no history, no decision numbers, no references to review passes (memory note "design-docs-no-history").
- Separate the API ("the API we want") from the system (how things are stored, resolved, uploaded, placed). Stories come in two tiers for the same reason.
- He answers questions best in a file with a `> Simon:` line per question and enough context per item (story, code, what is missing, proposal). Too little context per item made him unable to answer (this happened once).
- When he says "continue on your own", decide details yourself and stop on major findings.
- Delegate mechanical work to subagents (Sonnet for mechanical, Opus for judgment); keep design and review in the main session. An independent `senior-engineer-review` of new code found 20 real issues once; do that again after larger steps.

## The vocabulary (settled this session)

| Kind | Terms |
|---|---|
| plain data | spec, request, template, reference, selector (and rule, in the sub-design) |
| durable | record, label |
| queries | dataset source, through `client.datasets` |
| holders, in a session | stage, accumulator |
| policy | driver |

The essentials, all in the README with code:

- A spec is an `ess.reduce.spec.WorkflowSpec`: params model and outputs model, both pydantic. An output field fulfils a params field when their `DataField`s agree.
- `client.submit` (pending) and `client.compute` (submit and wait) take a spec with values, a stage with values, an accumulator, a request, a list, or a dict, and return the same shape. Under a label, a dict's keys become members. Label and member are given at submission; they are not part of the request.
- A record holds the request with every value filled in; statuses `pending`, `completed`, `failed`, `cancelled`; a finished record never changes. Records are history, kept for a retention period, and outlive sessions; an output's value is kept only while a pending request, a client's record handle, or a holder holds it, or once saved (system.md); long-term provenance is what `publish` puts in the catalogue. Provenance stops at datasets (what lies behind a dataset belongs to its source).
- Every connection between requests is a reference; a reference to a pending record is a valid input, which is the only scheduling mechanism.
- Datasets are named by `dataset(run=/path=/pid=)`; the record names the identity. The backend resolves names and reads data through its dataset source; drivers and forms list, watch, and read metadata through `client.datasets`, which shows only the datasets the client's proposal may read. Selectors match raw datasets unless they name another kind.
- An accumulator spec (`AccumulatorSpec(name=, version=, element=)`) takes one list per element field and outputs the element model, so a combined value can be pushed again. A package derives CONTRIBUTE, the accumulator spec, and FINALIZE from its sciline `Aggregation`. `SUM.of(element)` is a generic one.
- Holders live in a session (`client.session(where=...)`): a stage holds a template; an accumulator holds pushed elements. A stage never changes what a record says; a snapshot's record names its accumulator and how many elements it covers.
- A driver is code that uses the client over time (notebook, application, trigger loop in a driving server); drivers never run in the backend. A tree of partial sums over a known list is how the backend may execute one accumulator request, not a driver.
- Several ways to write a sum are accepted: a spec with a list parameter, a chain of requests, the same chain through holders.

Decisions from Simon's comments this session, beyond the above: no list-valued outputs; templates and rules are unnamed plain data with no store, and records do not name templates; publishing a result from a notebook-bound workflow is not refused (records say what ran; a hosted backend runs only installed workflows; `local(bind=...)` may bind notebook workflows); recompute in a recorded environment and publishing corrections (`supersedes`) are deferred; removing a dataset is deferred; session cleanup, record retention, and the store's form are system matters.

## Implementation state

Done, with story tests passing:

- In-process backend: atomic submission (dataset names resolved outside the lock; unknown parameters, references to missing or failed or foreign-proposal records, element references, and misfitting fields refused with a message naming the request and the field); scheduling on pending references with failures passed to dependents through a worklist; cancel ends every unfinished record; outputs checked against the spec's outputs model.
- Client: shapes, placeholders renumbered to `@<index>` before the backend sees them, labels, members, `records(since=, until=)`, provenance with `.datasets()` and `.records()`.
- Templates, `apply`, lookups (`LastBefore`), rules with series, `TriggerLoop` (`step`, `status`, `run`), reading what it handled from the records under the rule's label.
- Bindings (`bindings.py`, `pipeline.py`): every binding has `stage(fixed, blanks)`; a plain function computes everything on each call.
- Sessions with stages and accumulators. Stages live in the backend and keep what their binding computed; a request names its stage, which the backend checks at submission (open, same proposal, same spec) and keeps until the requests through it have run.
- The event log (`log.py`, `views.py`, `system.md`): three events (`submitted`, `finished`, `pushed`), each checked, appended, and applied to the views (`Views.apply`); what is not history (sessions and holders, outputs, staged callables, held values, what waits for what) stays in the backend; a backend given an existing log (a JSON-lines file) replays it and runs what was pending. A failed write leaves the file as it was. Output values are not in the log and live in memory.
- Accumulators live in the backend. A push is checked when made; a snapshot's record is `Snapshot(spec, accumulator, upto)`, not a request, and provenance reads its elements through `Backend.inputs`. A push takes only a finished record; the binding must have `accumulator()` (as `combine` does) and combines it at the push, and a snapshot completes at submission. Drivers wait for many records with `client.as_completed`. D7: 0.86 s for 1000 angles with one worker (5.6 s before), 1000 stored element references (501,500 before).

Strict xfails and what they need: C2, E3, F1, F2, F4 (publication, provenance `.software`, recompute, supersedes); G1 (grants across proposals); G2 (`local(bind=...)`, publish, software mark); B4 (views; form open); B5 (a notebook crash is not simulated); A1 (local folders by path); A4 (removing a dataset, deferred).

Provisional choices in the code, easy to change: generic accumulator specs are named `sum[Counts]` (README open question 1); `apply` and rules key members by `run` unless told otherwise; order means run order.

## Decisions to ask Simon

Each affects more than one area. Ask them in this order at the start of the next session.

### A. Still open from the accumulator questions

**A7. The README guarantee on values passed in memory changed in `795651b`.**
It said "a value passed in memory is a copy of the referenced output", but nothing copied, and a stage returns the same object in every record for what does not depend on its blanks. It now says the value is the referenced output itself, so a workflow must not modify its inputs; the binding contract says the same. Copying would cancel the saving a stage exists for.

> Simon:

### B. Earlier questions (asked on 2026-09-29, deferred)

**1. What a binding receives in place of a reference, and what `None` means.**
Now a binding gets whatever `DatasetSource.read` returns, and outputs of other records as objects in memory; that works only for the toy specs.
LoKI's pipeline needs a NeXus file path, and a hosted backend keeps outputs as files.
The old skeleton let each field ask for a form (`Inputs.path` or `Inputs.array`, `Wiring.resolve` in `adapter.py` on `architecture-sketch`).
Proposal, one rule with nothing to configure per field:

```python
# dataset reference              -> a local path; loading is the workflow's job (sciline's NeXus loaders take a filename)
# output reference, Format.SCIPP -> the loaded scipp object; the framework loads it by the output's declared format
```

Also `None`: the backend fills every default at submission, so a binding cannot tell "not given" from "given as `None`". `PipelineBinding` sets every value on the pipeline, so `beam_centre=None` replaces the pipeline's beam-centre provider. Leaving the key unset for `None` is wrong in general: esssans sets the direct beam to `None` to skip that correction. Options: `PipelineBinding` takes the fields for which `None` means "leave it to the pipeline"; or the spec marks such fields; or submission keeps "not given" apart from `None`. Recommendation: the first, since it keeps the question inside the sciline adapter.

> Simon:

**2. What comes after the accumulators: real workflows, the system tier, or provenance and publication.**
The system tier is the store, a hosted backend with `connect(url)`, sessions placed in a backend process, and `client.datasets` served over the connection (`watch` needs a streaming or long-poll endpoint); README open question 2 (session placement, the trigger loop owning a session) belongs to it.
The in-process backend hides all of it.
Proposal: LoKI first (then Amor), which tests the binding contract and decision 2 on real data before infrastructure is built around them; entry-point registration comes with it.

> Simon:

**3. How a generic accumulator spec is named in a record** (README open question 1), and how an author declares that grouping does not change the result.
Needed before `AccumulatorSpec` is proposed to ess.reduce; the provisional `sum[Counts]` works until then. With the event log, snapshots of an accumulator no longer need grouping independence; a tree of partial sums over a plain request still does.

> Simon:

## Next steps

In `todo.md`.

Open questions in the README: generic accumulator spec identity and the declaration that grouping does not change a result; sessions (a holder without a user-opened session, the trigger loop owning one, the placement argument); removing a member from an accumulator; labels and members are tentative.

## Gotchas

- The hook `/workspace/dotclaude/hooks/prevent-git-add-all.py` blocks any Bash command in which `git commit` is followed by a segment like `-workspace-...` (it reads as `-a`), including a scratchpad path given to `-F`. Commit with the message on stdin: `git commit -F - <<'EOF' ... EOF`. A subagent once worked around it silently and also changed the attribution line; check subagent commits.
- The test file name hook requires `*_test.py` or `conftest.py` under `tests/`; toy specs therefore live in `tests/stories/conftest.py` and are imported with `from .conftest import ...`.
- `xfail_strict = true`: a story that starts passing fails the suite until its mark is removed. When lifting marks, remove stale `# ruff: noqa: F821` lines.
- scipp arrays compare elementwise; stories compare `.values.tolist()` or use `sc.identical`.
- Background subagents stop when the session ends; resume them with SendMessage to their id, and check their output files before assuming work landed.
- `docs/developer/plans/findings-questions.md` (Simon's answers on the story findings) was removed after folding; it is in history at `1802651`.

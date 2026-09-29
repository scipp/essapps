# Handoff: after implementing the core (2026-09-29)

This is the handoff for the next session on branch `core`. Read it first, then `docs/developer/README.md`, then `plans/todo.md`. Delete this file once its pending question is settled and folded into `todo.md`.

## Where things are

| What | Where |
|---|---|
| Branch and worktree | `core` in `/workspace/essapps-core` (not pushed since `9c8bcbe`; local commits up to `a2d5574` and this file) |
| The API design | `docs/developer/README.md` (was `proposals/core-api.md`) |
| API-tier stories | `docs/developer/user-stories.md`: 47 stories as client code; an "Open" section at the end |
| System-tier stories | `docs/developer/system-stories.md`: actor, goal, property; no code yet |
| Sub-design: batch and automatic reduction | `docs/developer/automatic-reduction.md` |
| To-do list | `docs/developer/plans/todo.md` |
| Scoping | `docs/developer/scoping.md` |
| Code | `packages/essapps/src/ess/apps/`: `records.py`, `backend.py`, `client.py`, `datasets.py`, `batch.py`, `accumulators.py`, `sessions.py`, `rules.py`, `testing.py` (about 1,100 lines) |
| Tests | `packages/essapps/tests/`: `backend_test.py`, `sessions_test.py`, `stories/*_test.py` (one test per API-tier story), toy specs and fixtures in `stories/conftest.py` |
| The previous attempt of this session's work | branch `core-old` (README with 8 terms, stories with the old vocabulary, the accumulating-inputs draft) |
| The previous design and skeleton | branch `architecture-sketch`, tip `b840b1f`; see "Implementation notes" in `todo.md` |
| sciline ADR 0003 (Stage, Aggregation, Accumulator) | `/workspace/sciline`, branch `map-reduce-outside-the-graph`; `docs/developer/adr/0003-*.md` and `docs/developer/architecture-and-design/map-reduce-outside-the-graph.md` |
| ess.reduce workflow spec (ADR 0001) | `/workspace/ess`, branch `653-minimal-workflow-spec`, `packages/essreduce/src/ess/reduce/spec/` |

Environment: `.venv` in the worktree, made with `python3 -m venv --system-site-packages .venv`, then `pip install -e /workspace/ess/packages/essreduce --no-deps` and `pip install -e 'packages/essapps[test]' --no-deps`.
Run tests from `packages/essapps`: `../../.venv/bin/python -m pytest tests -q -n auto` (50 pass, 11 strict xfails, about 11 s, of which story D7 takes 10 s).
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
| queries | dataset source |
| holders, in a session | stage, accumulator |
| policy | driver |

The essentials, all in the README with code:

- A spec is an `ess.reduce.spec.WorkflowSpec`: params model and outputs model, both pydantic. An output field fulfils a params field when their `DataField`s agree.
- `client.submit` (pending) and `client.compute` (submit and wait) take a spec with values, a stage with values, an accumulator, a request, a list, or a dict, and return the same shape. Under a label, a dict's keys become members. Label and member are given at submission; they are not part of the request.
- A record holds the request with every value filled in; statuses `pending`, `completed`, `failed`, `cancelled`; a finished record never changes. Records are medium-term working state and outlive sessions; long-term provenance is what `publish` puts in the catalogue. Provenance stops at datasets (what lies behind a dataset belongs to its source).
- Every connection between requests is a reference; a reference to a pending record is a valid input, which is the only scheduling mechanism.
- Datasets are named by `dataset(run=/path=/pid=)`; the record names the identity. A dataset source (separate from the client, injected into drivers and forms) lists, watches, and reads metadata. Selectors match raw datasets unless they name another kind.
- An accumulator spec (`AccumulatorSpec(name=, version=, element=)`) takes one list per element field and outputs the element model, so a combined value can be pushed again. A package derives CONTRIBUTE, the accumulator spec, and FINALIZE from its sciline `Aggregation`. `SUM.of(element)` is a generic one.
- Holders live in a session (`client.session(where=...)`): a stage holds a template; an accumulator holds pushed elements. A holder never changes what a record says.
- A driver is code that uses the client over time (notebook, application, trigger loop in a driving server); drivers never run in the backend. A tree of partial sums over a known list is how the backend may execute one accumulator request, not a driver.
- Several ways to write a sum are accepted: a spec with a list parameter, a chain of requests, the same chain through holders.

Decisions from Simon's comments this session, beyond the above: no list-valued outputs; templates and rules are unnamed plain data with no store, and records do not name templates; publishing a result from a notebook-bound workflow is not refused (records say what ran; a hosted backend runs only installed workflows; `local(bind=...)` may bind notebook workflows); recompute in a recorded environment and publishing corrections (`supersedes`) are deferred; removing a dataset is deferred; session cleanup, record retention, and the store's form are system matters.

## Implementation state

Done, with story tests passing:

- In-process backend: atomic submission (dataset names resolved outside the lock; unknown parameters, references to missing or failed or foreign-proposal records, element references, and misfitting fields refused with a message naming the request and the field); scheduling on pending references with failures passed to dependents through a worklist; cancel ends every unfinished record; outputs checked against the spec's outputs model.
- Client: shapes, placeholders renumbered to `@<index>` before the backend sees them, labels, members, `records(since=, until=)`, provenance with `.datasets()` and `.records()`.
- Templates, `apply`, lookups (`LastBefore`), rules with series, `TriggerLoop` (`step`, `status`, `run`), reading what it handled from the records under the rule's label.
- Sessions with stages and accumulators, which hold their definition but no computed values: every call computes the full request. This is why D7 is slow and quadratic.

Strict xfails and what they need: C2, E3, F1, F2, F4 (publication, provenance `.software`, recompute, supersedes); G1 (grants across proposals); G2 (`local(bind=...)`, publish, software mark); B4 (views; form open); B5 (a notebook crash is not simulated); A1 (local folders by path); A4 (removing a dataset, deferred).

Provisional choices in the code, easy to change: generic accumulator specs are named `sum[Counts]` (README open question 1); `apply` and rules key members by `run` unless told otherwise; order means run order.

## Pending question to Simon (last message of the session)

Simon wrote, while I was working: "I feel both should actually be implementation details, abstracted away -- a spec does not necessarily need to wrap a sciline workflow, it could just be a Python function. Can this be a pluggable system, or something generic where the bindings can be registered? Similar for the software question -- we'd want something generic that hides away any details/decisions about how different kinds of environments are tracked."

"Both" refers to my note that the next candidates were how a sciline workflow is bound to a spec, and what a record says about software. My answer, awaiting his reply:

> Yes, both can be pluggable, and the core already treats bindings that way.
>
> **Bindings.** A binding is any callable that takes the parameter values and returns the outputs, so a plain Python function already qualifies; that is how the toy specs are bound.
> - Registration: in-process through `bind=`, as now. For installed packages, through entry points, one group for specs and one for bindings, as ess.reduce ADR 0001 suggests.
> - Optional capabilities: a binding may offer extra abilities that the backend asks for. `stage(fixed, blanks)` returns a callable that computes only what depends on the blanks. A plain function falls back to computing everything, so it still gives the same records, only slower. A sciline adapter, which could live in ess.reduce, would use `sciline.Stage`. The backend never imports sciline.
>
> **Software.** A record carries a `software` mapping from name to string that the framework stores and compares but never interprets.
> - A pluggable recorder on the backend fills it: for example package versions from `importlib.metadata`, a conda environment hash, or a container image digest.
> - A binding may add entries such as the spec's `code_revision`, or that it was bound in the notebook. Provenance and publication show whatever is there.
>
> If that shape is right, I'll build it next. After that come holders that keep values in memory through sciline, then the provenance and publication sub-design.

## Next steps (also in todo.md)

1. Once Simon agrees: the binding protocol and registry (in-process `bind=` and entry points), and the pluggable software recorder with `record.software`. Keep sciline out of the backend. Unblocks G2 partly and provenance `.software`.
2. Holders that keep values: a binding capability to stage (and for accumulator specs, to accumulate in memory), used by sessions; the records stay the same. Needs sciline from the `map-reduce-outside-the-graph` branch installed into the venv (`pip install -e /workspace/sciline --no-deps`). Check D7's time drops.
3. The sub-design for provenance and publication: its own document and stories (F1 to F4, C2, E3, G2), including what `publish` needs from the backend and a fake publisher.
4. Later: grants (G1), views (B4, form waits for plotting), a hosted backend and `connect(url)`, the system document and code for system stories, real workflows (LoKI, then Amor).

Open questions in the README: generic accumulator spec identity and the declaration that grouping does not change a result; sessions (a holder without a user-opened session, the trigger loop owning one, the placement argument); removing a member from an accumulator; where a notebook's dataset source comes from; labels and members are tentative.

## Gotchas

- The hook `/workspace/dotclaude/hooks/prevent-git-add-all.py` blocks any Bash command in which `git commit` is followed by a segment like `-workspace-...` (it reads as `-a`), including a scratchpad path given to `-F`. Commit with the message on stdin: `git commit -F - <<'EOF' ... EOF`. A subagent once worked around it silently and also changed the attribution line; check subagent commits.
- The test file name hook requires `*_test.py` or `conftest.py` under `tests/`; toy specs therefore live in `tests/stories/conftest.py` and are imported with `from .conftest import ...`.
- `xfail_strict = true`: a story that starts passing fails the suite until its mark is removed. When lifting marks, remove stale `# ruff: noqa: F821` lines.
- scipp arrays compare elementwise; stories compare `.values.tolist()` or use `sc.identical`.
- Background subagents stop when the session ends; resume them with SendMessage to their id, and check their output files before assuming work landed.
- `docs/developer/plans/findings-questions.md` (Simon's answers on the story findings) was removed after folding; it is in history at `1802651`.

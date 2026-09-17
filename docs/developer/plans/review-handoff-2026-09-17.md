# Review handoff, 2026-09-17

Tracking issue: scipp/essapps#1. This document is what a fresh session gets
together with review findings of the form "I looked at item X, here is what I
think". It says where the skeleton stands, what each review item is meant to
answer, what is already decided, and how to run things.

## State

Branch `architecture-sketch`, unpushed on 2026-09-17, with the skeleton under
`packages/essapps` (import `ess.apps`). It depends on scipp/ess#690 through the
PR branch (`essreduce @ git+...@653-minimal-workflow-spec`); the venv has
`/workspace/ess/packages/essreduce` installed editable. `ess.apps.spec` holds
only what the sketch adds to that spec. 140 tests pass, run with

    .venv/bin/python -m pytest packages/essapps/tests -q -p no:randomly -p no:benchmark -n 4

The LoKI notebook executes headless with

    .venv/bin/python -m jupyter nbconvert --to notebook --execute packages/essapps/notebooks/loki-session.ipynb --output ../../../.scratch/run.ipynb --ExecutePreprocessor.kernel_name=essapps

Decisions D1 to D15 of `docs/developer/architecture.md` are all implemented in
local mode: records and both reference forms, the data store with private cache
and disk tier, session and subprocess launchers, the group submit with pending
outputs, the warm sciline wrapper on `sciline.Stage`, contributions with
contribute, combine, and finalize, labels and member keys, rules with apply,
backlog, reprocess, rerun and a memoryless trigger loop, the folder dataset
source and the picker, publication with a provenance snapshot. Not started:
HTTP, any UI, a Tiled disk tier, a SciCat dataset source, environment
provisioning, retention, remote sessions.

## What changed on 2026-09-17, so a reviewer is not surprised

- A data field in a spec is a reference only, annotated with the format of the
  bytes. The callable takes `(params, inputs)` and asks `inputs.path(ref)` or
  `inputs.array(ref)` per parameter; the sciline wrappers name that form in
  `resolve=`. See D8 and D13 in the sketch and the twelfth review-log entry.
- `Ref` in the spec is the union of `OutputRef` and `DatasetRef`. A dataset
  reference is one string; `ess.apps.spec.dataset_ref` encodes `pid:<pid>`,
  `run:<instrument>/<run>`, or `path:<path>`, and `dataset_path` reads a path
  back. Member keys and provenance entries carry these strings.
- `cheap` is gone from the spec; which parameters a stage takes per call is the
  binding's `stage_inputs`.

## Review items and the question each answers

1. **LoKI notebook** (`packages/essapps/notebooks/loki-session.ipynb`). Is the
   client interface right when used as a scientist would: submit, chain by
   reference, rerun with a slider, publish? Findings here change the sketch's
   choice 4 (D9, D10) and `client.py`.
2. **Callable contract** (`binding.py`, `warm.py`, `loki.py`,
   `aggregation.py`). Would you bind DREAM or BIFROST with `resolve` and
   `stage_inputs`? Do the three entry points fit a combine you have written?
   Where does this contract live once it settles, essreduce or essapps? Findings
   change D8 and the wrappers.
3. **Records and references** (sketch sections "Records and references",
   "Changes needed in the workflow spec", choice 3). Is the model sound, and is
   the single-string dataset identity acceptable? Findings change D1, D13, and
   `spec.py`, `sources.py`.
4. **Rules, batches, trigger loop** (`rules.py`, `batch.py`, sketch section
   "Rules"). Do lookup, selector, apply, backlog, reprocess, rerun match how ISIS
   interfaces are used? Findings change D14 and can be large; this is the least
   grounded part.
5. **Combining** (sketch section "Combining", `examples.py` `NORMALIZE`). Is
   contribution-then-finalize right beyond SANS? Findings change D15 and
   `aggregation.py`.

Not worth review time: `store.py`, `datastore.py`, `launcher.py`, `records.py`.

## Decided, do not reopen without a reason

- pandas is a hard dependency (batch table).
- A dataset reference is a single identity string in the spec; the framework
  encodes it. Chosen over three identity fields in scipp/ess#690.
- Materialization is not a spec concern; the binding asks for path or object.
- Records are never deleted singly; nothing is stored per dataset; the record
  store is not a catalogue.
- A declaration belongs on the spec only if a reader that cannot import
  workflow code needs it (`contribution`, `finalize_params` pass; `cheap` did
  not).

## Open, needs Simon

- Where the callable contract (`Inputs`, `Workflow`, `resolve`) lives once
  stable.
- When the team reviews the sketch; the deck is being refreshed for it.
- Splitting `rules.py` (600 lines: criteria, stored data, apply, trigger loop,
  pandas view) if it stays as is after item 4.

## Done after the review items were listed, without review

- The LoKI bins are the branch's `QEdges` and `WavelengthEdges`; the Q edges
  are the one stage input (commit 0d9000d).
- `WorkflowSpec.serialize()` carries `contribution` and `finalize_params`.
- The review deck matches the current sketch (cb1f47f).
- Amor reflectometry is bound as a probe, `packages/essapps/src/ess/apps/amor.py`
  and `tests/amor_test.py`; its findings are in
  [amor-probe-2026-09-17.md](amor-probe-2026-09-17.md). The ones that change
  the sketch, by review item: the opaque combine gets none of D15's member
  consistency check (item 5); collection keys are the submitter's invention and
  a pending output's key is never validated (item 3); chaining compares format
  only, so a declared `ArraySpec` is never read (item 3); a stage input is also
  how a session shares work across members of a series, which D8 does not say
  (item 2); every scalar parameter costs a `NewType` and a provider, a
  `convert=` beside `resolve=` would remove them (item 2); an output needing its
  own serializer has no place to declare one (item 2); the range vocabulary
  lacks pixel-index, angle, and Q ranges, and an edges model whose range the
  data derives (item 3); reflectometry's additive half, same-angle runs, still
  needs D15 built against `with_filenames` (item 5); the scale-factor feedback
  cycle is expressible but D14 has no word for it (item 4).

## How to resume

Give a fresh session this file and the findings. For each finding say which
item it belongs to and whether it is a design change (edit the sketch first,
then code) or a code change. The sketch is the source of truth; code follows it.
Commit with the `git-commit-handler` agent; do not commit on `main`.

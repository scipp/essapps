# Automatic reduction with the batch model

Question: are rules (`ess.apps.rules`) and the batch (`ess.apps.batch`) one model? This document writes the automatic-reduction stories with the batch, in scripts that run, and lists what the batch lacks.
Written by an agent on 2026-11-13, on `main` at `bca647a`. Scripts: `.scratch/next-steps/rules_*.py`, run with `.scratch/venv-container/bin/python`. Each claim is marked **run** (a script shows it) or **inferred** (read from the code).
`rules_common.py` holds the setup (toy specs of `user-stories.md`, `FakeFiles`, a `Log` and `FakeStore` that outlive a workspace, so a new `Workspace` on them is a restart) and the missing pieces, marked `MISSING`.

Claude read this, ran five of the scripts (`rules_arrival`, `rules_can_before`, `rules_series`, `rules_template`, `rules_restart`; they run and print what section 2 says), and added the `> Claude:` lines. One caution: the agent was told that you suspect one model, so weigh the scripts, not its agreement.

## 1. The two models

```python
# Rules (rules.py)                                # Batch (batch/reduction.py, state.py)
inputs: client, rules = [Rule(name, submit,      inputs: client, spec, columns, label,
          selector, label, at)]                           source(files) -> rows, settings
state:  cursor in client.changes(),              state:  rows (scan layer + hand edits),
        handled[rule] = files that records               _submitted[key] = request (memory),
        under rule.label hold at `at`                    _tweaked, an open stage
step:                                            step ("reduce remaining"):
  read changed records into handled                rows = source(all files)          # scan
  for rule, file in new files of rule.selector:    result[key] = newest record with
      if file in handled[rule]: continue                      label, member=key, spec, submitter
      try: record = rule.submit(client, file)      todo = rows complete, not running, and
      except LookupError/ValueError/SubmitError:          (no result | failed | cancelled | stale)
          status[rule] = reason; continue          client.submit(all todo, label)  # all or none
      handled[rule] |= files of record at `at`
```

"Handled" in rules is a property of a *file* (any record under the label that holds it at `at`). "Remaining" in the batch is a property of a *row* (its newest result, by key, spec and submitter, and an in-memory copy of what it was submitted with).

## 2. The stories with the batch model

The obvious loop is `batch.scan(); batch.reduce(settings, remaining(batch.state(settings)))` (`naive_step`). The loop with the missing pieces is `auto_step(batch, settings, at)`.

| Story | Rules today | Obvious loop | Batch with the missing pieces | Script |
|---|---|---|---|---|
| Transmission run reduced as it arrives; a sample waits for its transmission, then references its output | one function: `LookupError` until the partner exists, chain in one call | works | works with a change (gap 1; gap 3: S's source reads T's results) | `rules_arrival.py` |
| Each sample with the can measured before it (S7) | function looks up the can | works | works | `rules_can_before.py` |
| Source pairs with the *last* can, which arrives later | not affected: decided once at the first call | reduces sample 2 again with the new can | works: not reduced again | `rules_can_before.py` |
| Growing series, every new angle (E1), angle arriving again | `at='runs'` settles each angle | works until a restart; after it, angle 5 is never reduced and the row reads `completed` | works with a change (gap 1) | `rules_series.py` |
| Template improved during a beamtime (E4); setting changed while running | a new rule, handled from records | cannot: run 1 is reduced again (stale by settings) | works with a change (gap 1) | `rules_template.py` |
| Goes quiet (E2): spec version removed | reason per file | the whole step refused, reason returned | works with a change (gap 2): reason per row | `rules_template.py` |
| One refused row among good ones | that file skipped, others reduced | cannot: every step refused, runs 1 and 3 never reduced | works with a change (gap 2) | `rules_template.py` |
| Restart; a record running when the process ended | handled from records | resubmits run 9 at every restart (crash loop) | works with a change (gap 1): run 9 stays failed | `rules_restart.py` |
| Restart under another submitter name | handled (submitter ignored) | reduces runs 1, 2, 9 again | works with a change (gap 1) | `rules_restart.py` |
| A file that arrives again | same identity, handled | works | works | `rules_series.py`, `rules_restart.py` |
| History dropped (durable.md item 7) | all files reduced again | all again | all again | `rules_restart.py` |
| Own output not reduced (E3) | selector matches raw files only | `Batch(selector=)` defaults to raw files: works (inferred; publication not implemented) | same | none |
| LoKI, real workflows, three configuration layers | not written | not run | works: 60339 waits for 60394, 60387 for 60386, second water sample 1.6 s against 4.7 s for the first (the shared stage: inferred) | `rules_loki.py` |

Every entry in the two batch columns marked works or cannot is **run**; the scripts print the outputs. The rules column is what `rules_test.py` and `e_automatic_test.py` check (run: 30 passed, E3 xfail), except the "last can" row, which is inferred from `rules.py`.

## 3. What is missing in the batch model for automatic reduction

**Gap 1: "handled" at a column, read from the records.** The obvious loop decides by `remaining`, which fails in four ways (run): settings changed means stale (E4), failed means remaining (crash loop after `started`), staleness lives in `Batch._submitted` and is lost at restart (E1), and results are filtered by submitter. Rules' check, applied to rows, fixes all four:

```python
def unhandled(self, at: str) -> list[str]:          # Batch method, ~20 lines with _files_at
    """Ready rows whose files at `at` no record under the label holds, at `at`."""
    handled = set().union(*(files_at(r, at) for r in self.client.records(label=self.label)))
    return [k for k in ready(self.state()) if files_at_cell(self.rows[k], at) - handled]
```
Change to `ess.apps.batch`: `_files_at` moves from `rules.py` to `state.py`; one method on `Batch`. A changed cell at another column (a later can) does not reduce a row again; a new file at `at` (a new angle) does.

**Gap 2: one refused row must not hold back the others.** `Batch.reduce` is all or nothing (`batch.md`, Rules). Reducing row by row works but calls `state()` per row, so a step with N new rows is quadratic (run, `rules_cost.py`):

| New rows in one step | row by row | all but refused | trigger loop |
|---|---|---|---|
| 300 | 0.5 s | 0.1 s | 0.1 s |
| 1000 | 4.6 s | 0.4 s | 0.5 s |
| 3000 | 47.7 s | 1.3 s | 1.5 s |

```python
records, refused = batch.reduce(settings, keys, strict=False)   # ~15 lines in Batch.reduce
# refused: {'2': '2: reduction.bins: Input should be a valid integer'}; a reason of the
# settings (E2) names every row
```
`rules_common.reduce_all_but_refused` does this today on the public interface: submit, drop the rows the `SubmitError` names (`FieldError.entry`), submit the rest.

**Gap 3: a chain across batches.** A row of S references the output of T's row of the same sample. `batch.md` says a source "calls nothing", and chains are written as hand edits after `reduce`. An edit is lost at restart; a source recomputes. The script uses a closure (run):
```python
def samples(files):
    done = {T.rows[k].info['name']: s.record for k, s in T.state().items() if s.record}
    ...values['transmission'] = done[name].ref('transmission')
```
Change: no code; the guide allows a source to read another batch's results, and the loop steps T before S. A failed T record makes S's row `incomplete` ("the record it reads is failed") (run). Rules instead submit the reduction, which fails with the centre and settles the file.

**Gap 4: the loop and its status.** `RuleStatus` becomes the refusals of the last step; why a row waits is `RowState.problems` (`['empty']` for a missing transmission, run).
```python
@dataclass(frozen=True)
class Automatic:                    # ess.apps.batch.auto, ~30 lines with step()
    batch: Batch
    at: str                                   # the column whose new files trigger a reduction
    settings: Callable[[], Mapping[str, Any]] # read at each step: a change applies to new rows

def step(autos: Sequence[Automatic]) -> dict[str, dict[str, str]]:   # refusals by label, key
    for a in autos:
        a.batch.scan()
        _, refused[a.batch.label] = a.batch.reduce(a.settings(), a.batch.unhandled(a.at), strict=False)
```

Cost of an idle step at 3000 reduced runs: 60 ms for the batch (scan, records, state), 4.9 ms for the trigger loop, which reads only the changes (run). Linear in the runs of the proposal; no stated requirement sets a bound.

## 4. What rules have that would be lost

Only what a story or requirement needs.
- **A chain in one call** (the centre example of `automatic-reduction.md`, README "Automatic reduction"). Under the batch model it is two batches and gap 3. Nothing a story checks is lost (run, `rules_arrival.py`).
- **A failed upstream record settles the file.** Rules submit the reduction against a pending centre; it fails with it and settles. The batch does the same when both are submitted in one step (run). When T has already failed, S's row waits as `incomplete`. A person reduces T's row again; S's row is then handled already (its failed record holds the sample), as with rules (run).
- **Reading only the changes.** Idle cost above. Inferred: no requirement asks for it.

Nothing else in `rules.py` is needed by E1 to E4 or `users.md` "Automatically".

## 5. What the batch does that rules cannot

| Batch feature | Rules | Needed by automatic reduction? |
|---|---|---|
| hand edits, rows added or removed | none; a person submits under another label | no: a variant is a person's batch (deployments.md) |
| corrections (`CorrectedFiles`) | works too: it wraps the file source that rules read (inferred, `batch.md`) | same in both |
| tweak of one row | none | no |
| stale rows | none | no: E4 asks the opposite |
| inspecting rows before reducing (`state`, `problems`) | a reason string per rule | yes: E2 and "see why it reduced nothing" (users.md, Assumed) |
| stage: shared part computed once | each file is a plain request | inferred: LoKI's can (4.7 s against 1.6 s, run) |
| export, `essapps-batch`, Panel view, web page | none | the page of the automatic reduction (deployments.md) is a batch page (inferred) |

## 6. Options

### A. One model: rules removed

LoKI writes nothing beyond `ess.instruments.loki.batch`; the service reads configuration files and builds `Automatic` entries:
```python
autos = [Automatic(iofq_batch(client, label='auto/iofq'), at='sample', settings=lambda: layers('iofq')),
         Automatic(iofq_batch(client, label='auto/iofq/agbeh'), at='sample', settings=lambda: layers('iofq', 'agbeh'))]
while True: step(autos); time.sleep(1)
```
- Goes: `rules.py` (165 lines; `Rule`, `RuleStatus`, `TriggerLoop`), `rules_test.py` (631 lines, 25 tests), `automatic-reduction.md` (84 lines, rewritten), the rule code of `e_automatic_test.py` (137 lines, rewritten), README "Automatic reduction" (14 lines).
- Added (estimated): `Batch.unhandled`, `reduce(strict=False)`, `batch/auto.py`: about 65 lines; about 10 tests ported from `rules_test.py` (handled at a list column, failed counts as handled, refused row, restart, arrives again, setting changed).
- Stays: all of `ess.apps.batch` (core 1468 lines, 12 public names; panel and web 4935 lines).
- A technique writes pairing once, as a row source. Under B and C it writes it twice: a `RowSource` and a per-file rule function (`loki/batch.py:pair_by_sample` against `reduce_sample` in `automatic-reduction.md`).
- The automatic reduction's configuration is a batch template, so a scientist's variant (deployments.md) starts from the same file with **Load settings**.

### B. Both kept, with a shared "handled"

Rules stay for automatic reduction, the batch for persons; `_files_at` moves to a shared module, and the batch gains gaps 1 and 2 only if someone runs a batch automatically.
- LoKI writes `pair_by_sample` (batch) and a rule function per file, about 15 lines, as in `automatic-reduction.md`.
- Goes: nothing. Added: guide text on which to use; labels of rules and batches kept apart by convention.

### C. Both kept as today

No change. The defects of `durable.md` item 3 stay (a batch's records under a rule's label count as handled, reproduced there).

### The layered configuration, under A

A parameter is replaced whole by a later layer; a part is a batch of its own, with its own label, and the rows it selects leave the base batch (run, `rules_loki.py`, as Python dicts):
```yaml
# auto/loki.yaml: the instrument (a Template file of essapps-batch, plus at and source)
iofq: {spec: {name: loki-iofq, version: 1}, blanks: [sample, sample_transmission], at: sample,
       source: ess.instruments.loki.batch:pair_by_sample,
       params: {wavelength: {start: 1.0, stop: 13.0, num_bins: 200}, q: {...}, beam_centre: {...}}}
# auto/p1234.yaml: the proposal, and a part of it
params: {can: 60393, can_transmission: 60392, empty_beam: 60392,
         masks: {'@latest': masks, output: masks}}
parts:
  agbeh: {batch: iofq, select: {sample: agbeh}, params: {q: {start: 0.02, stop: 0.3, num_bins: 50}}}
```
`settings('iofq', 'agbeh') = loki.params | p1234.params | parts.agbeh.params`, read at each step. A changed file applies to rows not yet handled (E4, run with a toy). Inferred: a run that moves from the base batch into a part after it was reduced is reduced again under the part's label. Under B and C the same files hold a rule per part, with a `Selector` in place of `select`.

### What each option implies for durable.md

| Item | A | B | C |
|---|---|---|---|
| 3 labels and members | one kind of key: label = a batch, member = a row's key. Two readers of it: the result of a row (label, member, spec, submitter) and handled (label, files at `at`). 3(a) is enough. The submitter filter of `state.results` makes a page show "not reduced" for handled rows after a submitter change (run: `rules_restart.py` reduces nothing, inferred for the page) | 3(a), and the convention that rules and batches use distinct labels | as B, unenforced |
| 4 where a batch's definition lives, stale | the automatic batch's definition is the configuration files; it has no hand edits, tweaks, or stale rows, since gap 1 replaces "remaining". Item 4 concerns persons' batches only; 4(c) is unaffected | unaffected | unaffected |
| 7 handled after a history is dropped | handled is read from records, so every file is reduced again (run). 7(b) or 7(c) as for rules; for 7(c) the SciCat query asks for derived datasets whose input is the file at `at` | same | same |

## 7. Recommendation

Option A. Every story of section 2 runs with the batch and two changes to `Batch` (gaps 1 and 2) plus a 30-line loop, and the batch brings what automatic reduction needs and rules lack: a row state that says why a run waits, a stage for the shared runs, and a page. A technique then writes its pairing once, and the automatic configuration is a template that a scientist's batch can load. Rules' one strength, a chain in one function, becomes two batches, which the arrival story shows.

## 8. Questions for the lead

1. Converge to one model: A (rules removed), B (both, shared handled), or C (as today)?

> Claude: A, with one thing said plainly. A gives one model of rows, sources, settings and row state, but two answers to "what runs next": for a person, the rows that are remaining or stale; for the automatic loop, the rows with a file at `at` that no record holds. The two differ on purpose (E4: a changed setting must not reduce old runs again), so `Batch` gets a second method next to `remaining`, and the guide has to say which is for whom.

> Simon:

2. In automatic mode, does a row run again only for a new file at `at` (gap 1; a later can or transmission does not rerun earlier samples), or also when any cell changes (the obvious loop's stale)? The first is what rules do and what E4 asks.

> Claude: only a new file at `at`. It is what E4 asks and what rules do.

> Simon:

3. Partial reduce: `Batch.reduce(..., strict=False)` in the batch, or the retry on `SubmitError` kept in the loop (`reduce_all_but_refused`, no change to `Batch`)?

> Claude: keep the retry in the loop. It needs no change to `Batch.reduce`, whose all-or-nothing rule is right for a person, and it costs 1.3 s for 3000 new rows.

> Simon:

4. Chains: may a source read another batch's results (a closure, as in `rules_arrival.py`), or does the loop write the references as edits after each step?

> Claude: let a source read another batch's results. A source is recomputed after a restart; a hand edit is lost.

> Simon:

5. Where the loop lives: `ess.apps.batch.auto` (tested with the batch), or the automatic-reduction application of #151?

> Claude: `ess.apps.batch.auto`. The application of #151 then adds the process around it: one per instrument, a workspace per proposal, the configuration files.

> Simon:

6. `state.results` filters by submitter. Drop the filter (one folder or one service holds a history), or fix the service's submitter name?

> Claude: drop the filter. One folder or one process holds one history, and nothing defines whom `submitter` names (`durable.md`, Other things on disk). The filter was written for a workspace shared by several users, which no deployment has.

> Simon:

## 9. Not checked

> Claude: the second entry matters for the automatic reduction's page: the loop and a page would use one `Batch` from two threads, and `Batch` has no lock. To be settled when the application of #151 is built.

- E3: publication is not implemented; the batch's raw-file default is read from `reduction.py:118`.
- A page (Panel or web) showing the automatic batches, and a page and the loop using one `Batch` from two threads. `Batch` has no lock (inferred).
- Several proposals in one process (#151): one set of batches per proposal client (inferred, as for `TriggerLoop`).
- A folder workspace (`open_folder`): the scripts use an in-memory `Log` and `FakeStore`; `rules_loki.py` uses `local()` without a store, so nothing there is persisted.
- A setting changed while a staged batch runs: `Batch._open` replaces the stage when the template differs (inferred); `rules_template.py` uses no stage.
- A rule that pushes into an accumulator (D7, Open in both models).
- How many tests of `rules_test.py` carry a behaviour not covered by the ported ones (estimated at 10 of 25 from their names).

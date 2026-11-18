# #222: several workflows on one table, a prototype and its cases

**Summary.** The model holds for every case I ran, and it is short to explain. It needed four changes to what was proposed:

1. Settings do not decide whether a result is current. If they did, one change of a centre setting would empty the centre column of every row, and every I(Q) would wait.
2. A parameter becomes a column only when `takes` names it, or when a column of its name already exists. Making every required parameter a column gives LoKI's I(Q) six columns that are settings, such as `q` and `can`.
3. "Reduce" always runs to the end of the chain. A "Reduce up to X" brings back case E of #222: Follow then reduces I(Q) for a centre that a person reduced again.
4. "Use" sets a cell by hand. That makes the row "changed by hand", so Follow does not take over after it, and "Use" has to reduce the row too.

The model is not much more complicated than the alternative of one batch per workflow. Its real cost is the page, not the logic (section 5).

The prototype is beside `Batch`, not a split of it. Nothing under `packages/` changed.

## 1. The model as it turned out

```
table   = source + scope + edits, all in column names; rows by key
step    = spec + takes {param: column} + settings + accept + per
columns = what the source fills, what `takes` names, and per step one output column '<step>.<output>'
label   = '<table>/<step>'; member = row key, or the value of column `per`
current = the newest record of the step for the row: not failed or cancelled, of the bound spec,
          and its request equals the row's now at every parameter the step takes (settings may differ)
output cell = set by hand if a person set it; else a reference to the current record (pending counts);
              else empty.  A step with accept=True fills nothing: only "Use" does
a step waits while an input cell is empty; its status names the column
Reduce(rows)  = at each step in order: the ready rows whose result is not current (+ earlier settings at "again" steps)
Follow        = scan, then at each step in order: the ready rows not changed by hand with no result,
                or a result that differs at a column the source fills or at an output column
changed by hand (of a row) = a cell set by hand that some step taking it has not reduced yet
```

```python
t = Table(client, 'b7', source=pair_by_sample, fills=('sample', 'sample_transmission'))
t.add(CALIBRATED_DETECTOR, id='detector', takes={'run': 'sample'})
t.add(BEAM_CENTRE, id='centre', takes={'detector': 'detector.detector'},
      settings={'masks': masks.ref('masks')}, accept=True)
t.add(IOFQ, id='iofq', takes={'beam_centre': 'centre.centre'}, settings={...})  # sample, sample_transmission: by name
t.columns  # ['sample', 'sample_transmission', 'detector.detector', 'centre.centre', 'iofq.iofq', 'iofq.transmission']
t.follow(); t.reduce(['60339']); t.use('60339', 'centre.centre', record)
```

## 2. Cases

Statuses are per step, in the order `detector | centre | iofq`. "Trial" is the first trial of #222, which used one `Batch` per workflow. I ran each case.

| Case | What a person sees | Verdict | Trial, where it differs |
|---|---|---|---|
| A: new rows, Follow | One pass submits `detector`, `centre` and `iofq` for 60339, all three while the earlier ones are still pending. 60340: `completed \| completed \| incomplete: sample_transmission empty` | works | same |
| B: detector fails | `failed \| waits for detector.detector \| waits for centre.centre`. Follow does not retry. A person's Reduce of the row runs all three in one action, and Follow then has nothing left | works | Trial: `failed \| incomplete \| incomplete`. A person reduces the detector, and Follow does the rest at its next pass |
| B2: centre fails | `completed \| failed \| waits for centre.centre`. The cause is in the same row | works | Trial: I(Q) `incomplete`, "the record it reads is failed", with the cause on another page |
| C: `sample` set by hand | `outdated \| waits for detector.detector \| waits for centre.centre`, and the row is marked changed by hand. Reduce can no longer run I(Q) with the old centre, because its centre cell is empty | works | Trial: `outdated \| completed \| outdated`, and Reduce at I(Q) used the new file with the old centre |
| C with Follow | Follow leaves the whole row alone, also after "Reduce up to detector". The person's Reduce of the row runs the centre and I(Q) | works | Trial: Follow reduced the centre once the person had reduced the detector |
| C: a row added by hand | `not reduced \| waits \| waits`, changed by hand. Follow leaves it. After one Reduce it is treated like any other row | works | — |
| E: centre setting changes | `completed \| completed, earlier settings \| completed`. Follow does nothing. "Reduce them again" at the centre, run to the end of the chain, reduces centre and I(Q) of those rows in one action. Follow then has nothing left | works | Trial: Follow reduced I(Q) afterwards, which nobody asked for |
| E with "up to centre" | I(Q) reads `outdated` and the next Follow reduces it | surprising, the trial's problem again | same as the trial |
| E-strict: settings decide "current" | `completed \| outdated \| waits for centre.centre` on every row as soon as the setting changes | wrong for a series; the "earlier settings" rule is lost | — |
| D: series ended, partner measured later | `completed \| completed \| outdated` (changed: `sample_transmission`) | works | same |
| F: add a workflow to a table with results | Adding `iofq` adds no column. Every row reads `not reduced` at iofq, and the next Follow reduces every row there. Adding `norm` with `takes={'thickness': 'thickness'}` adds the column `thickness`, and rows read `incomplete: thickness empty`. A person fills it and must press Reduce: a filled cell counts as changed by hand | works; the Follow part is a consequence to note | — |
| G: accept a centre | After Follow: `completed \| completed, not used \| waits for centre.centre`. After "Use": iofq `not reduced`, and the row is changed by hand, so Follow does not take over. Reduce runs I(Q). A newer centre then shows `completed, another used`, and I(Q) stays `completed` with the accepted one | works, if "Use" also reduces the row | — |
| H: shared transmission run | With `per='sample_transmission'`, two rows get one record: one submission, the same ID in both cells. When a newer transmission of `w` arrives, both rows read `not reduced` (not `outdated`), and Follow reduces it once. A row whose transmission is set by hand to another run reads `not reduced` and is left to the person | works; "outdated" disappears for such a step | — |
| I: remove a workflow | Removing `centre` is refused: `['iofq'] take the output of 'centre'`. Removing `norm` removes `thickness` and `norm.normalised`, and drops the edits at them | works | — |
| J: restart | A new `Table` made from `chain.json` and `edits.json`, both saved as JSON and read back, shows the same statuses, including the accepted centre | works | — |
| LoKI's three specs | They bind with stand-in functions. `detector.detector` fits `Referenced[DetectorImage]`, `centre.centre` fits `BeamCentre \| Referenced[BeamCentre]`, and the raw `sample` file fits both `RunFile` and `SampleRunFile`. I(Q) reads `not reduced` after "Use" | works | — |

One limit of the prototype: within one pass, a referenced record can fail between the state and the submission. The workspace then refuses the request. The prototype drops the refused rows, as `Automatic` does.

## 3. The six decisions of #222

1. **When a row is ready at a workflow.** No separate rule is needed. An output cell holds the current result or nothing, and a step with an empty input waits with `waits for <column>`. A reference to a pending record fills a cell, so one pass still submits the whole chain.
2. **What "Reduce" means.** It acts on rows and runs every step whose result is not current, in the order of the chain. "Reduce them again" names the steps at which earlier settings count, and continues down the chain. A result that a person reduces again is taken by the later steps in the same action. Follow reduces only for the source's columns and for output columns, and never touches a row that is changed by hand, which is now a property of the row. Offer no "Reduce up to X" (section 4).
3. **Acceptance.** `accept=True` on a step: the step still runs, by Follow too, so its column shows candidates, but its output cell is filled only by "Use". "Use" sets the cell by hand and reduces the row. The cell then stays until it is reverted, and a newer result reads `another used`. The flag is on the step, not on an input, because the output cell is one column shared by every step that takes it.
4. **Names.** `takes` maps parameter to column. A parameter takes a column of the same name by default. Types need not match: file roles narrow what a picker offers and do not validate, and each step validates the cell at its own parameter. Edits, `at`, scope and the table files stay in column names. The translation is in one place, where a request is built from a row and where a refusal's position is turned back into a column. Other places still need a decision: the type of a column on the page or in a table file (from which step's parameter?), the blanks of the template (now per step, in `chain.json`), and the cache of resolved values, which is keyed by parameter and so belongs to the step.
5. **A value shared by some rows.** `per='<column>'`: the step takes only that column, and its member is the cell's value. Rows with the same transmission run share one record, and "the newest record under the label by member" stays the rule. Finding results by all their inputs would lose `outdated` and the comparison that Follow and "changed by hand" need (section 4). Not tested: two rows sharing a value through a file named by run and by identity. The member must be the resolved identity.
6. **Changes of the list.** Removing a step is refused while another takes its output. Columns that no step takes and the source does not fill go away, with their edits. Records stay. Changing `takes` after settings exist is not prototyped; I propose remove and add again. Adding a step under Follow reduces every row at that step on the next pass.

Smaller points of #222: a row added by hand counts as set by hand at the input columns only, so its output cells come from the steps. Each step keeps its own stage, unchanged. An output that is not persisted is still gone after a restart while its row reads `completed`; the model does not change this.

## 4. What did not hold, and why

| Proposed | What happened | What holds |
|---|---|---|
| "current" includes the workflow's settings as they are now | One setting change empties that output column in every row, so every later step waits (E-strict), and "a change of settings applies to rows not reduced yet" is lost | Settings do not decide "current". A result with other settings reads `completed, earlier settings` and stays in the cell. One condition in code (`settings_count` in the prototype) |
| Every parameter with no setting and no existing column becomes a new column | LoKI: I(Q) would add `can, can_transmission, empty_beam, direct_beam, masks, wavelength, q`, and the centre would add `masks` | A new column only where `takes` names one. The same name is taken by default. Any other parameter is a setting, refused until it is set if it has no default |
| "Reduce up to column X" | Leaves the later steps `outdated`, and Follow reduces them (E with "up to centre"): the trial's problem again | Reduce always runs to the end. A person who wants to inspect a value before the long step marks that step `accept` |
| "Use" as an ordinary cell set by hand, with the rest following | The row is changed by hand, so Follow leaves I(Q) (G) | Keep the rule. The page's "Use" sets the cell and reduces the row |
| A result found by the values of its inputs | Each distinct input set has its own history, so a row whose input changed has no earlier result to compare with. `outdated`, `changed`, `at` and "changed by hand" lose their meaning. Reasoned, not run | `per=<column>`: one result per distinct value of that column |
| One table per batch, output cell current or empty, changed by hand per row, removal refused while taken | held | — |

## 5. Splitting `Batch`

I built the prototype beside `Batch` and reasoned about the split. I did not prototype it.

| Part | Today in | Goes to |
|---|---|---|
| source, selector, scan, rows (`RowTable`), `put`, edits and their definitions, `set_by_hand`, scope (ADR 0011) | `Batch`, about 90 lines | table |
| spec, settings and their definitions, label, records polled under the label, `_compare`, `_resolve_cells` with its cache, stage, `reduce`/`_submit`, `cancel`, `close`, `_once` | `Batch`, about 480 lines | step; `takes` translates in `_cells` and `_once` |
| `RowState`, `row_states`, `problems`, `ready`, `left_to_reduce`, `earlier_settings` | state.py | per (row, step); add `waits`; `problems` gets the column of each parameter; "not a column" moves to the table |
| `changed_by_hand`, `arrived` | state.py | row level, over the steps; output columns join `at` |
| `Automatic` | auto.py | walks the steps, as `Table.follow` does |
| `settings.json` per label | definition.py | `chain.json` per table: `[{id, takes, accept, per, template}]` |

`Batch(client, spec, columns=..., label=...)` can stay as the one-step case of the same code: a table with one step, `takes` mapping each column to itself, the step's label set to the given label, and `state()` returning that step's `RowState`s. The page, the command, `frames`, `text` and the 51 `Batch(` calls in tests then keep working unchanged.

Size, estimated, not measured: reduction.py +200 lines net, state.py +60, auto.py +30, definition.py +50, plus tests. A page for several steps is the bulk. server.py (1232 lines) assumes one spec in about 20 places: the settings form from `batch.spec`, the column types from `spec.params`, and `changed` read as settings fields. Several hundred lines of server and JavaScript, and their tests.

Risks:
- `per` breaks the assumption member == key in `results()` and `cancel(keys)`.
- A state now costs the sum over the steps, with one record poll per label. One shared `changes` cursor would be needed.
- The labels of a one-step batch must stay as they are, so the step label is stored rather than derived.
- The column type for a page or a table file has to come from one chosen parameter.
- The page.

## 6. What a page would show

- One table: the input columns, editable, then one cell per output column holding the step's status. Selecting a cell shows the record and its plot. A hand-set output cell has "Revert".
- "Reduce" and "Reduce selected" act on rows and run the whole chain. "Reduce them again" is per step.
- A settings form per step, as tabs or an accordion, each with its own Apply.
- On a step with `accept`: "Use" on a cell, which sets the cell and reduces the row; the same for selected rows, for a centre shared by a sample changer.
- The header names the steps and what each takes.

## 7. Questions for the owner or an instrument scientist

1. Do you agree that settings do not decide "current" (section 4, first row)? The other choice means one batch per set of settings, which ADR 0011's scopes make possible.
2. A setting needed by two steps, such as LoKI's `masks` for the centre and for I(Q), is given once per step. Is that acceptable, or is a setting of the table wanted?
3. Instrument scientists: is a shared value always "one per value of one column", as with the transmission per transmission run? Or are there values shared by rows that depend on several columns?
4. Instrument scientists: should a step be marked `accept` by the application, or chosen by the person per batch?

## 8. Running it

```sh
python spikes/222/cases.py   # all cases of section 2
python spikes/222/loki.py    # the chain on LoKI's three specs, stand-in bindings
python spikes/222/trial.py   # the trial script of #222, for comparison
```

- `spikes/222/table.py`: the prototype, `Table` and `Step`, about 400 lines of code.
- Needs `essspec`, `essapps` and `essinstruments` installed from this branch.
- Five runs of `cases.py` gave the same output. Whether I(Q) of a row whose centre fails is submitted in the same pass depends on timing; the statuses do not.
- Not done: staging, scope, the cache of resolved values, a page, and a real reduction with LoKI data.

## Stage 2: the split

Two commits on `222-workflows-on-a-table`, on top of the spike:

| Commit | What it does |
|---|---|
| 1 `2b93eaf` | Splits `Batch` into `Table` and `Step`. `Batch` keeps its constructor and every public member, and is built from one `Table` and one `Step`. No behaviour change. No test changed. |
| 2 (see `git log`) | Lets one batch hold several steps: `takes`, output columns, `waits for <column>`, `Batch.add/remove/walk/states/reduce_left`, and Follow over the chain. 23 tests in `tests/batch/chain_test.py`. `accept` and `per` are only in the spike. |

The full suites of `packages/essapps` and `packages/essinstruments` pass after each commit: 1234 tests after commit 1, and 1257 after commit 2. `web/`, `cli.py` and `frames.py` are not touched.

### 1. Size

| File | Commit 1 (+/−) | Commit 2 (+/−) |
|---|---|---|
| `reduction.py` | +392 / −122 | +382 / −61 |
| `state.py` | — | +11 / −3 |
| `auto.py` | — | +38 / −18 |
| `definition.py` | — | — |
| `__init__.py` | — | +2 / −1 |
| `docs/batch.md` | — | +20 |
| `tests/batch/chain_test.py` | — | +488 (new) |

`reduction.py` was 574 lines on `main` (199 of them docstrings). It is now 1167 lines (441 docstrings):

| | Code and blank lines | Docstring lines |
|---|---|---|
| Commit 1 | +126 | +144 |
| Commit 2 | +225 | +98 |

My stage-1 estimate was "+200 net in reduction.py" for both parts together. The real figure is +593 for both commits, or +351 without docstrings.

- **Commit 1:** most of its growth is the `Batch` facade. It has 13 delegating members, and each repeats the docstring of the `Table` or `Step` member it delegates to.
- **Commit 2:** the new code is `StepRows`, the walk, `add` and `remove`, the translation between parameter and column in `Step`, and `reduce_left`.

### 2. Commit 1: test changes, and what resisted the cut

No test changed. The cut follows the line between "rows" and "records":

| Part | Goes to | How the cut was made |
|---|---|---|
| source, selector, scan, `RowTable`, `put`, the edits in the definitions, `set_by_hand` | `Table` | — |
| spec, settings and their definitions, label, records polled under the label, comparison, cache of resolved values, stage, submit, cancel, close | `Step` | `Step.state/results/reduce` take the rows (and `set_by_hand`) as arguments, so the step knows nothing of files or edits |
| emptying the cache of resolved values when the table scans | `Batch.scan` | calls `table.scan()`, then `step.forget_resolved()` (public) |
| the columns | both | `Table.columns` (used by `put` and for rows added by hand) and `Step.columns` (parameters) hold the same tuple when there is one step |

What resisted, or did not cut cleanly:

- **The cache of resolved values.** It is keyed by parameter, so it belongs to the step. The table's scan must empty it, and only `Batch` can do that. It is one explicit call.
- **The `changes` cursor.** It stays with each step, one per label. With several steps, each one reads `client.changes` on its own. One cursor shared by the batch would cost less. Not done.
- **The stage.** It belongs entirely to the step. No problem.
- **Definitions.** The edits go to the table and the settings to the step, both keyed by the label. One behaviour changed that no test covers: if the edits file and the settings file are both broken, the error for the settings is raised first.
- **Duplicated docstrings** between `Batch` and `Table`/`Step` (`rows`, `put`, `scan`, `settings`, `state`, `results`, `reduce`, `cancel`).

**Is commit 1 mergeable as it is?** Yes as code. But it adds 270 lines and no feature, so it should only be merged together with something that uses the split: the chain, or the manager of ADR 0011.

### 3. ADR 0011 on the two sides

| Part of ADR 0011 | Side | Why |
|---|---|---|
| header: title, description | batch | it names what a person made: the table with its chain |
| header: source (registered name) | table | it makes the rows |
| index of a row | table (`Row`) | it orders rows, whatever the steps |
| scope, applied by the scan | table | `rows = scan in scope, with edits on top` |
| `edits.json` | table | edits are by column |
| `settings.json` | step | one template per step: spec, settings, blanks = the parameters taken |
| label | batch, plus a label per step for its records | see below |
| `following.json` | application | unchanged |

**Labels, as implemented in commit 2, and the scheme I propose:**

```
records of the first step       label 'b7'           (a batch of one step keeps its plain label)
records of each added step      label 'b7/<name>'    ('b7/centre', 'b7/iofq')
definitions/labels/b7/          header.json, edits.json, settings.json (of the first step)
definitions/labels/b7%2Fcentre/ settings.json        (FolderDefinitions writes '/' as %2F, so a sibling folder)
```

- Each label names the records and the settings of one step. That is today's rule, one label per `Step`, so nothing in `Step` changes.
- The batch-level files sit under the label of the first step.
- The manager lists as batches only the labels that have a `header.json`, so `b7/centre` is not listed as a batch.
- A batch that a person makes, rather than code, also needs `labels/b7/chain.json` = `[{name, spec, takes}]`. Not implemented: in commit 2 the chain is code, as `spec` and `columns` are today, and a restart (`test_a_batch_made_again_on_its_definitions_reads_the_same_states`) needs only the files that exist.
- The cost of the scheme is an asymmetry: the first step has no `/<name>`. Removing the first step leaves the batch-level files under a label whose records belong to no step. `remove` allows it, and keeps the first step's input columns.

### 4. Names

Words that are already taken:

| Word | Already means |
|---|---|
| step | one step of Follow: `Automatic.step()`, and "each step of Follow" in batch.md and ADR 0011 |
| table | `RowTable`; the table files a person loads (`frames`); the "stored tables" of batch.md's model |
| workflow | the code that a binding connects to a spec. The declaration is `WorkflowSpec`, called "spec" in every doc. In essreduce, "workflow" means the sciline pipeline |
| reduction | a record that runs ("a long reduction starts") |

Proposal:

- **`Batch` is the table with its chain.** It is what a person makes, names, bounds and removes; it has the header; a page shows it.
- **The table part is private:** `_Rows`, used only by `Batch`. Nothing outside `Batch` needs it, and keeping it private avoids a third "table" next to `RowTable` and the table files.
- **The step part is public**, because `add`, `steps` and `step(name)` return it and a person sets its settings. I propose **`Workflow`**: it is the owner's word ("a batch holds a list of workflows"), and it pairs with `WorkflowSpec` as the use of a declaration. The cost is a clash with essreduce's "workflow" for a sciline pipeline, outside this package.
- If that clash is too much, **`Link`** ("a link of the chain"), which clashes with nothing.
- Either way, Follow keeps `step()`, and `StepRows` becomes `WorkflowRows` or `LinkRows`.

The code still uses the working names `Table` and `Step`, so that renaming is one change.

### 5. What changed when written against the real code

**Simpler than in the spike:**

- **The parameter–column translation** is two methods of `Step`, `_in_params` (row → request) and `_in_columns` (state → columns, refusal positions included). With `takes` the identity, both return their argument unchanged, so a batch of one step pays nothing. Everything between them is the old code, still working in parameter names.
- **"Current"** is one 8-line function over the fields `RowState` already has (`status`, `changed`, `spec_changed`, `problems`, `waits`).
- **"waits"** is one field of `RowState`, one line in `text`, and one in `ready`. `left_to_reduce`, `earlier_settings` and `arrived` did not change.
- **Follow over the chain** reuses `arrived` as it is. `at` gains the output columns. The case "settings refused" is handled per step.
- **A restart needed no new file** while the chain is code (see 3).

**Harder, or found only here:**

- **The columns of the source.** Under the decided rule, a parameter becomes a column only through `takes` or an existing column of its name. Today, a value at a name that no step takes reads `not a column`. Together:
  - I(Q) must name `sample_transmission` in `takes`, although the source fills it.
  - A batch of detector and centre over a source that fills `sample_transmission`, as LoKI's does, is `incomplete` in every row, because no step takes `sample_transmission` (test `test_a_value_of_the_source_that_no_step_takes_is_not_a_column`).

  The table needs the source's columns declared. ADR 0011's registered source has them ("the columns it fills"). This is the main open point, and it is a few lines once the source declares its columns.
- **The one-step members of `Batch`** (`spec`, `settings`, `settings_fields`, `template`, `state`, `results`, `reduce`) raise `ValueError` for several steps. The page, the command and `frames` use them, so they stay on batches of one step.
- **Return values.** `Automatic.step()` and `reduce_left()` return, for each row, the last record submitted. It reads the earlier ones, so waiting for it waits for the chain. I chose this so that `step()` stays `dict[key, Record]` for the existing tests.
- **Changed by hand.** It is implemented as "from the first step at which the row is changed by hand, on". The exact rule, any step, would need a full state of the chain before reducing, which doubles the cost. The two differ only for a step before the hand-set cell is taken. Such a step does not read that cell, so the difference is harmless.
- **Cost.** The walk reads a step's state again after it reduces, so that the next step sees the new records. Otherwise the state is cached. With one step there is no second read.
- **A refusal at a later step.** `reduce_left` is all-or-none per step. A refusal at a later step raises after the earlier steps were submitted (test `test_settings_refused_at_a_later_step_raise_after_the_earlier_ones_submit`). The same happens when a record that a cell reads fails between the state and the submission; the spike dropped such rows instead.
- **A setting with the name of a column the same step takes** would read as a changed input. Not guarded.

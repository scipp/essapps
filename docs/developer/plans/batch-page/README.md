# Plan: the batch page after its review

This plan follows a review of `ess.apps.batch` and `ess.apps.batch.web` as PRs #204 and #205 left them.
It lists what the review found, what was decided, and the work that follows, as packages that each make one pull request.
`probes/` next to this file holds the scripts that reproduce the findings ([probes/README.md](probes/README.md)).
Delete this folder when the last package is merged.

Terms (*row*, *column*, *settings*, *source*, *scan*, *result*, *definition*, *label*) are those of [the batch guide](../../../../packages/essapps/docs/batch.md#terms).
*Follow* is the switch of the page that steps the automatic reduction of the page's batch ([the page's README](../../../../packages/essapps/src/ess/apps/batch/web/README.md)).

## Goal

The batch page is the base for both ways of reducing a batch: by a person, and by Follow as the files arrive.
The logic under it (`ess.apps.batch`) is small and predictable. The page on top grew by adding.
The work of this plan makes the page as easy to understand, and as predictable for the person who uses it, as the logic is. It adds no features.

The work is done when:

1. **Each rule exists once.** One view of a batch, one place that says in which state a row is, one name for each rule of which rows are reduced.
2. **A person can predict what the page does.** A change is in force when the person says so: settings after Apply, and Follow reduces for what the files bring, never for what a person changed. What the page says is true: no message that contradicts what happened, no mark that outlives its cause, no value sent that the person did not finish typing.
3. **The defects found are gone**, each with a test that fails without its fix.
4. **The code is no larger for it.** Two packages delete. No package adds a mechanism that a decision of section 2 does not ask for. `web/server.py` should be shorter at the end than at the start.
5. **The next question is ready to decide**: how the page is built around a range of rows.

Not goals: showing 10 000 rows fast, several people on one page, several batches on one page, hosting.

Each package below states its own goal, then how to get there. The goal is what counts: the steps, the code and the names are a proposal written before the work was done.

## 1. How the batch page works

```python
files   = client.files.list(selector)            # grows on its own
rows    = source(files)  ⊕  edits                # scan layer, with the hand layer on top (RowTable)
results = newest record per member under label   # grows on its own (the history)
state   = rows ⋈ results on key == member        # per row: status, problems, changed
todo    = rule(state)                            # the only place a person and Follow differ
batch.reduce(todo)                               # appends records, so `results` changes
```

`changed` names the parameters at which a row's request now (settings and cells) differs from the request of its result, both in the form that a record holds.

| | trigger | rule | on a refused row |
|---|---|---|---|
| a person | a button | `left_to_reduce`, `earlier_settings` ("Apply to all"), `ready` ("all"), or the selection | nothing is submitted, the row is named |
| Follow | a timer, and each action | `arrived(state, at)` | that row waits, the others are submitted |

Layers:

```
static/*.js       renders State, sends actions                          browser
web/api.py        HTTP: one POST per action, answers the whole State
web/server.py     Server: what the page shows and does, without HTTP    the UI core
web/models.py     State and its parts, as pydantic models               the UI core
batch/{table,state,reduction,auto}.py     the chain above               the logic
batch/{definition,corrections}.py         edits, settings, corrections kept as JSON
```

What the review found sound, so that it is not questioned again without a reason:

- State is derived from the files and the records. `Automatic` keeps nothing that decides which rows run, so a restart or a failed step loses nothing.
- `state.py` and `table.py` are pure and tested on values. `Batch` is the only class that calls the client.
- The tests of the page go through HTTP on a real in-process workspace with fake workflows.
- An instrument writes about 150 lines: a row source, the columns, the columns `at`, a model of what to correct on a run, and a function that draws.
- Mutation testing of the logic (`state.py`, `auto.py`, `reduction.py`): 52 of 55 mutations fail a test.

## 2. Decisions

Made by Simon on 2026-11-17, unless marked.

| Topic | Decision |
|---|---|
| Two views of one batch | One presenter. `ess.apps.batch.panel` is deleted. `Server` and `State` are the UI core; a view in a notebook, when one is needed, renders `State` and calls the same actions. |
| When a change of settings is in force | Only after an explicit **Apply**, with or without Follow. Several settings often change together. |
| A reduction of one row alone (`Batch.tweak`) | Left to the implementer. Chosen: remove it ([package 6](#package-6-remove-the-reduction-of-one-row-alone)), since Apply, "Reduce selected" and the rule of earlier settings already let a person try settings on one row. This is the one decision of this plan that Simon has not confirmed. |
| How many rows | Thousands are likely, 100 000 very unlikely. They need not all be shown at once. The page will probably be built around a range of rows (a range of runs) that a person shows and edits. No work goes into showing 10 000 rows. |
| The shape of automatic reduction | Open. Possibly a hosted instance of this page, configured by the instrument scientist only. No work on several users or several batches on one page for now. |
| `essapps-batch reduce --remaining` | Follows the rule of the page's "Reduce" (`left_to_reduce`). |
| A person's changes while Follow is on | Follow reduces a row for what the files changed, never for what a person changed. What a person changes is reduced when the person says so, with or without Follow ([package 4](#package-4-apply-for-settings-and-follow-leaves-a-persons-changes-to-the-person)). |

Consequences, so that nobody builds them by mistake:

- No protocol that sends changed rows only, no virtual scrolling, no replacement of Tabulator by an own table. These wait for the design of the row range ([package 10](#package-10-a-design-for-the-row-range-no-code)).
- No drafts per browser session, no second batch on a page, no rule for retrying failed rows without a person.

## 3. Findings and their state

`known_defects_test.py` in `packages/essapps/tests/batch/` holds one test for each defect of the server that still exists. Each asserts the behaviour wanted and is marked `xfail(strict=True)`, so a fix makes it fail until the marker is removed and the test is moved next to the tests of the same behaviour.

State on `main` at `e92ceb0` (after #205). D are defects of the server, F of the page in a browser.

| | What a person sees | State | Where, and the fix |
|---|---|---|---|
| D1 | A file source that raises ends the timer of Follow, and the page still shows Follow as on. | fixed in #204 | Pinned by `web_follow_test.py::test_a_step_that_raises_anywhere_is_said_and_follow_stays_on`. |
| D2 | A correction of a run whose scan then fails is answered "The change was not kept", although it was kept, and the draft of the settings is discarded. | present | `_action` catches every `OSError`, also one of the file source. Catch only a failed write of the definitions, for example an error `NotKept(OSError)` that `DefinitionStore.save` raises. |
| D3 | A refused "Load settings" leaves the values of the file read so far in the form; the next edit of any setting writes them to the batch. | present | `Server.load_settings`; `Fields.set` sets field by field. Becomes part of package 4. |
| D4 | A result that finishes during a step stays "running" on the page. | fixed in #204 | A step reads the changes of records once. Only the case between two steps has a test; the case inside a step has none (package 9). |
| D5 | A step runs after "Stopped following" and submits rows. | fixed in #205 | Pinned by `web_follow_test.py::test_a_tick_that_waited_for_the_request_that_stops_following_does_nothing`, which waits a fixed 0.2 s (package 9). |
| D6 | "still running" stays in the box of refusals after the row is done, also after a reload. | present | `Server.refused` keeps a reason that names a row but no cell; nothing forgets it but an accepted reduction. Do not keep such reasons: the answer to the request already says them. |
| F1 | HTML in a row's name, a cell or a failure reason runs as HTML in a tooltip. | present | Tabulator sets a tooltip given as a string with `innerHTML`. `probes/frontend/probe_xss.py` |
| F2 | A poll draws the settings form again while a person types: the half-typed value is sent, and the caret jumps (12 arrives as 1, then 21). | present | `form.js` replaces the form. `probe_typing.py` |
| F3 | A run that arrived cannot be chosen in a cell until the page is loaded again. | present | The options of a column are those of the first state. `probe_races.py F3` |
| F4 | The filter and the sort are not applied to rows that change. | present | `Grid.draw` in `tables.js`. `probe_races.py F4-filter F4-sort` |
| F5 | The row tab shows one row while two are selected. | present | The answer of `/api/row` is not checked against the selection. `probe_races.py F5` |
| F6 | An older answer of `/api/runs` replaces a newer one. | present | No check of the version. `probe_races.py F6` |
| F7 | The first rows take quadratic time: 1.3 s at 1 000 rows, 5.7 s at 3 000, about a minute at 10 000. The grid of the runs is built at load although hidden. | present | `addData` for the first rows. `probe_load.py`, `probe_adddata.py` |
| F8 | "Select all" at 10 000 rows: `/api/plot` answers 400 and the page says nothing. | present | The keys are in the URL. `probe_select_all.py` |
| F9 | A failed request outside an action shows nothing. | present | Six call sites in `app.js`. `probe_races.py F9` |
| F10 | The keyboard focus goes to the body at each new state; the button that reverts a cell cannot be focused. | present | Chips and row tab are replaced. `probe_focus.py` |

Left of D1: the timer thread (`ess.apps.batch.auto.every`) still ends if `Server.tick` lets an exception out, which needs an exception whose `str()` raises. Not worth a fix of its own; if package 9 touches `step`, make `every` or `tick` unable to end by an exception.

Design findings, with the package that answers each:

| | Finding | Package |
|---|---|---|
| S1 | `panel/view.py` implements the rules of `web/server.py` a second time, with the same private state (`_settings_shown`, `_unfit`, the loaded tweak). Each rule added to the page is written twice. | 3 |
| S2 | A row's status is worked out in more than one place. `RowState.text` reads a failed row as `outdated` once a setting changes; the page overrides that in `Server._status` (#179, point 2). So a failed row whose settings changed reads `failed` on the page and `outdated` in the table downloaded from it. (That the two use other words, `done` and `completed`, is not a defect.) | 2 |
| S3 | Each edit of the settings form that leaves nothing missing is written to the batch at once, and with Follow on each action is followed at once by a reduction. So a row that waits is reduced with the settings as they are after the first of several edits, which nobody chose, and Follow does not reduce it again. The same holds for two edits of the cells of one row. | 4 |
| S4 | Which results are tweaks is kept in memory (`Batch._tweaked`) and lost at a restart; `state.py`, `RowState.text` and `Server` each carry a special case for it. | 6 |
| S5 | Each change sends the whole `State`: about 0.6 kB and 0.05 to 0.5 ms of server time per row, under the one lock. Measured: 0.04 s and 0.6 MB at 1 000 rows, 0.4 to 1.2 s and 6 MB at 10 000. | 10 |
| S6 | Whether `version` moves is decided by comparing snapshots (`_rows`, the runs, what waits, the failure) in several methods. | 9 |
| S8 | "Remaining" names two rules: the page's button leaves rows reduced with earlier settings, the command's `--remaining` reduces them. | 5 |
| S10 | `RowTable` has no edit of many rows: `Server.edit`, `revert` and `remove` call `rows.edit(key, ...)` per key, and each call builds the dict of all rows. An edit of all rows at 10 000 took 337 s. | 7 |
| S11 | The fetches of the page outside `act()` (`/api/row`, `/api/runs`, `/api/plot`, downloads, uploads) each handle order and errors on their own, or not. | 8 |

Not answered by a package, and why: one process serves one batch to one person (decision above); a failed row is not retried without a person (decision above); `static/vendor/` is git-ignored, so a wheel probably ships without the page's libraries (not verified by building one; open an issue).

## 4. Packages

Each package is one pull request, stacked in the order given, or one group of commits if a single pull request is used.
A package is done when its tests pass, `tests/batch` passes, and the guides say what the code does (`packages/essapps/docs/batch.md`, the page's `README.md`, `docs/developer/automatic-reduction.md`, the docstrings of the modules touched).
Before a fix, run the probe or the test of the finding: the branch may have moved, and a finding may be gone.

Order and what each depends on:

```
1 defects of the server ─▶ 2 one status ─▶ 3 delete batch.panel ─▶ 4 Apply, and Follow ─▶ 5 --remaining
     ─▶ 6 remove the tweak ─▶ 7 edit of many rows ─▶ 8 the page's requests and table ─▶ 9 tests and version
     ─▶ 10 design of the row range
```

3 comes before 4 and 6 so that neither is written twice. 8 comes after 4 and 6 because all three change `static/app.js`.

### Package 1: defects of the server

**Goal:** What the page tells a person after an action is true: a change that was kept is not reported as lost, and a refusal goes when its cause is gone.

D2 and D6: the tests of `known_defects_test.py`, with the fix sketched in the table of section 3, one commit each. D3 has its test there too, and is fixed in package 4, which rewrites how the settings form is filled.

### Package 2: one status

**Goal:** One function says in which state a row is. The page, the table it writes and the command agree on it for every row, and differ at most in their words.

```python
# state.py: the one place that says in which state a row is
RowState.text      # a failed or cancelled row stays so when a setting changes; it needs reducing either way
status_texts(...)  # as now: adds "earlier settings"

# web/server.py: words only
_STATUS = {'not reduced': 'ready', 'pending': 'running', 'completed': 'done'}
```

`Server._status` keeps only the translation of words and the rows that wait for Follow. Closes point 2 of #179. Check `remaining` and `left_to_reduce`: they read `status` and `outdated`, not `text`, so they do not change.

### Package 3: delete `ess.apps.batch.panel`

**Goal:** A batch has one view, so a rule of the page is written once.

- Delete the package (`view.py`, `sheet.py`, `cells.py`, `demo.py`) and `tests/batch/view_test.py`. Look for other users first (`rg 'batch.panel|BatchView|RunSheet'`), also in `packages/essinstruments` and the notebooks.
- `ess.apps.panel` (`App`, forms, outputs) stays.
- `batch.md`: the example of "An application", the sections "The UI" and "The app and the command on one folder", and the table of interfaces use the Panel view; write them for the page (`create_app`, `python -m ess.apps.batch.web.demo`). ADR 0007 names the package.
- State in the docstrings of `web/server.py` and `web/models.py` that they do not depend on HTTP, and add to `tests/batch/boundaries_test.py` that they import neither `fastapi` nor `starlette`. Do not move or rename the modules: that waits for a second view.

### Package 4: Apply for settings, and Follow leaves a person's changes to the person

**Goal:** Settings are in force only after Apply. A row that a person changed is reduced when the person says so, with or without Follow. Follow reduces what the files bring.

```python
server.edit_settings(edit)     # changes the draft only
server.apply_settings()        # writes the draft to the batch; refused while something is missing
server.discard_settings()      # the draft shows the batch's settings again
state.settings_draft           # {'changed': bool, 'missing': [...]}  for the page to offer Apply and Discard
```

- "Load settings" and "Use the settings of this row" fill the draft. Nothing else writes `batch.settings` from the page.
- A refused "Load settings" leaves the draft as it was: read the file into a new `Fields`, and take it only if all of it is read.
- Follow reduces with the batch's settings, whatever the draft holds. The rule that Follow waits while the form misses something goes.
- While the draft differs from the batch's settings, "Reduce" is refused with the reason "Apply or discard the changed settings", as it is now refused while the form misses something. Otherwise a person would reduce with settings other than those shown. (A choice of this plan; say so in the pull request.)
- The page: buttons Apply and Discard at the settings form; the form is not replaced while one of its inputs has the focus (finding F2). A browser test types into the settings form while a poll arrives.
- This changes how a person works with the page, so have it reviewed by a fresh reviewer who drives the page in a browser before the pull request is opened.

The second half, in a commit of its own. Today each action is followed at once by the reduction of a step, so a row edited at two cells of `at`, such as its sample run and then its transmission run, is reduced twice, the first time with half of the change. The rule instead:

```python
def arrived(states, at):                 # the rule of Follow and of auto.run
    return [key for key in ready(states)
            if not states[key].by_hand   # a person changed this row since its result: the person reduces it
            and (states[key].record is None or states[key].changed & at)]

# by_hand: the row has cells set by hand (RowTable.edits, or a row added by hand) and
#   no result, or
#   one of those cells is in `changed`
```

- A cell set by hand that equals the result's is not a change, so a row that a person edited and reduced is followed again: a file that arrives later at another cell of `at` reduces it.
- The page's "Reduce" (`left_to_reduce`) already takes these rows, so nothing is added for the person.
- `RowState` needs to say which of its cells are set by hand, or `Batch.state` works `by_hand` out; choose what keeps `state.py` free of the table. Name the field for what it means.
- An action then submits nothing for the rows that the person touched. Check whether the reduction at once after each action (`Server._follow_now`) is still needed: it exists so that a row which waits for Follow does not show a reason that the action removed. Keep it only if a test of that behaviour fails without it.
- Corrections of runs reach the rows through the scan, so Follow reduces for them as for a file that arrives; two corrections that belong together still reduce a row twice. Say so in the guide.
- The guides state the loop's promise as "the results under its label equal its batch's rows at the columns of `at`" (`batch.md`, "One label, one batch"; `automatic-reduction.md`). It becomes: at the cells of `at` that the scan gives. Update both, and the table "A row is reduced if it has ...".
- `auto.run` without a page follows the same rule: a batch whose rows were edited through its definitions waits for a person at those rows.

### Package 5: `--remaining`

**Goal:** "Remaining" means the same rows on the page and in the command.

`essapps-batch reduce --remaining` takes the rows of `left_to_reduce`. A second flag (name it for what it does, for example `--earlier-settings`) adds the rows of `earlier_settings`. `state.remaining` then has no caller outside `left_to_reduce`: keep one function under one name, and shorten the table "A row is reduced if it has ..." of `batch.md` by the row of the command.

### Package 6: remove the reduction of one row alone

**Goal:** Everything the page says about a row follows from its cells, the settings and the records; nothing about a row is kept in memory only.

With package 4, a person tries settings on one row without a tweak:

```
change the settings, Apply     # every done row now reads "done, earlier settings"; nothing is reduced
select the row, Reduce         # only this row is reduced with the settings
look at the result
"Apply to all"                 # or: change the settings back, Apply; the row then reads "earlier settings"
```

Remove `Batch.tweak`, `Batch.tweak_params`, `Batch._tweaked`, `RowState.tweaked` and its cases in `row_states`, `earlier_settings` and `RowState.text`; in the server the tweak form, `_tweak_of`, `edit_tweak`, `submit_tweak`, `RowDetail.tweak`, `Row.alone`; their routes; the tweak box of the row tab in `app.js`; the sections of the guides. The row tab keeps what the row was reduced with, and "Use these settings".

With Follow on, the settings applied for the trial are also those of the rows that arrive meanwhile. Say so in the guide.

Keep this package apart from the others, so that it can be dropped if Simon decides otherwise.

Not built now: a row that keeps settings of its own. If one is needed, the rule about values would be that a row may hold a value at any parameter, kept in `edits.json` with its cells, and the settings are what a row does not set itself. It would touch `problems` ("not a column"), `Batch._compare`, a staged `reduce` (rows with values of their own cannot share the stage), `earlier_settings`, and the table files.

### Package 7: an edit of many rows

**Goal:** An edit of K rows costs one pass over the table, not K.

`RowTable.edit_each(keys, values)`, `remove_each(keys)`, `reset_each(keys, *columns)`, each one pass over the table, as `CorrectedFiles.correct_each` is; the single-row methods call them. `Server.edit`, `revert`, `remove`, `restore` use them. A test on the number of rows touched or on time at 10 000 rows is not needed; a test of the result is.

### Package 8: the page's requests and table

**Goal:** The page never shows an answer older than one it has shown, never fails without a message, never runs text as HTML, and keeps the focus, the filter and the sort when the state changes.

One commit per item, each with a browser test where the finding is marked F in section 3:

- One helper for every request of the page: it shows the message of a failed request and drops an answer that is older than one already shown. `loadRow`, `loadRuns`, `renderPlot`, `download` and `upload` use it (F5, F6, F9).
- Tabulator: a tooltip is an element, never a string (F1). The editor of a column reads its options from the state shown, not from the first one (F3). After rows change, the filter and the sort are applied again (F4). The first rows are set with `replaceData`, and the grid of the runs is built when the Runs view is first shown (F7).
- The keys of a plot and of a download of results go in the body of a POST (F8).
- The chips of the filter are updated in place, so that the focus stays; the button that reverts a cell can be focused (F10).
- The URLs of the API are relative and set in one place, so that the page can be served under a path prefix.
- `GZipMiddleware` in `create_app`.

### Package 9: tests, and what moves the version

**Goal:** Each behaviour of Follow and of the version that an open page relies on fails a test when it is broken.

The mutation table (`probes/mutation/table.md`) lists the mutations that no test fails. Add a test for each behaviour of this list that still exists after packages 1 to 8, through HTTP:

- after a step of Follow, a page learns at its next poll of new runs, of a row that now waits, of a failure of the step, and of new rows;
- a result that finishes while a step runs reaches the page (a client that finishes the record right after the step read the changes; the probe of D4 did this);
- an action reaches a second page at its next poll;
- the marks of a refusal go after a reduction that succeeds, and stay when another cell or another setting is edited;
- `Action.running` and `Action.again`; `Row.result` of an outdated result;
- a result of a spec with the same name and version but another definition reads outdated;
- a cell that reads a cancelled record makes its row incomplete;
- `RowTable.put` and `remove` for a key that is both scanned and added by hand (three cases in the table).

Then, only if `Server` still compares snapshots in more than one method: let `version` follow from one counter per kind of change (the batch's rows and settings, the cursor of the listing, the server's own drafts and messages), and remove the comparisons. Skip this if it does not shorten `server.py`.

Hygiene: `test_a_tick_that_waited_for_the_request_that_stops_following_does_nothing` waits a fixed 0.2 s for the timer to reach the lock, so it can pass without testing anything; let the lock say when the tick waits. The browser tests wait for the server without a deadline (`web_browser_test.py`, where the thread is started); a test that fails while it holds `Gate` closed stalls for 30 s, so the fixture opens the gate at teardown.

### Package 10: a design for the row range (no code)

**Goal:** Simon can decide from one document how the page is built around a range of rows.

Write `row-range.md` next to this file, for Simon to decide on. Lead with examples and code, state each question with a line `> Simon:` under it. It should answer:

- What a range is: runs from A to B, the newest N, a filter on metadata, or a selector of the file source. Which of these the source of rows needs to see (a partner outside the range, such as a can measured at the start, must still be found).
- What the page shows of the rows outside the range: counts per state, or nothing.
- What "Reduce", "Apply to all" and Follow act on: the range, or the batch.
- What it costs: `Batch.state(keys)` answers for chosen rows without reading the others (0.6 ms for 10 rows of 30 000); `Server.state` builds every row.
- Whether the table of the page is then a plain table of at most a few hundred rows, without Tabulator.

## 5. Measurements to compare against

Toy workflow with three columns, workspace in memory (`probes/scale/scale.py`); the host was under load, so large N varies by up to a factor of 2.

| | 1 000 rows | 10 000 rows | 30 000 rows |
|---|---|---|---|
| `batch.state()` | 0.03 s | 0.17 s | 0.55 s |
| `server.state()` | 0.04 s | 0.4–1.2 s | 1.7–2.4 s |
| JSON of one `State` | 0.6 MB | 6 MB | 18 MB |
| a step of Follow with nothing new | 0.14 s | 1.1 s | 5.3 s |
| `poll` with nothing changed | < 0.1 ms | < 0.1 ms | < 0.1 ms |

In the browser, with the demo: the page shows 1 000 rows after 2 s and 10 000 rows after 53 to 78 s.

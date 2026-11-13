# The batch page under an automatic reduction

Simon's question: "Did this include thinking about how the unified UI (existing SANS web demo, for example) would cope with this? Would it just append new rows as they show up?"

**Answer.** No. `one-interface.md` ran the page's server object only. Run for real, the page shows a new row about 1.5 s after its file arrives at 300 rows, but it does not append it. It replaces the whole table: the scroll jumps to the top, and every row is computed, sent, and drawn again. A row that waits for its partner file does not show at all. At 1 000 rows a new row costs the browser 1.6 s and the server 1.7 s, during which the loop waits for the lock. At 3 000 rows the page shows a new row 31 s after its file, and the loop steps take 14 s. The proposal survives, but the page needs the work of section 3 first, and most of that work helps a person's batch too.

Every claim is marked **run** or **inferred**. Scripts, outputs, and how to run them: `.scratch/one-interface-page/`.

**Setup (run).** The checkout at `9c28338`, the branch's `Batch` (not the spike `one.py`). The toy spec `PLAIN` of `tests/batch/toys.py`. A row is a sample file plus the transmission file of the same sample name (`toys.pair`). A sample file that holds a negative number fails. The page is `create_app(batch)` served by uvicorn. A thread runs `Automatic(batch, at='sample', settings=lambda: server.settings.params).step()` every second under `server.lock`, which is what the proposal describes. The browser is headless Chromium at 1440×900. Rows the loop reduced show `done` here: the loop reads the form's settings, so the page and the loop compare the same requests. In `page_branch.py` the loop had its own settings, and these rows showed `outdated`.

## 1. What happens today (run: 300 rows in the browser, `browser.py`; 20 rows for the controls, `http_checks.py`)

```
+-------------------------------------------------------------+---------------------+
| [Reduce 3 remaining][v] [Cancel] [Rescan runs]               | Result|Settings|Row  |
| (all 311) (done 309) (failed 1) (incomplete 1)   <- chips    |                     |
| [ ] name   sample   transmission  thickness  status          |  plot of the first  |
| [ ] s0     run 0    run 1000000   1.0        done            |  12 results, or of  |
| ...        every row, drawn at once ('basic' renderer)       |  the rows selected  |
| [ ] a1     run ...  run ...       1.0        done   <- newest at the bottom       |
+-------------------------------------------------------------+---------------------+
```

| Situation | What the page does |
|---|---|
| A file arrives with its partner | The row shows 1.3 to 1.6 s later, without a click, already `done` (the toy takes milliseconds). The browser receives one whole state (0.17 MB) and asks `/api/plot` twice. |
| A sample arrives and waits for its partner | The row does not show: 6 s later the server holds 302 rows and the page 301. `Server.poll` moves its version only when a record changes, and a waiting row has no record. The row shows, as `incomplete`, with the next record of any other row. |
| A row fails | It shows as `failed` with the reason. The primary button says "Retry 1 failed". The loop does not run the row again. |
| The table is scrolled to row s100 | A new row sets the scroll back to the top (`scrollTop` from 3000 to 0). The cause: when the keys change, `Grid.draw` calls `replaceData` instead of `addData`. Since a new row arrives every few seconds, a person cannot stay at a place in the table. The newest row is at the bottom. |
| A row is selected | The selection stays ("1 selected" before and after). |
| Row tab open, the form "reduce alone" open, a value typed | It stays open, the value and the focus stay. |
| A cell is being edited | The editor stays open, and the new row waits until the edit ends (308 of 309 rows shown). After Enter, the edit is written and the new row shows. |
| A value typed in the settings form and not yet sent | The value and the focus stay. The form is drawn again only when the settings change. |
| A filter chip ("failed") | It stays applied when a new row arrives. |

The page's controls while the loop runs:

| Control | Effect |
|---|---|
| Retry failed (primary) | Submits under the lock, between two steps. No conflict with the loop (run). |
| Rescan runs | Does what the loop's next step would do anyway ("Found 313 rows"). No conflict (run). |
| Remove a row | The row stays removed after later steps (run). |
| Edit a cell at the trigger column `sample` | The loop reduces the row again at its next step: records go from 1 to 2 (run). |
| Edit another cell (thickness) | The row shows `outdated`, and the loop leaves it alone (run). |
| Edit a setting | The loop uses it for the next rows. Every earlier row turns `outdated`, and the primary button offers "Reduce 309 remaining" (run). At 100 000 rows this one click submits 100 000 rows (inferred). |
| Load a table file | The loop's next step scans and drops the loaded table: the thickness of 4.0 that the table gave is gone 2.5 s later. The page still says the loaded table is in place (`server.loaded == 'mine.csv'`) (run). **The only real conflict found.** |
| Primary button while reductions run | Disabled whenever any row is `running`. Its counter counts from the first row still running, so with 3 s reductions and a file every second it read "0 of 1", "0 of 2", ..., "0 of 6" and was disabled in 28 of 32 samples (run, toy). With a file every few seconds and reductions of minutes, a person could never press it (inferred). |
| A row the loop cannot submit | `Automatic.refused` holds the reason ("transmission: empty", "persisting needs a store"), but the page never shows it. Without a store, every row the loop refused showed `ready`, and the button offered "Reduce 5 remaining" (run). |
| Settings after a restart | `Server.__init__` starts the form at the spec's defaults. If the defaults are complete, a loop stepped by the server reduces new rows with them, without a warning (inferred, read from the code). |

Found on the way, a defect of the web view that does not depend on the loop (run, profiled): `forms.cell` searches the options of a file picker for the cell's value one by one, with pydantic `__eq__`. Every row has two file cells, and each picker lists every file, so `Server.state()` costs rows × files. It only matters after `_relist()`, and every poll that sees a new record calls `_relist()`. `Server.plot` calls `state()` again, so one new row costs two or three full states under the lock.

## 2. Numbers

One new row arriving, with the page open and the loop stepping every second. "Fixed lookup" means the same code with `forms.cell` replaced by a dictionary lookup inside the measuring script (nothing tracked was changed).

| Rows | `Server.state()` as is | with fixed lookup | of it `Batch.state` | JSON per state | browser busy per new row | file to row on screen | loop step while the page is open |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 300 | 0.17 s | 14 ms | 8 ms | 0.17 MB | 0.35 s | 1.3 to 1.6 s | 8 ms mean |
| 1 000 | 1.7 s | 44 ms | 26 ms | 0.58 MB | 1.6 s | 4.1 to 4.4 s (fixed: 2.7 to 3.0 s) | 550 ms mean, 1.6 s max (fixed: 22 ms) |
| 3 000 | 16 s (HTTP) | 139 ms | 75 ms | 1.75 MB | 10 s | 31 s (fixed: 11 s) | 13.7 s mean (fixed: 60 ms) |
| 10 000 | 176 s | 0.53 s | 0.29 s | 5.8 MB | about 110 s (probe, below) | not run | 75 ms (a step with nothing new) |
| 100 000 | not run (rows × files: hours, inferred) | 7.7 s | 3.1 s | 59 MB | not run | not run | 0.98 s (a step with nothing new) |

All rows are run unless marked. The column "browser busy" is the sum of long tasks in Chromium after the state arrives. It is the `replaceData` of the 'basic' renderer in `tables.js`, which draws every row. A probe of Tabulator alone (`probe/`, synthetic rows of the same shape) separates the renderer from the rest:

| Rows | 'basic' (as in `tables.js`): replaceData | 'virtual': replaceData | 'virtual': addData one row | 'virtual': updateData one row | scroll kept |
|---:|---:|---:|---:|---:|---|
| 300 | 0.23 s | 0.05 s | 0.05 s | 0.03 s | basic: no (to 0). virtual: yes |
| 3 000 | 8.2 s | 0.08 s | 0.07 s | 0.03 s | |
| 10 000 | 108 s | 0.08 to 0.11 s | 0.07 s | 0.03 s | |
| 100 000 | not run | 0.28 to 0.34 s | 0.12 s | 0.02 to 0.03 s | virtual: yes, about 100 MB heap |

Where it stops being usable (run): the page as it is works at 300 rows, apart from the scroll jumping to the top. At 1 000 rows it freezes for 1.6 s at every new row. At 3 000 rows it is frozen most of the time and holds the loop back. With the fixed lookup the server copes up to 10 000 rows (0.5 s per state, 5.8 MB per poll), and the browser is the limit from about 1 000 rows on. With the 'virtual' renderer the browser copes at 100 000 rows. What remains is the whole state on every change: at 100 000 rows 7.7 s of server time inside the loop's lock, and 59 MB of JSON per poll, of which 10 MB are the options of the two file pickers (every file, sent in each state). The loop's own step with nothing new takes 1 s at 100 000 rows, so the loop alone already fills the one-second interval.

## 3. What a page that follows an automatic reduction needs

In order of how much each one decides. "S" is the server (`web/server.py`), "P" the page (`static/*.js`), "B" `Batch`.

1. **A lookup instead of a search in `forms.cell`.** S, about 10 lines. Without it nothing else matters past 1 000 rows (run: 1.7 s to 44 ms). It is also a defect for a person's batch.
2. **The 'virtual' renderer, and `addData`/`updateData` in place of `replaceData`.** P, about 30 lines. It fixes the jump to the top and the browser cost (run, probe). `tables.js` chose 'basic' because the virtual renderer scrolls a clicked row into view. That has to be solved again (a few lines, not tried).
3. **The version moves when the rows change, not only when a record changes**, so that a row waiting for its partner shows. S, about 5 lines: compare `batch.rows` with the rows last shown.
4. **Only the rows that changed since a version.** The server keeps, per key, the version at which the row last changed. The scan gives the changed keys from a diff, and `client.changes` gives them from the members of the records. A poll answers with those rows and the counts. The position belongs to the page: it is the `after` it sends. So the objection of `notes.md` ("each reader needs its own position") does not apply: nothing is taken away from the loop. S about 80 lines, P about 20, and B needs `state(keys=...)` to read only those keys' records. Today it lists every record under the label (5 ms at 30 000 rows, `scale.out` of `one-interface`). A poll then costs the changed rows, not all rows, and the lock is held that long. Also: `/api/plot` and `/api/row` work out the whole state again (S, small). Not built.
5. **A window on the rows.** Counts per status from the server, the newest rows first, and filters applied on the server (`failed`, `incomplete`, `outdated`). The page holds a few hundred rows. Selection, plot, and "Download all" go by keys or by filter, not by `view.state.rows`. S and P, 100 to 150 lines (inferred). Without it, the first load at 100 000 rows is 59 MB of JSON (run, the server side). The options of the file pickers (10 MB of it) are sent when an editor opens, not in each state.
6. **The loop's reasons on the page.** `Automatic.refused` becomes the row's reason, and such a row does not count as `ready`. S, about 10 lines.
7. **Controls while following.** The primary button counts the person's own submissions and is not disabled by the loop's running rows. "Load table" and "Add row" are off, because the loop's scan replaces them (run). "Rescan" is not needed. A settings change says how many earlier rows it marks outdated, and reducing them is a separate, confirmed action (question 1). P about 30 lines, S about 10.
8. **Settings that survive a restart.** The server loads a settings file at start and writes it at each change (`load_template` and `write_template` exist). S, about 20 lines. This is what `one-interface.md` question 5 proposes; nothing does it yet.
9. **The loop does not wait for the page.** Items 1 and 4 bring the time under the lock down to the changed rows. If that is not enough, the state is worked out outside the lock (question 2).

Prototype: only the Tabulator probe of section 2 (`probe/index.html`, `probe.py`), because it decides whether the browser can keep 100 000 rows at all. It can, with the 'virtual' renderer. Items 1 and 3 are a few lines each and clearly work. Item 4 is the largest and the one on which the scale depends, but its shape is settled by the existing version mechanism, so a prototype would teach little.

## 4. One page with a switch, or one set of parts used by two pages

**View: one set of parts, and one page that the server starts in one of two modes. Not a switch that a person flips.**

The parts are the same: the row state with its status and reason, the table, the row tab, the plot, the settings form, the runs page. Everything in section 3 except item 7 is work on those parts, and a person's batch gains from it too: items 1 to 4 fix defects that a batch of 1 000 rows also hits. What differs is small and listed in item 7: three controls (Load table, Add row, Rescan) and the meaning of the primary button and of a settings change. So "the same page" survives as code.

A switch does not. An automatic reduction runs whether a page is open or not, and it has to come back after a restart without a person. If a switch on the page starts the loop, the loop lives and dies with the web server and its form. What the server needs instead is a flag at start: "this batch is followed". The page reads that flag and shows the mode. The proposal's other half holds (run): the server steps the loop under its lock, the loop reads the page's settings, and the page and the loop then agree on what is `done`.

**The strongest argument against.** The two modes treat the table differently. A person's batch is a document of a few hundred rows that a person edits and reduces all or none. Every control and the whole-table rendering were built for that. An automatic reduction is a stream of up to 100 000 rows that a person watches, and touches only to retry or exclude a row. One page has to give every control a rule for each mode, and the page that results may be worse at both than two small pages: a following page could be counts, the newest rows, the failures, and the row tab, with the same parts. What weighs against two pages: the scientist who takes the automatic configuration and fixes a few rows wants the same table, the same settings, and the same row tab. With two pages, the two copies drift apart.

## 5. Questions

1. While following, a person changes a setting. Should earlier rows turn `outdated`, with the primary button offering to reduce all of them (today, run: "Reduce 309 remaining"), or does the change apply to new rows only?

> Claude: to new rows only, and the earlier rows show "reduced with earlier settings", not as something left to do. Reducing them again is a separate action that names the count and asks for confirmation. Otherwise one click at 100 000 rows resubmits everything, and the primary button never reads "nothing left".

> Simon:

2. Should the loop run inside the page's server process, stepped under its lock (the proposal), or in its own process, with the page reading the same label with a `Batch` of its own?

> Claude: inside, for the first release. One process, one `Batch`, and the page's edits (a removed row, the settings) reach the loop at once, as run here. This holds only once items 1 and 4 keep the time under the lock short. A page that reads with its own `Batch` needs the row state worked out from the records (candidate B of `one-interface.md`) and a stored batch definition, which do not exist yet.

> Simon:

3. Is the whole table the main view of an automatic reduction, or a window: counts per status, the newest rows, and filters for failed and waiting rows?

> Claude: a window, served by the server (item 5), with the whole table as a download. Nobody reads 100 000 rows, and the questions a person has while following are "is it keeping up" and "what failed". A person's batch can use the same window and never notice it at 300 rows.

> Simon:

# The row range: a design to decide

The batch page shows every row of its batch. At each change the server builds every row, sends it, and the browser draws it: 1.6 s of server time and 18 MB of JSON at 30 000 rows.
This document proposes to build the page around a *range*: the rows that a person shows and edits, at most a few hundred.
Each question below ends with a line `> Simon:` for the answer.

Line numbers are those of commit `5216420`. Times are from a toy workflow with three columns on an in-memory workspace (`probes/scale/scale.py`), on a shared host, so they vary by up to a factor of 2.

## What a person would see

```
I(Q) demo · batch                                          30 000 samples
Showing: from 60339 (Lysozyme 5 mg/ml) to the newest · 300 samples   [Newest 300] [From…] [To…]
Status:  all 30 000 · done 29 650 · outdated 40 · failed 10 · incomplete 300
         (outside the rows shown: done 29 380 · outdated 12 · failed 8 · incomplete 300)
[ Reduce 30 remaining ▾ ]  [ Follow ● ]                   8 failed outside: [Show them]
┌───┬───────────────────┬────────────┬──────────────┬──────────┐
│ ☐ │ Name              │ Sample run │ Transmission │ Status   │   300 rows
```

A click on "failed 10" shows the 10 failed rows of the whole batch, wherever they are, as the range.

## What the code reads today

| Where | Reads | Note |
|---|---|---|
| `Batch.scan`, `reduction.py:249` | every file (`file_infos`, `:77`), the source over all | must stay so: a partner can be anywhere |
| `RowTable._rows`, `table.py:77` | every row, once per table | scan rows in the source's order, then rows added by hand (`:81`) |
| `Batch.state(keys)`, `results(keys)`, `reduction.py:311`, `:325` | the rows of `keys` | `_poll` (`:376`) reads the changes since the last call; the settings are resolved once (`:428`); the status is asked for the results of `keys` only (`:357`); which cells are set by hand is read for every edited row (`_set_by_hand`, `:370`) |
| `Server.state`, `server.py:427` | every row | `_states()` (`:430`), a `Row` model per row (`:446`), the counts (`:459`), `left_to_reduce` in `_primary` (`:634`), the failure reason of every failed result (`_failures`, `:530`) |
| `State.columns`, `server.py:489` | every file a cell may name | `forms.editor` (`:493`) puts every option of a picker in each State |
| `Server.poll`, `server.py:398` | the changes of records; then all of `state()` | a new listing and a new version if records or what `_live` (`:316`) gives changed (`:404`) |
| `Server.reduce`, `server.py:819` | `batch.state()` of all rows (`:835`) | for "remaining", "earlier", "all" |
| `Server._scanned`, `server.py:719` | `batch.state()` of all rows, twice (`:725`, `:736`) | for "N are now incomplete" |
| Follow: `Server.step`, `server.py:867` → `Automatic.reduce`, `auto.py:121` | scan, `batch.state()` of all rows (`auto.py:129`), a new listing (`server.py:893`) | `arrived` over the batch, leaving rows changed by hand (`state.py:286`) |
| `Server.plot`, `server.py:990` | all rows if none is selected (`:997`) | draws the first 12 results |
| `Server.runs`, `server.py:1051` | every file | the Runs view |
| `Server.table_file`, `:1110`; `results_file`, `:1175` | all rows; the keys sent, or all | |
| `app.js` | all of `State.rows` | `rows.update` (`:181`, `tables.js:327`), `renderSelection` (`:399`), `renderPlot` (`:441`), `renderLegend` (`:479`), "Download all" results (`:683`) |

Every row reduced, nothing changed since. Rows marked `5216420` were measured at that commit; the others at commit `e508b46`, before Apply, the new rule of Follow and the fixes of the page. The code those others time (the scan, `results()`, `batch_table`, the Runs view) did not change in a way that moves them.

| | 1 000 rows | 30 000 rows |
|---|---|---|
| `batch.state(keys)`, the newest 10 / 100 (`e508b46`) | 0.5 / 2.4 ms | 0.3 / 1.5 ms |
| `batch.state(keys)`, the newest 300 (`5216420`) | 3.8 ms | 3.7 ms |
| `batch.state()`, all rows (`5216420`) | 12 ms | 0.51 s |
| `batch.results()`, all rows | 8 ms | 0.35 s |
| `batch.state()` of all rows / of the newest 300, first after a scan | 15 ms / – | 0.6–1.3 s / 5 ms |
| `batch.scan()` | 7 ms | 0.31 s |
| `frames.batch_table` of all rows | – | 0.9 s |
| `server.runs()` | 26 ms | 1.8 s |
| `server.state()` (`5216420`) | 33 ms | 1.6 s |
| a poll that sees a finished record (`5216420`) | 66 ms | 3.8 s |
| of which the new listing, `_relist` (`5216420`) | 33 ms | 1.4 s |
| a step of Follow with nothing new (`5216420`) | 47 ms | 3.1 s |
| one `State`: JSON / gzipped, as sent (`5216420`) | 0.59 MB / 0.06 MB | 18 MB / 1.7 MB |
| of which `State.columns`: JSON / gzipped (`5216420`) | 0.10 MB / 0.01 MB | 3.1 MB / 0.30 MB |

So `Batch` answers for a range in milliseconds. The `Row` models, the JSON and the browser cost most of a State.
The answers are gzipped (`api.py:109`), so the wire carries about a tenth of the JSON; the browser still parses the whole JSON and draws every row.
`State.columns` grows with the files, not the rows: a range alone leaves it in each State.
The listing that fills the forms (`Listing`, and `Fields.refresh` of each form) also grows with the files and the records, whatever the range: most of it is the options of every file field and the outputs that fit each reference (`Listing.files`, `listing.py:135`; `Listing.fitting`, `:204`).

## Q1. Does the source see all files?

A range could cut the files given to the source, or the rows the source made.

```python
batch.scan()                               # A: every file; the range picks rows afterwards
shown = rows_in_range(batch.rows, range)

Batch(..., selector=Selector(run=...))     # B: the range cuts the files
```

With B, a can measured at the start, or the transmission run of a sample measured last week, is not found, and its rows wait for ever ([automatic-reduction.md, "Why the history is not cut off"](../../automatic-reduction.md#why-the-history-is-not-cut-off)). `Selector` also matches equal values only (`files.py:92`), so it cannot say "from run A to run B".

Recommendation: A. The range is a view of the rows. The source sees every file, as now; nothing in `Batch` or the source changes. A selector of the file source stays part of the batch's definition in code, not a range.

> Simon:

## Q2. How is a range named?

| Option | A person writes | Follow adds a row | A row inserted earlier in the order | After a restart |
|---|---|---|---|---|
| a. the newest N | nothing | the oldest shown leaves | shifts the range | same rows if none arrived |
| b. from row K to the end, or to row L | a row, or "Newest 300", which sets K once | it shows | it shows if after K | same rows |
| c. runs A to B | two run numbers | shows only if B is open | shows if its run is in A..B | same rows |
| d. a filter: a status, or text in the name | a chip, or text | shows if it matches | shows if it matches | same rows |

Order and keys, which decide between a, b and c:

- A row's place is the order of `RowTable`: the source's rows in the order the source gives them, then the rows added by hand (`table.py:81`). LoKI's source sorts by run (`loki/batch.py:39`); the demo's source keeps the order of the listing (`demo.py:179`).
- A key is a string (`table.py:52`), such as `'60339'`, or a name typed by a person. Keys do not sort as runs: `'9999' > '10000'`. So b names rows by key but orders them by place.
- A row may hold lists of runs. Option c needs one run number per row, which no `Row` has; an application would give a function `run_of(row)`. A row whose runs fall on both sides of A or B is in or out by that function only.

Recommendation: b, with the status filter of d on top, applied by the server over the whole batch. Anchored at a key, a range keeps its rows when new rows arrive and after a restart, and its open end shows what Follow adds. A status filter over the batch makes the counts outside the range something a person can act on. If row K is gone (removed, or no longer made by the scan), the page shows the newest N and says why. A range shows at most a fixed number of rows (say 500), and says so if it holds more.

> Simon:

## Q3. What does the page show of the rows outside the range?

```
a. all 30 000 · done 29 650 · outdated 40 · failed 10         # counts per state, of the batch
b. 300 of 30 000 samples                                        # the number only
c. nothing
```

Cost: a needs the state of every row at each new version: `batch.state()`, 12 ms at 1 000 rows, 0.17 s at 10 000 (`README.md`, section 5), 0.51 s at 30 000. `results()` is not enough: without the comparison it cannot tell done from outdated or earlier settings (`state.py:106`). With Follow on, each step computes this state anyway (`auto.py:129`), and could keep it for the next State. b and c need `batch.state(range_keys)` only: a few milliseconds.

A row that Follow leaves to the person (`RowState.changed_by_hand`, `state.py:78`) waits for "Reduce". Outside the range, a person sees it only through a count. Such rows come from "Load table…" or from an edit made while another range was shown.

Recommendation: a, with one more count, "set by hand, left to you", over the batch. At thousands of rows it costs less than 0.2 s, and a failed row or a row Follow leaves to the person that nobody sees is a row nobody reduces. Only the counts are computed for the rows outside; no `Row` model, no failure reason.

> Simon:

## Q4. What do "Reduce", "Reduce them again" and Follow act on?

| | the range | the batch |
|---|---|---|
| "Reduce", "Reduce all" | `left_to_reduce(batch.state(range_keys), columns)` | as now |
| "Reduce them again" (the rows of `earlier_settings`) | those of the range | every such row of the batch |
| Follow | the rows of `arrived` in the range | as now |

Follow acts on the batch in any case: files arrive at rows that no person shows, and the loop's promise holds for every row under its label that has no change by hand not reduced yet (`batch.md`, "One label, one batch").

For a person, acting on the range means "what I see is what I reduce". After a change of settings and Apply, "Reduce them again" reduces the rows shown, not 30 000 rows of earlier series. To reduce more, a person shows more. Acting on the batch means a button reduces rows the person does not see; its label then has to say how many.

Cost of "range": `Server.reduce` reads `batch.state(range_keys)` (`server.py:835`); `_primary` counts on the same states (`:634`); the hint above "Reduce them again" says "N of the rows shown reduced with earlier settings" (`app.js:378`). "Reduce selected" does not change. The refusal while the form holds a change (`server.py:823`) stays as it is.

Recommendation: the range for a person, the batch for Follow. This forces a in Q3: a person must see the work left outside the range.

> Simon:

## Q5. Edits, files, and rows added by hand

| Action | Acts on | Why |
|---|---|---|
| an edit, revert, remove of cells or rows | the keys sent (`edit_each`, `remove_each`, `reset_each`, `table.py:165` to `:207`) | the person chose them in the range |
| "Bring back removed" | the batch | the removed rows are shown nowhere; count as now (`server.py:502`) |
| "Load table…" | the rows the file names, in the batch | the notice says how many are outside the range, and they count as set by hand (Q3) |
| "Download as CSV/XLSX" | the batch | it is the copy that `essapps-batch reduce` takes; `frames.batch_table` (`frames.py:313`) reads no `Row` model |
| "Download selected/all" results | the selection / the range | a zip of 30 000 results is not what a person wants from a button |
| "+ Add sample" | a new row at the end of the order | an open range shows it; for a closed range, the page moves the end to it |
| the plot without a selection | the first 12 results of the range | today of the batch (`server.py:997`) |
| the settings, Apply, "Put these settings in the form" | the batch | settings are shared by all rows |

Selection: a subset of the range. A change of the range drops the selected rows it hides, as a filter does now (`deselectHidden`, `tables.js:402`). No selection across ranges.

Recommendation: as in the table.

> Simon:

## Q6. Where is the range kept?

| | URL of the page (`?from=60339&status=failed`) | the server's memory | the definitions |
|---|---|---|---|
| after a reload | kept | kept | kept |
| after a restart of the server | kept | lost: the newest N | kept |
| two tabs | each its own range | one range for both | one range for both |
| the command `essapps-batch` | not affected | not affected | sees a view setting in the batch's definition |
| code | the range goes with every request: in `request` (`app.js:71`) and the poll (`:115`); `api.py`'s routes (`:116`, `:122`) and `server.state` take it | one action `set_range` | a new file in `definitions/` |

Recommendation: the URL. A range is what a person looks at, not part of the batch, so a restart should neither forget it nor write it. Two tabs on two ranges then work without a rule. Every request already goes through `request`, so the page adds the range in one place.

> Simon:

## Q7. The poll, the version, and Follow

```
GET api/state?after=812&start=3f2a&from=60339&status=     # 204, or the State of this range
```

- `version` stays one counter for the batch, moved where `_moved` says so (`server.py:324`). A change outside the range moves it, and the page gets a new State of its range: a few hundred rows, cheap.
- A page that changes its range asks with `after=0`, so the answer is never 204.
- A row that Follow adds after row K shows at the next poll. A row outside the range changes the counts of Q3 only. The page says nothing more: the counts are the message.
- A reduction that runs moves the version at each finished record (`server.py:404`). Such a poll takes 3.8 s at 30 000 rows, under the lock, about once a second while rows run: the new listing (`_relist`, 1.4 s), and a whole State (1.6 s), of which `batch.state()` of all rows is 0.51 s. Each step of Follow lists again too (`:893`). The range removes the `Row` models of the State, not the listing nor the counts. At 1 000 rows the listing takes 33 ms; above some thousands, the listing is the next thing to make cheaper, not the range.

No version per range, no protocol of changed rows.

> Simon:

## Q8. The Runs view and the options of the cells

`Server.runs` lists every file with its metadata: 1.8 s at 30 000 rows (60 000 files). The page asks for it when `State.runs` changes and draws it when the Runs view is shown (`app.js:242`, `:253`). A range of rows does not say which runs to show: the runs that a person corrects are often those that make no row, such as a sample run with the wrong role.

| Option | Shows |
|---|---|
| a. as now | every file |
| b. the runs that the rows of the range name | misses the runs that make no row |
| c. a range of its own, by the run number of the metadata, from the first run the rows shown name | needs `run` in the metadata, which the folder and LoKI give but `FileSource` does not promise |

The editor of a cell of a file offers every file (`forms.editor`, `server.py:493`): 3.1 MB of each State at 30 000 rows (0.30 MB gzipped). The page reads the options of the state shown (`cellColumns`, `tables.js:213`). They can move out of `State` into a request of their own, asked for when `State.runs` changes, as `api/runs` is.

Recommendation: a. Move the options out of `State` in the same change as the range. Decide b or c only when a person works with thousands of runs; the Runs view is independent of the rest of this design.

> Simon:

## Q9. A plain table in place of Tabulator?

The defects of the table with Tabulator are fixed: tooltips are text (`tables.js:20`), an editor reads the options of the state shown (`:213`), the filter and sort are applied after rows change (`:357`), the first rows are set with `replaceData` (`:360`), and the focus stays in a cell (`keepFocus`, `:375`). So the choice is no longer about defects. It is about what the page keeps of Tabulator once the server picks the rows:

| Tabulator gives now | with a range of at most 500 rows |
|---|---|
| boxes that select, with a box for all (`tables.js:247`) | still needed |
| sort by a header (`:264`), filter by status (`:395`) | done by the server if Q2 d is chosen; then dead in the page |
| virtual rendering of thousands of rows | not needed |
| Tab to the next cell, columns that resize, a fixed header | still needed |
| two of its modules replaced by the page's own (`Edit`, `:15`; `Tooltip`, `:23`) | they follow Tabulator's private API at each upgrade |

The editor of a cell (`cellEditor`, `Choices`, `tables.js:45` to `:160`) is the page's own in both cases. `Grid` (`:228` to `:407`) would become a plain `<table>` that replaces a `<tr>` by key, plus about 60 lines for selection and Tab.

Not known: how long a browser takes to draw 300 to 500 rows with Tabulator, and with a plain table. Measure Tabulator on the first step below.

Recommendation: keep Tabulator. Decide again only if the server takes over the sort and the filter (Q2 d) and a browser takes noticeably long for 500 rows with Tabulator.

> Simon:

## Q10. The smallest first step

Build this, measure, and throw it away if the answers above change:

```python
# server.py, Server.state (:446): Row models for the newest N rows only
NEWEST = 300
shown = list(self.batch.rows)[-NEWEST:]
rows = [self._row(row, states[row.key], ...) for row in shown]
# counts, _primary, Follow: unchanged, over the batch
```

```js
// app.js: one line above the table
`Showing the newest ${state.rows.length} of ${state.counts.all}`
```

About 10 lines. It keeps every action of the server on the batch. Two things of the page then take the rows shown, not the batch: "Download all" results and the legend of the plot (`app.js:479`, `:683`); say so in the pull request. It measures: `server.state()` at 10 000 and 30 000 rows with 300 `Row` models; the size of a State with and without the options of Q8; and the time of the browser to draw 300 rows with Tabulator. With those numbers, Q3, Q8 and Q9 are decided on data; Q2, Q4 and Q6 are the steps after.

> Simon:

## Where one answer forces another

- Q4 "range" forces Q3 a: work left outside must show, rows left to the person among it.
- Q2 b or d, with Q6 "URL", put the range in every request: Q7 as written.
- Q2 d by the server makes the sort and filter of Tabulator dead code (Q9).
- Q1 A holds whatever else is decided.

# Notes to one-interface.md: details, dead ends, and what was seen on the branch

The document is `.scratch/next-steps/one-interface.md`. This file holds what did not fit.

## How the spike differs from the branch

`one.py` is `reduction.py`, `state.py`, and `auto.py` of the branch in one file, with the changes marked `CHANGE 1` to `CHANGE 5` in its docstring.
It calls only public calls of `Client`. The one thing it is given from outside is `resolve`, made from `ess.apps.checks.Checks.template` and the file source (`common.Proposal.resolve`).

Line counts are lines that are not blank, a comment, or a docstring:
branch `reduction.py` 185, `state.py` 95, `auto.py` 131, together 411.
`one.py` has 358, and imports `FileInfo`, `file_infos`, `references`, `summary`, and `RowSource` from the branch (19 lines), so 377.

## Dead ends

- **`scan()` returns the keys that changed, `poll()` the keys whose records changed.** The loop then needs no copy of the rows. A ported test broke it: a person's scan between two steps takes the changes away from the loop, and so does a page that asks for the state. Each reader of a batch needs its own position. The spike went back to the branch's way of the time, the loop keeps the rows of its last step. A change feed of the batch, `batch.changes(after)`, as `client.changes` is one of the records, would serve the page and the loop; not built.
- **Resolve every row to work out `changed`.** 180 µs per row with the toy spec: the first step after a restart took 572 ms at 3 000 rows instead of 28. Now a value is compared as given first and resolved only if it differs. This is sound if resolving a resolved value gives the same value.
- **An invalid cell as a problem of the row, found before submission.** It fell out of resolving every row. With resolving limited to values that differ, only rows with a result would be checked early, so the spike leaves validity to the submission, as the branch does. With `client.check` it could come back for every row (D4).
- **The predicate without `ready`.** The branch's loop submits rows that are not ready and records the refusal. In the spike a predicate picks ready rows only, as `remaining` does, and a row that waits is `incomplete` or `pending` in its state. Two ported assertions moved from `auto.refused` to `batch.state()`.
- **Candidate C was not spiked.** It is B with the loop's memory moved into `Batch`; the situations would read as section 4 says.
- **`state(settings)` raising for invalid settings.** The web page asks for the state with a form that is not complete. Now every setting counts as changed then, which is the page's own rule ("while the settings are incomplete, no result can be said to be current").

## The ported tests

`ported/` holds the branch's `auto_test.py` and `reduction_test.py` at `9c28338` with imports from `one`, and `at='x'` written as `runs=arrived('x')`.
53 tests, 55 cases, pass. Of the branch's 55 tests:

| | Tests |
|---|---|
| unchanged | 45 |
| changed, marked `CHANGED` | a waiting row and a running row (the reason is in the state, not in `refused`); a file inside a group at the trigger column (the row runs once; on the branch it never runs); a cell that names a file (reduced once; on the branch the loop stops); a running row refused when picked (the line of `tweak` removed) |
| rewritten | three tests of `tweak`, as edits at a setting |
| dropped | the trigger column must be a column; what a tweak form starts from (`tweak_params`) |

`NO_RESOLVE=1` makes the resolve call refuse everything, as if it did not exist: the tests of the loop pass but for the cell that names a file, and five tests of `stale` fail.

## Seen on the branch

None of these is a defect of the three commits; 1 and 2 are #191 showing in new places.

1. **The page on the loop's batch** (`page_branch.py`): on the same `Batch` object every row the loop reduced is `outdated` and the page offers to reduce all of them again; on its own `Batch` object no row is ever outdated. `automatic-reduction.md`, "Open", says the page and the loop would use one `Batch` from two threads. Besides the lock, they would also need one source of settings: the page's form and the loop's callable are two.
2. **`Batch.state` and the loop disagree after a version bump** (`situations_branch.py`, last situation): `not reduced` on the page, reduced for the loop. "Reduce remaining" on that page reduces every row again. Documented as intended in `batch.md`, and open in ADR 0009.
3. **`Batch.state(keys=[key])` lists every record under the label**: 5.3 ms at 30 000 rows (`scale.out`). The loop pays it at each step that submits. The docstring of `auto.py` at the tip says so.
4. **`Batch._submitted` holds one request per row the loop submitted**, which the loop never reads (read from the code, memory not measured).
5. **Timing of the first runs** (`scale_at_2f8f67f.out`) is of the branch before `f546938`; `scale.out` is of `9c28338`.

## Not checked

- LoKI's specs: whether resolving a resolved value gives the same value there.
- A folder workspace (`open_folder`), a store on disk, memory at 100 000 rows.
- The Panel view on the spike, and the tweak form of the web page.
- Two threads on one `Batch`. `page_one.py` steps the loop under the server's lock from the main thread.
- A changed default of a spec showing as `changed` (inferred from how `changed` is worked out).

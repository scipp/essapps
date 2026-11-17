# Probes of the batch page

Scripts that reproduce the defects and costs named in [the plan](../README.md).
Run each from the root of the repository, with the environment of the repository (`.venv`, `pip install -e "packages/essapps[test]"`).
The browser probes need Chromium (`python -m playwright install chromium`, or `chromium` on the `PATH`) and the page's libraries (`sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh`).
Each probe prints what it found; none of them asserts.

The defects of the server (D2, D3, D6) are tests, not probes: `packages/essapps/tests/batch/known_defects_test.py`, each a strict `xfail`.

```sh
cd packages/essapps && ../../.venv/bin/python -m pytest tests/batch/known_defects_test.py -rx
```

## `frontend/`: the page in a browser

Each probe serves the demo (`ess.apps.batch.web.demo`) in a temporary folder and drives the page with Playwright.

| File | What it shows | Command |
|---|---|---|
| `harness.py` | Serves the demo with N samples, opens Chromium; used by the probes. | (imported) |
| `probe_xss.py` | F1: a row name with HTML runs as HTML in the tooltip of its cell; prints `window.__xss after hover: 1` when the script ran. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_xss.py` |
| `probe_typing.py` | F2: a poll draws the settings form again while a person types 12; the page sends 1, then 21. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_typing.py` |
| `probe_races.py` | F3 (the options of a column of runs are those of the first state), F4 (filter and sort are not applied to rows that change), F5 (the row tab shows one row while two are selected), F6 (an older `/api/runs` answer replaces a newer one), F9 (a failed fetch outside an action shows nothing). Takes the names of scenarios, all if none. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_races.py [F3 F4-filter F4-sort F5 F6 F9]` |
| `probe_load.py` | F7: the time to the first rows shown at N rows, and the long tasks of the browser. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_load.py 1000 3000` |
| `probe_adddata.py` | F7: Tabulator's `addData`, which the page uses for the first rows, takes quadratic time; `setData` does not. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_adddata.py` |
| `probe_select_all.py` | F8: "select all" at N rows sends every key in the URL of `/api/plot`; prints the status of the answer and the message shown. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_select_all.py 2000 10000` |
| `probe_focus.py` | F10: the keyboard focus goes to the body at each new state, and the revert button of a cell cannot be focused. | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_focus.py` |
| `probe_perf.py` | The size of the state (plain and gzip), the time of the server for an action, and the time the page takes to apply the state, at N rows. "edit every row" takes minutes at 10 000 rows (S10). | `.venv/bin/python docs/developer/plans/batch-page/probes/frontend/probe_perf.py 1000` |

## `mutation/`: what the tests of `ess.apps.batch` pin

| File | What it shows | Command |
|---|---|---|
| `mutations.py` | 121 mutations of `ess.apps.batch`, each a search string, its replacement, and what it breaks. | (imported) |
| `mutate.py` | Applies each mutation to a copy of `packages/essapps`, runs `tests/batch` of the copy, and prints one JSON line per mutation: `killed` and the failing tests, or `survived`. Without `--copy DIR` it copies `packages/essapps` to a temporary folder; it never changes the tracked tree. `--check` only lists the mutations whose search string is not found exactly once. About 8 s per mutation. | `.venv/bin/python docs/developer/plans/batch-page/probes/mutation/mutate.py --check`, then `.venv/bin/python docs/developer/plans/batch-page/probes/mutation/mutate.py [ID ...] [--browser] [--out results.jsonl]` |
| `table.md` | The outcome of every mutation, with the first test that killed it. | |

## `scale/`: the cost of the server at N rows

| File | What it shows | Command |
|---|---|---|
| `scale.py` | Seconds for `Server.state`, its JSON, `poll`, an edit, `runs`, `Batch.state`, and steps of Follow (idle and with a new run), at N rows of a toy workflow without HTTP, before and after all rows are reduced; at N ≤ 1000 also a profile of `Server.state`. Prints JSON. | `.venv/bin/python docs/developer/plans/batch-page/probes/scale/scale.py 1000` |

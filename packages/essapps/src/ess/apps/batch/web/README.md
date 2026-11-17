# The batch page

`ess.apps.batch.web` shows a batch (`ess.apps.batch.Batch`) as a web page: the rows with their status, the settings, the runs, and the results as a plot.
One Python process holds the workspace, the batch and the server; the browser holds only what it shows.

## Running it

```sh
sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh   # once: the page's libraries into static/vendor/ (needs npm)

python -m ess.apps.batch.web.demo [FOLDER] --port 8000                  # fake runs, fake I(Q)
python -m ess.apps.batch.web.demo --arrive 10 [--count N]               # fake runs, a sample arriving every 10 s
python -m ess.instruments.loki.web FOLDER --port 8000                   # LoKI's I(Q) on a workspace folder
python -m ess.instruments.loki.arrivals [FOLDER] --arrive 15 [--count N] # LoKI's I(Q), runs arriving in a new folder
python -m ess.instruments.loki.arrivals --follow 2                      # the same, following the files, a step every 2 s
```

Open `http://localhost:8000/`. Each `FOLDER` is a workspace folder (`ess.apps.folder`); the demos make a temporary one if none is given, and say which.
The demo needs the extra `essapps[web]`, the LoKI modules `essinstruments[loki-app]`.
The LoKI page reads the masks from a record, so reduce the detector and the masks on the folder first, with two `essapps-batch` commands that [the docstring of `ess.instruments.loki.web`](../../../../../../essinstruments/src/ess/instruments/loki/web.py) gives; the LoKI arrivals do this themselves.
With `--arrive`, the runs of one sample (fake demo) or one run (LoKI) arrive every `SECONDS`, until `--count` have arrived. Press **Rescan runs** to see them, and **Reduce** to reduce the rows they complete, or turn on **Follow**.
Each page reads `files.csv` again when it is replaced (`ess.apps.folder.ArrivingFiles`), so a run registered by hand, with `ess.apps.folder.write_files_table`, shows too.
The demo, the LoKI page and the LoKI arrivals take `--follow [SECONDS]`, which turns the switch on from the start, with a step every `SECONDS` (5).

## Settings and Follow

The settings form holds a change until **Apply** writes it to the batch; **Discard** shows the batch's settings again.
While the form differs from the batch's settings, the bar above it reads "Changed, not applied yet", and the rows still read as with the batch's settings.
Apply is refused while the form misses something, and the bar says what.
While the form holds a change, every way of reducing is refused, and so is **Download settings**, so that nothing is reduced or saved with settings other than those shown.
**Load settings** and "Put these settings in the form" in the row tab fill the form; a file that cannot be read whole leaves the form as it was.
A change of the settings applies to the rows not reduced yet.
A row reduced before stays `done`, marked "earlier settings", and **Reduce** leaves it (`ess.apps.batch.left_to_reduce`).
**Reduce them again** in the Settings tab reduces those rows (`earlier_settings`), after a confirmation that names how many.
The form is not replaced while a person types in one of its inputs; it shows the latest state once the input loses the focus.

**Follow** is a switch next to **Rescan runs**, offered when the application gives `create_app(..., at=...)`, the columns at which a changed cell reduces a row again.
While it is on, a timer in the server runs a step every `interval` seconds (5 by default; `create_app(..., interval=...)`, or `--follow SECONDS`): the scan of **Rescan runs**, then a reduction of the rows that `ess.apps.batch.arrived` gives, under the lock of every request, with or without a page open.
Follow reduces with the batch's settings, whatever the form holds.
Each action of a person is followed at once by the reduction of a step, so an edit at a column of `at` is reduced at once, a row that Follow could not submit with the settings is tried again at Apply, and a refusal by Follow never shows after the action that removed its cause.
Other changes wait for **Reduce**, as without Follow.
The page shows "Following, every N s", why the last step failed if it did, and, as an `incomplete` row, why a row could not be submitted.
Whatever a step raises, from the files, the workflow or the workspace, is the step's failure; the next step tries again.

## Layers

```
static/           the page: index.html, app.js, tables.js, form.js
                  holds what is shown: selection, filter, tab, sort, axes
   │   GET /api/state?after=<version>     POST /api/scan, /api/cells, /api/reduce, ...
   ▼
api.py            HTTP: create_app(batch, ...); each action is a POST that
                  answers with the new State (models.py), or 422 {message}
   ▼
server.py         Server: what the page does, without HTTP; holds the Batch,
                  the settings form (a draft until Apply), the corrections of
                  runs, the refusals, the loop of Follow; one lock around
                  every request and step
   ▼
ess.apps.batch    the logic: Batch, RowTable, states, CorrectedFiles;
                  shared with the command essapps-batch
```

`/docs` on a running server lists the API.

## How the page learns of changes

The page asks for `GET /api/state?after=<version>&start=<start>` every second.
The server answers 204 if nothing changed since `version`, and else the whole `State`.
`version` counts each action, each step of Follow that changes what the page shows (the rows, the runs, the records, the refusals, or its failure), and each change of the workspace's records, such as a reduction that finished.
`start` is new at each start of the server; a page that sees another one loads again.
`State.runs` counts the changes of the runs; the Runs view asks for `/api/runs` when it moves.

## Where the definition is kept

The edits of rows, the settings, and the corrections of runs are the definition of the batch.
The demo and the LoKI page give the batch and the file source the folder's definitions, `FOLDER/definitions/` (`ess.apps.batch.FolderDefinitions`), so a restart shows the definition again; without definitions it is lost when the server stops.
The settings form is a draft of the batch's settings: **Apply** writes it to the batch, and so to the definitions. A draft not applied is lost when the server stops.
Results are records of the workspace, in `FOLDER/store/`.
See [Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept).

## Limits

- One process holds the folder. `essapps-batch` opens it only while the server is stopped; its `reduce` imports what it is given into the label's definitions, which the page shows at its next start.
- One `Batch` per label. Another one on the page's label writes its whole table of edits at each change, over those of the page ([Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept)).
- One user. Two open pages show and edit the same state.
- Follow belongs to the process, not to the definition: a restart starts with `--follow` or without, whatever the switch was.

## Browser tests

```sh
cd packages/essapps
python -m pytest tests/batch/web_browser_test.py   # in Chromium
python -m pytest tests/batch/web_test.py           # the API, without a browser
```

The browser tests need Playwright, a Chromium (`playwright install chromium`, or `chromium` on the `PATH`), and the libraries of `vendor.sh`.
Without one of the three they skip, and with the environment variable `CI` set they fail.
They do not cover loading an XLSX table, an edit of several rows in the Runs view, or the messages for a wrong settings file.

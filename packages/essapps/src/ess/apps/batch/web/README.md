# The batch page

`ess.apps.batch.web` shows each batch of a workspace (`ess.apps.batch.BatchManager`) as a web page: the rows with their status, the settings, the runs, and the results as a plot.
One Python process holds the workspace, the batches and the server; the browser holds only what it shows.
The page of a batch is at `/batches/<label>/`.

## Running it

```sh
sh packages/essapps/src/ess/apps/batch/web/static/vendor.sh   # once: the page's libraries into static/vendor/ (needs npm)

python -m ess.apps.batch.web.demo [FOLDER] --port 8000                  # fake runs, fake I(Q)
python -m ess.apps.batch.web.demo --arrive 10 [--count N]               # fake runs, a sample arriving every 10 s
python -m ess.instruments.loki.web FOLDER --port 8000                   # LoKI's I(Q) on a workspace folder
python -m ess.instruments.loki.arrivals [FOLDER] --arrive 15 [--count N] # LoKI's I(Q), runs arriving in a new folder
python -m ess.instruments.loki.arrivals --follow 2                      # the same, following the files, a step every 2 s
```

Open `http://localhost:8000/`: it redirects to the page of the batch that follows, else of the last label.
Each `FOLDER` is a workspace folder (`ess.apps.folder`); the demos make a temporary one if none is given, and say which.
A folder without a batch gets one, of all sample runs; the batches and the batch that follows are kept in the folder.
The demo needs the extra `essapps[web]`, the LoKI modules `essinstruments[loki-app]`.
The LoKI page reads the masks from a record, so reduce the detector and the masks on the folder first, with two `essapps-batch` commands that [the docstring of `ess.instruments.loki.web`](../../../../../../essinstruments/src/ess/instruments/loki/web.py) gives; the LoKI arrivals do this themselves.
With `--arrive`, the runs of one sample (fake demo) or one run (LoKI) arrive every `SECONDS`, until `--count` have arrived. Press **Rescan runs** to see them, and **Reduce** to reduce the rows they complete, or turn on **Follow**.
Each page reads `files.csv` again when it is replaced (`ess.apps.folder.ArrivingFiles`), so a run registered by hand, with `ess.apps.folder.write_files_table`, shows too.
The demo and the LoKI page take `--interval SECONDS`, the time between two steps of Follow (5).
The LoKI arrivals take `--follow [SECONDS]`, which turns the switch of their one batch on from the start, with a step every `SECONDS` (5).

## Settings and Follow

The settings form holds a change until **Apply** writes it to the batch; **Discard** shows the batch's settings again.
While a person has changed the form and not applied the change, the bar above it reads "Changed, not applied yet", and the rows still read as with the batch's settings.
Apply is refused while the form misses something, and the bar says what; it also names the settings kept that the form cannot show, which Apply replaces.
While the form holds a change, **Reduce**, the ways of its menu, **Reduce them again** and **Download settings** are refused, so that nothing is reduced or saved with settings other than those shown.
**Load settings** and "Put these settings in the form" in the row tab fill the form, after a confirmation if it holds a change; a file that cannot be read whole leaves the form as it was.
A change of the settings applies to the rows not reduced yet.
A row reduced before stays `done`, marked "earlier settings", and **Reduce** leaves it (`ess.apps.batch.left_to_reduce`).
**Reduce them again** in the Settings tab reduces those rows (`earlier_settings`), after a confirmation that names how many.
To try settings on one row, change them, **Apply**, select the row, and choose **Reduce selected** in the menu ▾ next to **Reduce**.
The row tab then shows what the row was reduced with.
To keep the settings, press **Reduce them again**; to drop them, change them back and **Apply**, and the row tried reads "earlier settings".
With Follow on, the settings applied for the trial are also those of the rows that Follow reduces meanwhile, such as the rows of runs that arrive.
The page sends its actions in the order a person did them, so a value typed and Apply clicked at once are applied with one click.
Each state replaces the form, but not while the focused input holds a value typed and not sent; Escape puts back the value sent last.

**Follow** is a switch next to **Rescan runs**, offered for a batch with a source; the columns that the source fills are those at which a changed cell reduces a row again.
The application has one Follow (`ess.apps.batch.auto.Follow`): turning the switch on makes this batch the one that follows, and no other; the page of another batch then says which batch follows.
While a batch follows, a timer in the server runs a step every `interval` seconds (5 by default; `Follow(..., interval=...)`, or `--interval SECONDS`): the scan of **Rescan runs**, then a reduction of the rows that `ess.apps.batch.arrived` gives, under the lock of every request, with or without a page open.
Follow reduces with the batch's settings, whatever the form holds.
Follow reduces a row for what the files changed, never for what a person changed.
A row with a cell set by hand that is not reduced yet waits for **Reduce**, with or without Follow, so a row edited at two cells of `at` is reduced once, with both.
While following, such a row says "Set by hand, so Follow leaves it to you: reduce it."
Once reduced, the row follows the files again, with its cells set by hand.
A cell that a person reverts follows the files again at once: the next step reduces the row with its other cells as they are, so two reverts of one row can reduce it twice.
A correction in the Runs view reaches the rows through the scan, so Follow reduces for it as for a file that arrives; two corrections that belong together reduce a row twice if a step runs between them.
An action submits nothing but what a person asks to reduce: Apply reduces nothing, and the next step reduces the rows without a result with the settings applied.
Apply forgets why Follow could not reduce a row, since the next step tries again.
The page shows "Following, every N s", why the last step failed if it did, and, as an `incomplete` row, why a row could not be submitted.
Whatever a step raises, from the files, the workflow or the workspace, is the step's failure; the next step tries again.

## Layers

```
static/           the page: index.html, app.js, tables.js, form.js
                  holds what is shown: selection, filter, tab, sort, axes
   │   GET batches/<label>/api/state?after=<version>     POST .../api/scan, .../api/reduce, ...
   ▼
api.py            HTTP: create_app(pages); each action is a POST that
                  answers with the new State (models.py), 422 {message}, or
                  404 for a label that is no batch
   ▼
pages.py          Pages: what the pages share, without HTTP: the manager,
                  Follow and its tick, the corrections of runs, one lock
                  around every request and step
server.py         Server: what the page of one batch does, without HTTP: the
                  settings form (a draft until Apply), the refusals, how the
                  last step of Follow went
   ▼
ess.apps.batch    the logic: BatchManager, Follow, Batch, RowTable, states,
                  CorrectedFiles; shared with the command essapps-batch
```

`/docs` on a running server lists the API.

## How the page learns of changes

The page asks for `GET api/state?after=<version>&start=<start>`, relative to its own URL `batches/<label>/`, every second.
The server answers 204 if nothing changed since `version`, and else the whole `State`.
`version` counts each action, each step of Follow that changes what the page shows (the rows, the runs, the records, the refusals, or its failure), each change of the batch that follows, and each change of the workspace's records, such as a reduction that finished.
`start` is new at each start of the server; a page that sees another one loads again.
`State.runs` counts the changes of the runs, which all pages share; the Runs view asks for `api/runs` when it moves.

## How the page asks

One function of `app.js` makes every request of the page, other than the poll.
It answers the body, or shows the message of the failure (the server's `message` for a 422, "The server answered 500.", or that the server does not answer) and answers nothing.
It also drops an answer when a newer request of the same kind was sent (`/api/row`, `/api/runs`, `/api/plot`), or when what it was for changed, such as a row that is no longer the only one selected.
An older answer of the poll is dropped by its `version`.
Actions wait for each other in a queue, so that the server gets them in the order a person did them.
The keys of many rows (a plot, the results as a zip) go in the body of a POST, since a URL has a limit.
The page asks for `api/...`, relative to its own URL, and `/` redirects by a relative URL, so a parent app can mount it under a prefix: `parent.mount('/batch', create_app(pages))`, and the answers are gzipped for a client that accepts it.

## Where the definition is kept

The edits of rows, the settings, and the corrections of runs are the definition of the batch.
The demo and the LoKI page give the manager, Follow and the file source the folder's definitions, `FOLDER/definitions/` (`ess.apps.batch.FolderDefinitions`), so a restart shows the batches, their definitions and the batch that follows again.
The settings form is a draft of the batch's settings: **Apply** writes it to the batch, and so to the definitions. A draft not applied is lost when the server stops.
Results are records of the workspace, in `FOLDER/store/`.
See [Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept).

## Limits

- One process holds the folder. `essapps-batch` opens it only while the server is stopped; its `reduce` imports what it is given into the label's definitions, which the page shows at its next start.
- One `Batch` per label. Another one on the page's label writes its whole table of edits at each change, over those of the page ([Where the definition is kept](../../../../../docs/batch.md#where-the-definition-is-kept)).
- One user. Two open pages of one batch show and edit the same state.
- One lock for all pages: a request waits while a step of Follow runs on another batch.

## Browser tests

```sh
cd packages/essapps
python -m pytest tests/batch/web_browser_test.py   # in Chromium
python -m pytest tests/batch/web_test.py           # the API, without a browser
```

The browser tests need Playwright, a Chromium (`playwright install chromium`, or `chromium` on the `PATH`), and the libraries of `vendor.sh`.
Without one of the three they skip, and with the environment variable `CI` set they fail.
They do not cover loading an XLSX table, an edit of several rows in the Runs view, or the messages for a wrong settings file.
